from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import urlsplit

from storage import Offer

ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{10,32}$')
NFT_PATTERN = re.compile(r'^([A-Za-z][A-Za-z0-9_]{0,63})-([1-9][0-9]{0,14})$')
LINK_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')
CURRENCY_TEXT = {'STARS': 'звёзд', 'TON': 'TON'}


def parse_link(value: str) -> tuple[str, str, int]:
    value = value.strip()
    if value.startswith('t.me/'):
        value = 'https://' + value
    if value.startswith('https://'):
        url = urlsplit(value)
        try:
            valid = bool(url.hostname) and not (url.username or url.password or url.port or url.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('Некорректная ссылка.')
        if url.hostname.lower() in ('t.me', 'telegram.me'):
            segments = url.path.strip('/').split('/')
            if len(segments) == 2 and segments[0] == 'nft':
                match = NFT_PATTERN.fullmatch(segments[1])
                if match:
                    name, number = match.groups()
                    return f'https://t.me/nft/{name}-{number}', name, int(number)
        if len(value) > 1024:
            raise ValueError('Ссылка слишком длинная.')
        return value, '', 0
    if NFT_PATTERN.fullmatch(value):
        name, number = NFT_PATTERN.fullmatch(value).groups()
        return f'https://t.me/nft/{name}-{number}', name, int(number)
    if LINK_ID_PATTERN.fullmatch(value):
        return '', '', 0
    raise ValueError('Укажите HTTPS-ссылку, t.me/nft/... или идентификатор.')


def parse_offer_args(args: list[str]) -> tuple[str, str, str, str, str]:
    """/offer link amount stars|ton; recognize NFT gift name from its slug."""
    if len(args) != 3:
        raise ValueError('Формат: /offer ссылка сумма stars|ton')
    link_raw, amount_raw, currency_raw = args
    gift_url, gift_name, gift_number = parse_link(link_raw)
    reference = '' if gift_url else link_raw
    if gift_name:
        spaced_name = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', gift_name).replace('_', ' ')
        title = f'{spaced_name} #{gift_number}'
    elif reference:
        title = reference
    else:
        host = urlsplit(gift_url).hostname or 'Предложение'
        title = host[:100]
    currency = currency_raw.upper()
    if currency not in CURRENCY_TEXT:
        raise ValueError('Поддерживаются только stars и ton.')
    try:
        amount = Decimal(amount_raw.replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError('Некорректная сумма.') from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal('1000000000'):
        raise ValueError('Сумма должна быть положительной и не более миллиарда.')
    if currency == 'STARS' and amount != amount.to_integral_value():
        raise ValueError('Stars указываются целым числом.')
    if currency == 'TON' and -amount.as_tuple().exponent > 9:
        raise ValueError('Для TON допускается не более 9 знаков после запятой.')
    return gift_url, title, reference, format(amount.normalize(), 'f'), currency


def item_label(offer: Offer) -> str:
    if offer.item_title:
        return offer.item_title
    name = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', offer.gift_name).replace('_', ' ')
    return f'{name} #{offer.gift_number}'


def price_label(offer: Offer) -> str:
    amount = offer.amount or str(offer.price_stars)
    whole, sep, fraction = amount.partition('.')
    try:
        whole = f'{int(whole):,}'.replace(',', ' ')
    except ValueError:
        pass
    return whole + ('.' + fraction if sep else '') + ' ' + CURRENCY_TEXT.get(offer.currency, offer.currency)


def status_label(value: str) -> str:
    return {'ACTIVE': 'Активен', 'ACCEPTED': 'Принят', 'DECLINED': 'Отклонён',
            'CANCELLED': 'Отменён', 'EXPIRED': 'Истёк', 'TRANSFER_CONFIRMED': 'Передача подтверждена'}.get(value, value)


def miniapp_link(username: str, offer_id: str) -> str:
    return f'https://t.me/{username}?startapp={offer_id}'


def start_link(username: str, action: str, offer_id: str) -> str:
    return f'https://t.me/{username}?start={action}_{offer_id}'


def custom(glyph: str, role: str, ids: dict[str, str]) -> str:
    # No invented IDs: if an original custom emoji was not imported, text is
    # readable with the Unicode base glyph. Import actual IDs via /importemoji.
    emoji_id = ids.get(role, '')
    return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>' if emoji_id.isdigit() else glyph


def offer_text(offer: Offer, emoji_ids: dict[str, str]) -> str:
    gift = escape(item_label(offer))
    link = (f'<a href="{escape(offer.gift_url, quote=True)}">{gift}</a>'
            if offer.gift_url else gift + (f' · <code>{escape(offer.gift_name)}</code>' if offer.gift_name else ''))
    clock = custom('⏱', 'clock', emoji_ids)
    return (
        f'{custom("🎁", "gift", emoji_ids)} <b>Предложение на ваш подарок</b>\n'
        f'{link}\n\n'
        f'{custom("💎", "price", emoji_ids)} Цена предложения: <b>{escape(price_label(offer))}</b>\n\n'
        f'{clock} Действует ещё <b>6 часов</b>'
    )
