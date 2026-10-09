from __future__ import annotations

import asyncio
import logging
import re
import time
from html import escape

from telegram import (
    BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat,
    CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent, LinkPreviewOptions,
    MessageEntity, Update, WebAppInfo,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, ChosenInlineResultHandler,
    CommandHandler, ContextTypes, InlineQueryHandler, MessageHandler, filters,
)

from config import load_settings
from logic import (
    ID_PATTERN, item_label, miniapp_link, offer_text, parse_offer_args,
    price_label, start_link, status_label,
)
from storage import Offer, Store
from web_server import make_site

logger = logging.getLogger('giftrelayer')
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
ADMIN_ID = 8919897939
LINK_ARG = re.compile(r'^(view|accept|decline)_([A-Za-z0-9_-]{10,32})$')
ACTION_CB = re.compile(r'^dec:(accept|decline):([A-Za-z0-9_-]{10,32})$')


async def admin_only(update: Update) -> bool:
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return False
    return True


def offer_keyboard(offer: Offer, username: str) -> InlineKeyboardMarkup:
    # Both buttons use deep links, so recipients can interact without inline bot callbacks.
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('❌ Отклонить', url=start_link(username, 'decline', offer.id), style='danger'),
        InlineKeyboardButton('✅ Принять', url=miniapp_link(username, offer.id), style='success'),
    ]])


def decision_keyboard(offer: Offer, base_url: str) -> InlineKeyboardMarkup:
    rows = []
    if base_url:
        rows.append([InlineKeyboardButton('🎁 Открыть Mini App',
                                         web_app=WebAppInfo(url=f'{base_url}/app?offer={offer.id}'),
                                         style='primary')])
    rows.append([
        InlineKeyboardButton('✅ Подтвердить принятие', callback_data=f'dec:accept:{offer.id}', style='success'),
        InlineKeyboardButton('❌ Отклонить', callback_data=f'dec:decline:{offer.id}', style='danger'),
    ])
    return InlineKeyboardMarkup(rows)


def link_preview(offer: Offer) -> LinkPreviewOptions:
    if offer.gift_url:
        return LinkPreviewOptions(url=offer.gift_url, is_disabled=False,
                                  show_above_text=True, prefer_large_media=True)
    return LinkPreviewOptions(is_disabled=True)


async def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    return (await context.bot.get_me()).username


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        await msg.reply_text('Используйте бота в личных сообщениях.')
        return
    store: Store = context.application.bot_data['store']
    if context.args:
        match = LINK_ARG.fullmatch(context.args[0])
        if match:
            action, offer_id = match.groups()
            await store.expire()
            offer = await store.get(offer_id)
            if offer is None:
                await msg.reply_text('Предложение не найдено.')
                return
            if offer.status != 'ACTIVE':
                await msg.reply_text(f'Статус предложения: {status_label(offer.status)}.')
                return
            if user.id == offer.buyer_id or (offer.seller_id not in (0, user.id)):
                await msg.reply_text('Это предложение недоступно этому аккаунту.')
                return
            settings = context.application.bot_data['settings']
            intro = {
                'view': 'Ознакомьтесь с предложением и подтвердите решение.',
                'accept': 'Вы действительно хотите принять это предложение?',
                'decline': 'Вы действительно хотите отклонить это предложение?',
            }[action]
            await msg.reply_text(
                f'🎁 {item_label(offer)}\n💎 {price_label(offer)}\n'
                f'⏱ До завершения: {max(0, (offer.expires_at - time.time()) / 3600):.1f} ч.\n\n'
                f'{intro}\n\n'
                'При подтверждении ваш Telegram ID привяжется к предложению.',
                reply_markup=decision_keyboard(offer, settings.public_base_url),
            )
            return
    await msg.reply_text(
        '👋 Добро пожаловать!\n\n'
        'Данный бот создан для автоматизированного оформления и отправки '
        'предложений о сделках между пользователями Telegram с использованием '
        'Telegram Stars ⭐ и TON 💎.\n\n'
        '🔐 Наша цель — сделать процесс создания и отправки предложений '
        'удобным, быстрым и понятным.\n\n'
        '✨ Бот находится в стадии разработки. Некоторые функции временно ограничены.'
    )


