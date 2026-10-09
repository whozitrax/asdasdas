from __future__ import annotations

import gzip
import json
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qsl

from aiohttp import web

from config import Settings
from logic import ID_PATTERN, item_label, status_label
from security import InvalidInitData, authenticated_user_id
from services.gifts import GiftPresence, TransferCheck, verify_transfer
from services.media import _public_gift_preview_image, _fragment_gift_lottie, _find_gift_model_sticker
from storage import Store

logger = logging.getLogger('giftrelayer.web')
WEB = Path(__file__).resolve().parent / 'web'
NFT_SLUG = re.compile(r'^([A-Za-z][A-Za-z0-9_]{0,63})-([1-9][0-9]{0,14})$')


def _nft_slug(offer):
    try:
        p = urlparse(offer.gift_url)
        if p.hostname not in ('t.me', 'telegram.me') or not p.path.startswith('/nft/'):
            return None
        slug = p.path[len('/nft/'):].strip('/')
        return slug if NFT_SLUG.fullmatch(slug) else None
    except Exception:
        return None


def _money(offer):
    return offer.amount or str(offer.price_stars)


def _payload(offer, actor_id, username=None, buyer_name=None):
    seconds = max(0, offer.expires_at - int(time.time()))
    title = item_label(offer)
    currency = 'звёзд' if offer.currency == 'STARS' else offer.currency
    return {
        'id': offer.id,
        'status': offer.status,
        'gift': {'slug': _nft_slug(offer) or '', 'name': title, 'url': offer.gift_url},
        'price': {'value': _money(offer), 'currency': currency},
        'seconds_left': seconds,
        'creator': {
            'id': offer.buyer_id,
            'username': username or None,
            'first_name': buyer_name or None,
            'link': (f'https://t.me/{username}' if username else None),
        },
        'is_creator': actor_id == offer.buyer_id,
        'action_blocked': 'OWN_OFFER' if actor_id == offer.buyer_id else (
            'NOT_RECIPIENT' if offer.seller_id not in (0, actor_id) else None),
        'nft_verification_available': bool(_nft_slug(offer)),
    }


def _error(message, status=400, code='error'):
    return web.json_response({'ok': False, 'error': message, 'code': code}, status=status)