async def offer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    msg = update.effective_message
    if update.effective_chat.type != ChatType.PRIVATE:
        await msg.reply_text('Используйте /offer в личных сообщениях бота.')
        return
    try:
        url, title, reference, amount, currency = parse_offer_args(context.args)
    except ValueError as exc:
        await msg.reply_text(f'❌ {exc}\nПример: /offer t.me/nft/ViceCream-279721 900 stars')
        return
    store: Store = context.application.bot_data['store']
    settings = context.application.bot_data['settings']
    offer, secret = await store.create(
        buyer_id=ADMIN_ID, item_title=title, gift_url=url,
        amount=amount, currency=currency, duration_hours=settings.offer_hours,
        gift_name=reference,
        buyer_username=(update.effective_user.username or ''),
        buyer_first_name=(update.effective_user.first_name or ''),
    )
    # Не публикуем и не отправляем секретный ключ в Telegram.
    # В SQLite хранится только его SHA-256; исходное значение уничтожается.
    del secret
    ids = await store.get_emojis()
    username = await _bot_username(context)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=offer_text(offer, ids),
            parse_mode=ParseMode.HTML,
            reply_markup=offer_keyboard(offer, username),
            link_preview_options=link_preview(offer),
        )
    except TelegramError as exc:
        await store.act(offer.id, ADMIN_ID, 'CANCELLED')
        logger.exception('OFFER PREVIEW SEND FAILED offer_id=%s', offer.id)
        await msg.reply_text('❌ Не удалось отобразить оффер. Создание отменено. Проверьте custom emoji и логи.')
        return
    inline_value = f'@{username} {offer.id}'
    await msg.reply_text(
        f'Оффер создан: <code>{offer.id}</code>\n'
        'Вставьте в чате получателя:\n'
        f'<code>@{username} {offer.id}</code>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('📋 Скопировать ID', copy_text=CopyTextButton(text=offer.id))],
            [InlineKeyboardButton('📋 Скопировать Inline-команду', copy_text=CopyTextButton(text=inline_value))],
        ]),
    )
    logger.info('ADMIN OFFER CREATED offer_id=%s currency=%s amount=%s', offer.id, currency, amount)


async def myoffers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    store: Store = context.application.bot_data['store']
    await store.expire()
    offers = await store.list_buyer(ADMIN_ID)
    if not offers:
        await update.effective_message.reply_text('Нет предложений.')
        return
    await update.effective_message.reply_text('Последние предложения:\n\n' + '\n'.join(
        f'{o.id} — {item_label(o)} — {price_label(o)} — {status_label(o.status)}' for o in offers))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    if len(context.args) != 1 or not ID_PATTERN.fullmatch(context.args[0]):
        await update.effective_message.reply_text('Формат: /cancel OFFER_ID')
        return
    result, changed = await context.application.bot_data['store'].act(context.args[0], ADMIN_ID, 'CANCELLED')
    if result == 'OK' and changed:
        await update_inline_messages(context.application, changed)
    await update.effective_message.reply_text({
        'OK': '✅ Отменено.', 'NOT_FOUND': 'Не найдено.',
        'FORBIDDEN': '⛔ У вас нет доступа к данной команде',
        'ALREADY_DONE': 'Оффер завершён или истёк.'
    }[result])


async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text('Формат: /verify СЕКРЕТНЫЙ_КЛЮЧ')
        return
    offer = await context.application.bot_data['store'].get_by_secret(context.args[0])
    if not offer:
        await update.effective_message.reply_text('❌ Ключ не найден.')
        return
    await update.effective_message.reply_text(
        f'✅ Ключ действителен.\n'
        f'Оффер: {offer.id}\nНазвание: {item_label(offer)}\n'
        f'Сумма: {price_label(offer)}\nСтатус: {status_label(offer.status)}\n'
        f'Получатель: {offer.seller_id or "ещё не подтвердил"}\n\n'
        'Проверка ключа не подтверждает оплату или передачу предмета.'
    )
    # A Telegram command containing a secret remains in the administrator's private chat history.


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    me = await context.bot.get_me()
    emoji_ids = await context.application.bot_data['store'].get_emojis()
    await update.effective_message.reply_text(
        f'@{me.username}\nInline: {bool(me.supports_inline_queries)}\n'
        'Business API: не используется\n'
        f'Premium Emoji: {len(emoji_ids)}/3 импортировано\n'
        'Получатель: привязывается при первом подтверждении'
    )