async def make_site(settings: Settings, store: Store, telegram_application, notify_buyer) -> web.AppRunner:
    routes = web.RouteTableDef()

    async def with_buyer(offer, actor_id):
        # Resolve missing buyer username with the Bot API when accessible.
        username = (getattr(offer, 'buyer_username', '') or '').lstrip('@')
        first_name = getattr(offer, 'buyer_first_name', '') or ''
        if not username or not first_name:
            try:
                chat = await telegram_application.bot.get_chat(offer.buyer_id)
                username = (getattr(chat, 'username', None) or username or '').lstrip('@')
                first_name = getattr(chat, 'first_name', None) or first_name
                await store.save_buyer_profile(offer.id, offer.buyer_id,
                                               username=username, first_name=first_name)
            except Exception as exc:
                logger.debug('Buyer profile unavailable offer_id=%s error=%s', offer.id, type(exc).__name__)
        return _payload(offer, actor_id, username=username, buyer_name=first_name)

    async def get_authorized(request, *, allow_buyer=False):
        offer_id = request.match_info['offer_id']
        if not ID_PATTERN.fullmatch(offer_id):
            raise web.HTTPNotFound()
        raw = request.headers.get('X-Telegram-Init-Data', '') or request.query.get('init_data', '')
        try:
            actor = authenticated_user_id(raw, settings.bot_token, settings.max_init_age)
        except (InvalidInitData, ValueError) as exc:
            # Safe diagnostics: never log token, raw initData, user details, or signature/hash.
            try:
                field_names = {k for k, _ in parse_qsl(raw, keep_blank_values=True)}
            except ValueError:
                field_names = set()
            logger.warning(
                'MINIAPP AUTH REJECTED | offer_id=%s | reason=%s | '
                'init_data_present=%s | has_hash=%s | has_signature=%s | '
                'has_start_param=%s | configured_bot_id=%s',
                offer_id, str(exc), bool(raw), 'hash' in field_names,
                'signature' in field_names, 'start_param' in field_names,
                settings.bot_token.split(':', 1)[0],
            )
            raise web.HTTPUnauthorized(text=str(exc))
        await store.expire()
        offer = await store.get(offer_id)
        if not offer:
            raise web.HTTPNotFound(text='Оффер не найден.')
        if actor == offer.buyer_id:
            if not allow_buyer:
                raise web.HTTPForbidden(text='Нельзя принять собственное предложение.')
        elif offer.seller_id not in (0, actor):
            raise web.HTTPForbidden(text='Этот оффер предназначен другому пользователю.')
        return actor, offer

    @routes.get('/')
    async def root(request):
        raise web.HTTPFound('/app')

    @routes.get('/health')
    async def health(request):
        return web.json_response({'ok': True, 'mode': 'old-ui-new-offers'})

    @routes.get('/app')
    async def app(request):
        resp = web.FileResponse(WEB / 'index.html')
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @routes.get('/api/offers/{offer_id}')
    async def offer_info(request):
        try:
            actor, offer = await get_authorized(request, allow_buyer=True)
        except web.HTTPException as exc:
            return _error(exc.text, exc.status)
        return web.json_response({'ok': True, 'offer': await with_buyer(offer, actor)}, headers={'Cache-Control': 'no-store'})

    @routes.post('/api/offers/{offer_id}/accept')
    @routes.post('/api/offers/{offer_id}/decline')
    async def change_offer(request):
        try:
            actor, offer = await get_authorized(request)
        except web.HTTPException as exc:
            return _error(exc.text, exc.status, 'own_offer' if exc.status == 403 else 'error')
        action = 'ACCEPTED' if request.path.endswith('/accept') else 'DECLINED'
        result, changed = await store.act(offer.id, actor, action)
        if result != 'OK':
            return _error('Предложение уже обработано или недоступно.', 409, 'state_changed')
        try:
            await notify_buyer(telegram_application, changed)
        except Exception:
            logger.exception('Notification failed offer_id=%s', offer.id)
        return web.json_response({'ok': True, 'offer': await with_buyer(changed, actor)})

    # Keep the newer API working for any existing clients.
    @routes.post('/api/offers/{offer_id}/decision')
    async def decision(request):
        if request.content_length is not None and request.content_length > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=request.content_length)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return _error('Invalid JSON')
        if not isinstance(body, dict) or body.get('action') not in ('accept', 'decline'):
            return _error('Invalid action')
        try:
            actor = authenticated_user_id(body.get('init_data', ''), settings.bot_token, settings.max_init_age)
        except (InvalidInitData, ValueError):
            return _error('Откройте Mini App из Telegram заново.', 401)
        try:
            checked_actor, offer = await get_authorized_for_actor(request.match_info['offer_id'], actor)
        except web.HTTPException as exc:
            return _error(exc.text, exc.status)
        result, changed = await store.act(offer.id, checked_actor, 'ACCEPTED' if body['action'] == 'accept' else 'DECLINED')
        if result != 'OK':
            return _error('Предложение уже обработано или недоступно.', 409)
        try:
            await notify_buyer(telegram_application, changed)
        except Exception:
            logger.exception('Notification failed offer_id=%s', offer.id)
        return web.json_response({'ok': True, 'status': changed.status, 'status_label': status_label(changed.status)})

    async def get_authorized_for_actor(offer_id, actor):
        if not ID_PATTERN.fullmatch(offer_id):
            raise web.HTTPNotFound()
        await store.expire()
        offer = await store.get(offer_id)
        if not offer:
            raise web.HTTPNotFound()
        if actor == offer.buyer_id or offer.seller_id not in (0, actor):
            raise web.HTTPForbidden(text='Этот аккаунт не может подтвердить предложение.')
        return actor, offer

    @routes.post('/api/offers/{offer_id}/confirm-transfer')
    async def confirm_transfer(request):
        try:
            actor, offer = await get_authorized(request)
        except web.HTTPException as exc:
            return _error(exc.text, exc.status)
        if offer.status != 'ACCEPTED' or offer.seller_id != actor:
            return _error('Передача не ожидается.', 409, 'state_changed')
        slug = _nft_slug(offer)
        if not slug:
            return web.json_response({'ok': True, 'result': 'RETRY_REQUIRED',
                                     'message': 'Автоматическая проверка доступна только для NFT-подарков Telegram.'})
        result = await verify_transfer(telegram_application.bot, seller_id=actor,
                                      expected_buyer_id=offer.buyer_id, gift_slug=slug)
        # IMPORTANT: positive buyer visibility alone is not enough: require seller absence.
        if result.seller_presence == GiftPresence.PRESENT:
            return web.json_response({'ok': True, 'result': 'NOT_TRANSFERRED',
                                     'message': 'Подарок всё ещё принадлежит продавцу.'})
        if result.buyer_presence == GiftPresence.PRESENT and result.seller_presence == GiftPresence.ABSENT:
            transition, changed = await store.confirm_transfer(offer.id, actor)
            if transition != 'OK':
                return _error('Состояние изменилось.', 409, 'state_changed')
            try:
                await notify_buyer(telegram_application, changed)
            except Exception:
                logger.exception('Notification failed offer_id=%s', offer.id)
            return web.json_response({'ok': True, 'result': 'VERIFIED', 'offer': await with_buyer(changed, actor)})
        return web.json_response({'ok': True, 'result': 'RETRY_REQUIRED',
                                 'message': 'Telegram пока не дал достаточно данных для проверки.'})

    async def media_auth(request):
        actor, offer = await get_authorized(request, allow_buyer=True)
        slug = _nft_slug(offer)
        if not slug:
            raise web.HTTPNotFound(text='Это предложение не является Telegram NFT.')
        fake = SimpleNamespace(id=offer.id, gift_slug=slug, gift_url=offer.gift_url,
            seller_id=offer.seller_id or None, source_type='INLINE', chat_id=None,
            creator_id=offer.buyer_id, business_connection_id=None)
        return actor, offer, fake

    @routes.get('/api/offers/{offer_id}/gift-media')
    async def gift_media(request):
        try:
            actor, offer, gift = await media_auth(request)
        except web.HTTPException as exc:
            return _error(exc.text, exc.status)
        bot = telegram_application.bot
        sticker = await _find_gift_model_sticker(bot, gift, actor)
        if sticker:
            try:
                tgfile = await bot.get_file(sticker.file_id)
                data = bytes(await tgfile.download_as_bytearray())
                if getattr(sticker, 'is_animated', False):
                    data = gzip.decompress(data)
                    json.loads(data)
                    return web.Response(body=data, content_type='application/json',
                                        headers={'X-Gift-Media-Kind': 'lottie', 'Cache-Control': 'private,no-store'})
                if getattr(sticker, 'is_video', False):
                    return web.Response(body=data, content_type='video/webm',
                                        headers={'X-Gift-Media-Kind': 'video', 'Cache-Control': 'private,no-store'})
                return web.Response(body=data, content_type='image/webp',
                                    headers={'X-Gift-Media-Kind': 'image', 'Cache-Control': 'private,no-store'})
            except Exception:
                logger.exception('Sticker file unavailable offer_id=%s', offer.id)
        lottie = await _fragment_gift_lottie(offer.gift_url, gift.gift_slug)
        if lottie:
            return web.Response(body=lottie, content_type='application/json',
                                headers={'X-Gift-Media-Kind': 'lottie', 'Cache-Control': 'private,no-store'})
        preview = await _public_gift_preview_image(offer.gift_url)
        if preview:
            body, typ = preview
            return web.Response(body=body, content_type=typ,
                                headers={'X-Gift-Media-Kind': 'image', 'Cache-Control': 'private,no-store'})
        return _error('Изображение подарка недоступно.', 404)

    @routes.get('/api/offers/{offer_id}/gift-image')
    async def gift_image(request):
        try:
            actor, offer, gift = await media_auth(request)
        except web.HTTPException as exc:
            return _error(exc.text, exc.status)
        preview = await _public_gift_preview_image(offer.gift_url)
        if preview:
            body, typ = preview
            return web.Response(body=body, content_type=typ, headers={'Cache-Control': 'private,no-store'})
        return _error('Изображение недоступно.', 404)

    aio = web.Application(client_max_size=16384)
    aio.add_routes(routes)
    runner = web.AppRunner(aio, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host=settings.web_host, port=settings.web_port)
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    logger.info('Web server started at %s:%s', settings.web_host, settings.web_port)
    return runner