async def importemoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    msg = update.effective_message
    replied = msg.reply_to_message
    if not replied:
        await msg.reply_text('Перешлите оригинальное сообщение с Premium Emoji и ответьте на него /importemoji.')
        return
    entities = list(replied.entities or []) + list(replied.caption_entities or [])
    values: dict[str, str] = {}
    sequence = ('gift', 'price', 'clock')
    for ent in entities:
        if ent.type != MessageEntity.CUSTOM_EMOJI or not ent.custom_emoji_id:
            continue
        try:
            glyph = replied.parse_entity(ent) if ent in (replied.entities or ()) else replied.parse_caption_entity(ent)
        except (ValueError, AttributeError):
            glyph = ''
        if '🎁' in glyph or '🎀' in glyph:
            role = 'gift'
        elif '💎' in glyph:
            role = 'price'
        elif any(ch in glyph for ch in ('⏱', '⏰', '⌛', '⏳')):
            role = 'clock'
        else:
            role = next((r for r in sequence if r not in values), None)
        if role:
            values[role] = str(ent.custom_emoji_id)
    if not values:
        await msg.reply_text('Telegram не передал custom_emoji_id в пересланном сообщении. Используйте /setemoji роль ID.')
        return
    await context.application.bot_data['store'].save_emojis(values)
    await msg.reply_text(f'✅ Импортированы роли: {", ".join(values)}. Проверьте /status.')


async def setemoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    if len(context.args) != 2 or context.args[0] not in ('gift', 'price', 'clock') or not context.args[1].isdigit():
        await update.effective_message.reply_text('Формат: /setemoji gift|price|clock CUSTOM_EMOJI_ID')
        return
    await context.application.bot_data['store'].save_emojis({context.args[0]: context.args[1]})
    await update.effective_message.reply_text('✅ Emoji ID сохранён.')


async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    match = ACTION_CB.fullmatch(query.data or '')
    if not match:
        await query.answer('Некорректная кнопка.', show_alert=True)
        return
    action, offer_id = match.groups()
    result, offer = await context.application.bot_data['store'].act(
        offer_id, query.from_user.id, 'ACCEPTED' if action == 'accept' else 'DECLINED')
    responses = {'OK': 'Предложение принято.' if action == 'accept' else 'Предложение отклонено.',
                 'NOT_FOUND': 'Не найдено.', 'FORBIDDEN': 'Предложение недоступно этому аккаунту.',
                 'ALREADY_DONE': 'Предложение уже завершено.'}
    await query.answer(responses[result], show_alert=result != 'OK')
    if result == 'OK' and offer:
        try:
            await query.edit_message_text(
                f'Предложение: {item_label(offer)}\nСтатус: {status_label(offer.status)}\n'
                'Оплата и передача подарка не происходят автоматически.')
        except TelegramError:
            pass
        await notify_buyer(context.application, offer)


async def update_inline_messages(application: Application, offer: Offer) -> None:
    """Change all known inline messages, when Inline Feedback supplied their IDs."""
    store: Store = application.bot_data['store']
    inline_ids = await store.list_inline_messages(offer.id)
    if not inline_ids:
        return
    username = (await application.bot.get_me()).username
    text = (f'🎁 <b>{escape(item_label(offer))}</b>\n'
            f'💎 Цена предложения: <b>{escape(price_label(offer))}</b>\n\n'
            f'Статус: <b>{escape(status_label(offer.status))}</b>\n'
            'Принятие оффера не подтверждает оплату или передачу NFT.')
    keyboard = None
    if offer.status in ('ACCEPTED', 'TRANSFER_CONFIRMED'):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton('↗️ Открыть сделку', url=miniapp_link(username, offer.id))
        ]])
    for inline_id in inline_ids:
        try:
            await application.bot.edit_message_text(
                text=text, inline_message_id=inline_id,
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramError as exc:
            # An older/inaccessible inline message must never block the offer.
            if 'message is not modified' not in str(exc).lower():
                logger.warning('INLINE UPDATE FAILED offer_id=%s type=%s', offer.id, type(exc).__name__)


async def notify_buyer(application: Application, offer: Offer) -> None:
    try:
        await update_inline_messages(application, offer)
    except Exception:
        logger.exception('INLINE UPDATE ERROR offer_id=%s', offer.id)
    try:
        await application.bot.send_message(
            offer.buyer_id,
            f'🔔 Предложение {offer.id} — {status_label(offer.status)}.\n'
            f'Получатель: {offer.seller_id}\n'
            f'Цена: {price_label(offer)}\n'
            'Это согласие/отказ, не подтверждение оплаты.',
        )
    except TelegramError as exc:
        logger.warning('ADMIN NOTIFICATION FAILED offer_id=%s error=%s', offer.id, type(exc).__name__)


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    if not query:
        return
    if query.from_user.id != ADMIN_ID:
        await query.answer([], is_personal=True, cache_time=0)
        return
    offer_id = (query.query or '').strip()
    if not ID_PATTERN.fullmatch(offer_id):
        await query.answer([], is_personal=True, cache_time=0)
        return
    store: Store = context.application.bot_data['store']
    await store.expire()
    offer = await store.get(offer_id)
    if not offer or offer.buyer_id != ADMIN_ID or offer.status != 'ACTIVE':
        await query.answer([], is_personal=True, cache_time=0)
        return
    await store.save_buyer_profile(offer.id, query.from_user.id,
                                   username=query.from_user.username or '',
                                   first_name=query.from_user.first_name or '')
    me = await context.bot.get_me()
    ids = await store.get_emojis()
    result = InlineQueryResultArticle(
        id=offer.id, title=f'🎁 {item_label(offer)}',
        description=f'{price_label(offer)} · Нажмите, чтобы отправить с кнопками',
        input_message_content=InputTextMessageContent(
            message_text=offer_text(offer, ids),
            parse_mode=ParseMode.HTML,
            link_preview_options=link_preview(offer),
        ),
        reply_markup=offer_keyboard(offer, me.username),
    )
    try:
        await query.answer([result], is_personal=True, cache_time=0)
        logger.info('INLINE OFFER SHOWN offer_id=%s', offer.id)
    except TelegramError:
        logger.exception('INLINE RESULT FAILED offer_id=%s', offer.id)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram sends inline_message_id only if Inline Feedback is enabled."""
    chosen = update.chosen_inline_result
    if not chosen or chosen.from_user.id != ADMIN_ID:
        return
    offer_id = chosen.result_id
    if not ID_PATTERN.fullmatch(offer_id) or not chosen.inline_message_id:
        return
    store: Store = context.application.bot_data['store']
    offer = await store.get(offer_id)
    if not offer or offer.buyer_id != ADMIN_ID:
        return
    if await store.add_inline_message(offer_id, chosen.inline_message_id):
        logger.info('INLINE MESSAGE LINKED offer_id=%s', offer_id)
        # An offer may have changed status before Telegram delivered feedback.
        if offer.status != 'ACTIVE':
            await update_inline_messages(context.application, offer)


async def expiry_worker(application: Application) -> None:
    store: Store = application.bot_data['store']
    try:
        while True:
            await asyncio.sleep(45)
            await store.expire()
    except asyncio.CancelledError:
        raise


async def post_init(application: Application) -> None:
    store: Store = application.bot_data['store']
    await store.open()
    settings = application.bot_data['settings']
    application.bot_data['site'] = await make_site(settings, store, application, notify_buyer)
    application.bot_data['worker'] = asyncio.create_task(expiry_worker(application))
    me = await application.bot.get_me()
    try:
        from telegram import BotCommandScopeDefault, BotCommandScopeAllGroupChats
        await application.bot.set_my_commands([BotCommand('start', 'Начало работы')], scope=BotCommandScopeDefault())
        await application.bot.set_my_commands([BotCommand('start', 'Начало работы')], scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(
            [BotCommand('start', 'Начало работы')],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await application.bot.set_my_commands(
            [BotCommand('offer', 'Создать предложение'),
             BotCommand('myoffers', 'Мои предложения'),
             BotCommand('verify', 'Проверить ключ'),
             BotCommand('cancel', 'Отменить предложение'),
             BotCommand('importemoji', 'Импорт Premium Emoji'),
             BotCommand('setemoji', 'Установить emoji ID'),
             BotCommand('status', 'Статус бота')],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
    except TelegramError as exc:
        logger.warning('BOT COMMAND MENU SETUP FAILED: %s', exc)
    logger.info('BOT STARTED @%s | inline=%s | admin=%s | currency=STARS,TON',
                me.username, me.supports_inline_queries, ADMIN_ID)


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get('worker')
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    site = application.bot_data.get('site')
    if site:
        await site.cleanup()
    await application.bot_data['store'].close()


async def ignore_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error('Telegram error: %s', context.error, exc_info=context.error)


def build_application(settings=None) -> Application:
    settings = settings or load_settings()
    application = (ApplicationBuilder().token(settings.bot_token)
                   .post_init(post_init).post_shutdown(post_shutdown).build())
    application.bot_data.update(settings=settings, store=Store(settings.data_dir / 'giftrelayer_forward.sqlite3'))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('offer', offer_command))
    application.add_handler(CommandHandler('myoffers', myoffers_command))
    application.add_handler(CommandHandler('cancel', cancel_command))
    application.add_handler(CommandHandler('verify', verify_command))
    application.add_handler(CommandHandler('status', status_command))
    application.add_handler(CommandHandler('importemoji', importemoji_command))
    application.add_handler(CommandHandler('setemoji', setemoji_command))
    application.add_handler(CommandHandler('help', status_command))
    application.add_handler(CommandHandler('id', status_command))
    application.add_handler(CallbackQueryHandler(on_decision, pattern=r'^dec:'))
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    application.add_handler(MessageHandler(filters.COMMAND, ignore_unknown_command))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
