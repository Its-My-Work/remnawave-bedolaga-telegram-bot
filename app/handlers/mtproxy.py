"""MTProxy handler — purchase, gift, and manage Telegram proxy."""
from datetime import UTC, datetime

import aiohttp
import structlog
from aiogram import Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.system_setting import get_setting_value
from app.database.crud.transaction import create_transaction
from app.database.crud.user import get_user_by_telegram_id, subtract_user_balance
from app.database.models import TransactionType, User
from app.services.admin_notification_service import AdminNotificationService

logger = structlog.get_logger(__name__)

MTPROXY_API_URL_KEY = 'MTPROXY_API_URL'
MTPROXY_API_TOKEN_KEY = 'MTPROXY_API_TOKEN'
MTPROXY_PRICE_30D_KEY = 'MTPROXY_PRICE_30D'
MTPROXY_ENABLED_KEY = 'MTPROXY_ENABLED'
MAX_PROXIES_PER_USER = 10


class MTProxyStates(StatesGroup):
    choosing_quantity = State()
    gift_recipient = State()
    gift_quantity = State()


async def _get_mtproxy_settings(db: AsyncSession) -> dict:
    url = await get_setting_value(db, MTPROXY_API_URL_KEY) or ''
    token = await get_setting_value(db, MTPROXY_API_TOKEN_KEY) or ''
    price_raw = await get_setting_value(db, MTPROXY_PRICE_30D_KEY) or '4900'
    enabled = (await get_setting_value(db, MTPROXY_ENABLED_KEY) or 'true').lower() == 'true'
    
    # ДЛЯ ОТЛАДКИ — УДАЛИ ПОСЛЕ
    print(f"[DEBUG] MTPROXY: url='{url}', token='{token[:10]}...' if token else None, enabled={enabled}")
    
    try:
        price = int(price_raw)
    except ValueError:
        price = 4900
    return {'url': url.rstrip('/'), 'token': token, 'price_30d': price, 'enabled': enabled}


async def _api_call(method: str, path: str, cfg: dict, json_data: dict | None = None) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            headers = {'Authorization': f'Bearer {cfg["token"]}', 'Content-Type': 'application/json'}
            url = f'{cfg["url"]}{path}'
            kw = {'headers': headers, 'timeout': aiohttp.ClientTimeout(total=15)}
            if method == 'GET':
                async with session.get(url, **kw) as resp:
                    return await resp.json() if resp.status == 200 else None
            else:
                async with session.post(url, json=json_data, **kw) as resp:
                    return await resp.json() if resp.status == 200 else None
    except Exception as e:
        logger.error('MTProxy API error', error=str(e), path=path)
    return None


def _format_price(kopeks: int) -> str:
    return f'{kopeks / 100:.0f} ₽' if kopeks % 100 == 0 else f'{kopeks / 100:.2f} ₽'


async def _get_user_active_count(cfg: dict, telegram_id: int) -> int:
    result = await _api_call('GET', f'/proxy/user/{telegram_id}', cfg)
    if result:
        return result.get('count', 0)
    return 0


# ========================= MAIN MENU =========================

async def show_mtproxy_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    cfg = await _get_mtproxy_settings(db)
    if not cfg['enabled'] or not cfg['url']:
        await callback.answer('Telegram Proxy временно недоступен', show_alert=True)
        return

    is_admin = settings.is_admin(db_user.telegram_id)
    result = await _api_call('GET', f'/proxy/user/{db_user.telegram_id}', cfg)
    proxies = result.get('proxies', []) if result else []
    active_proxies = [p for p in proxies if p['active']]

    text = '🛡 <b>Прокси для Telegram</b>\n\n'
    text += '🚀 <b>Что это?</b>\n'
    text += 'Специальный прокси для быстрой работы Telegram —\n'
    text += 'дополнительная скорость и стабильность соединения.\n\n'
    text += '✅ Подключение в 1 клик\n'
    text += '✅ Работает на всех устройствах\n'
    text += '✅ Не нужно устанавливать приложения\n\n'
    text += f'📊 <b>Куплено:</b> {len(proxies)} шт.  |  '
    text += f'<b>Активных:</b> {len(active_proxies)} шт.\n'

    if active_proxies:
        text += '\n'
        for i, p in enumerate(active_proxies, 1):
            exp = p.get('expires_at', '')[:10]
            text += f'  🔑 #{i} — до {exp}\n'
        text += '\n'

    if is_admin:
        text += '👑 <b>Админ:</b> безлимитный доступ\n\n'

    text += f'💰 <b>Цена:</b> {_format_price(cfg["price_30d"])} / 30 дней\n'
    text += f'💵 <b>Баланс:</b> {_format_price(db_user.balance_kopeks)}\n'
    text += f'📱 <b>Лимит:</b> {len(active_proxies)}/{MAX_PROXIES_PER_USER}\n\n'
    text += '━━━━━━━━━━━━━━━━━━━━\n'
    text += '⚠️ <b>ВАЖНО:</b> Прокси работает <b>ТОЛЬКО</b>\n'
    text += 'для приложения Telegram!\n'
    text += 'Это НЕ VPN — браузер и другие\n'
    text += 'приложения не защищены.\n'
    text += '━━━━━━━━━━━━━━━━━━━━'

    can_buy = len(active_proxies) < MAX_PROXIES_PER_USER or is_admin
    buttons = []

    if can_buy:
        buttons.append([types.InlineKeyboardButton(
            text=f'🛒 Купить прокси ({_format_price(cfg["price_30d"])})',
            callback_data='mtproxy_buy_choose'
        )])
        buttons.append([types.InlineKeyboardButton(
            text='🎁 Подарить прокси',
            callback_data='mtproxy_gift_start'
        )])
    else:
        buttons.append([types.InlineKeyboardButton(
            text=f'⛔ Лимит {MAX_PROXIES_PER_USER} прокси достигнут',
            callback_data='mtproxy_limit_reached'
        )])

    if active_proxies:
        buttons.append([types.InlineKeyboardButton(
            text=f'📋 Мои прокси ({len(active_proxies)})', callback_data='mtproxy_my_list'
        )])

    if is_admin and not active_proxies:
        buttons.append([types.InlineKeyboardButton(
            text='👑 Получить (админ)', callback_data='mtproxy_admin_get'
        )])

    buttons.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='back_to_menu')])

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        disable_web_page_preview=True,
    )
    await callback.answer()


async def mtproxy_limit_reached(callback: types.CallbackQuery, **kwargs) -> None:
    await callback.answer(
        f'Лимит {MAX_PROXIES_PER_USER} прокси на аккаунт достигнут.\nУдалите неиспользуемые прокси, чтобы купить новые.',
        show_alert=True
    )


# ========================= BUY (QUANTITY) =========================

async def mtproxy_buy_choose(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    cfg = await _get_mtproxy_settings(db)
    is_admin = settings.is_admin(db_user.telegram_id)
    active_count = await _get_user_active_count(cfg, db_user.telegram_id)
    available = MAX_PROXIES_PER_USER - active_count if not is_admin else 10
    available = max(0, min(available, 10))

    if available <= 0:
        await callback.answer(f'Лимит {MAX_PROXIES_PER_USER} прокси достигнут', show_alert=True)
        return

    price = cfg['price_30d']
    text = '🛒 <b>Покупка прокси</b>\n\n'
    text += f'💰 Цена: {_format_price(price)} за 1 прокси (30 дней)\n'
    text += f'📱 Доступно к покупке: {available} шт.\n'
    text += f'💵 Баланс: {_format_price(db_user.balance_kopeks)}\n\n'
    text += 'Выберите количество:'

    buttons = []
    row = []
    for qty in range(1, available + 1):
        total = price * qty
        label = f'{qty} шт.'
        row.append(types.InlineKeyboardButton(
            text=label,
            callback_data=f'mtproxy_buy:{qty}'
        ))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='menu_mtproxy')])

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


async def mtproxy_cant_afford(callback: types.CallbackQuery, **kwargs) -> None:
    await callback.answer('Недостаточно средств. Пополните баланс.', show_alert=True)


async def mtproxy_buy_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    qty = int(callback.data.split(':')[1])
    cfg = await _get_mtproxy_settings(db)
    price = cfg['price_30d']
    total_balance = price * qty
    total_direct = 5000 * qty  # 50₽ за прокси при прямой оплате
    is_admin = settings.is_admin(db_user.telegram_id)
    can_afford = db_user.balance_kopeks >= total_balance or is_admin

    text = f'🛒 <b>Покупка {qty} прокси</b>\n\n'
    text += f'📦 Количество: {qty} шт.\n'
    if is_admin:
        text += '\n👑 Админ: бесплатно\n'
    text += f'\n⚠️ 1 прокси = 1 устройство\n\n'
    text += '<b>Выберите способ оплаты:</b>\n\n'

    buttons = []

    if can_afford:
        text += f'💰 <b>С баланса:</b> {_format_price(total_balance)} (баланс: {_format_price(db_user.balance_kopeks)})\n'
        buttons.append([types.InlineKeyboardButton(
            text=f'💰 Оплатить с баланса ({_format_price(total_balance)})',
            callback_data=f'mtproxy_buy_do:{qty}'
        )])
    else:
        text += f'💰 С баланса: ❌ недостаточно ({_format_price(db_user.balance_kopeks)} из {_format_price(total_balance)})\n'

    if settings.is_platega_enabled():
        text += f'💳 <b>Напрямую:</b> {_format_price(total_direct)} (СБП / Карта / Крипта)\n'
        buttons.append([types.InlineKeyboardButton(
            text=f'💳 Оплатить напрямую ({_format_price(total_direct)})',
            callback_data=f'mtproxy_qb_qty:{qty}'
        )])

    buttons.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='mtproxy_buy_choose')])

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


async def mtproxy_buy_do(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    qty = int(callback.data.split(':')[1])
    cfg = await _get_mtproxy_settings(db)
    price = cfg['price_30d']
    total = price * qty
    is_admin = settings.is_admin(db_user.telegram_id)

    # Check limit
    active_count = await _get_user_active_count(cfg, db_user.telegram_id)
    if active_count + qty > MAX_PROXIES_PER_USER and not is_admin:
        await callback.answer(f'Превышен лимит. Доступно: {MAX_PROXIES_PER_USER - active_count}', show_alert=True)
        return

    # Deduct balance
    if not is_admin:
        if db_user.balance_kopeks < total:
            await callback.answer('Недостаточно средств', show_alert=True)
            return
        ok = await subtract_user_balance(db, db_user, total, f'Покупка {qty} Telegram Proxy (30 дней)')
        if not ok:
            await callback.answer('Ошибка списания', show_alert=True)
            return
        await create_transaction(
            db, user_id=db_user.id, type=TransactionType.PURCHASE,
            amount_kopeks=-total,
            description=f'Покупка {qty}x Telegram Proxy (30 дней)',
        )

    # Create proxies
    created = []
    for _ in range(qty):
        result = await _api_call('POST', '/proxy/create', cfg, {
            'user_id': db_user.telegram_id,
            'username': db_user.username or db_user.first_name,
            'days': 30 if not is_admin else 36500,
        })
        if result and 'link' in result:
            created.append(result)

    if not created:
        if not is_admin:
            from app.database.crud.user import add_user_balance
            await add_user_balance(db, db_user, total, 'Возврат: ошибка создания прокси')
        await db.commit()
        await callback.answer('Ошибка создания прокси', show_alert=True)
        return

    # Partial refund if some failed
    if len(created) < qty and not is_admin:
        refund = price * (qty - len(created))
        from app.database.crud.user import add_user_balance
        await add_user_balance(db, db_user, refund, f'Частичный возврат: создано {len(created)} из {qty}')

    await db.commit()

    # Notify admin
    try:
        notif = AdminNotificationService(callback.bot)
        await notif.send_mtproxy_purchase_notification(
            user=db_user, qty=len(created), total_kopeks=price * len(created),
        )
    except Exception:
        pass

    text = f'✅ <b>Создано {len(created)} прокси!</b>\n\n'
    text += '📱 <b>Мгновенное подключение:</b>\n'
    text += 'Нажмите «🔗 Подключить» — прокси добавится автоматически!\n\n'
    text += '📋 <b>Инструкция:</b>\n'
    text += '🍏 iPhone: Настройки → Данные и память → Прокси\n'
    text += '🤖 Android: Настройки → Данные и память → MTProto\n'
    text += '💻 ПК: Настройки → Продвинутые → Прокси\n\n'

    buttons = []
    for i, c in enumerate(created, 1):
        exp = c.get('expires_at', '')[:10]
        text += f'🔑 <b>#{i}</b> до {exp}\n<code>{c["link"]}</code>\n\n'
        buttons.append([types.InlineKeyboardButton(text=f'🔗 Подключить #{i}', url=c['link'])])

    text += '⚠️ 1 прокси = 1 устройство.\n💡 Только для Telegram!'

    buttons.append([types.InlineKeyboardButton(text='📋 Мои прокси', callback_data='mtproxy_my_list')])
    buttons.append([types.InlineKeyboardButton(text='◀️ В меню', callback_data='back_to_menu')])

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        disable_web_page_preview=True,
    )
    await callback.answer()


# ========================= GIFT =========================

async def mtproxy_gift_start(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession) -> None:
    cfg = await _get_mtproxy_settings(db)
    if not cfg['enabled']:
        await callback.answer('MTProxy недоступен', show_alert=True)
        return

    await state.set_state(MTProxyStates.gift_recipient)
    await state.update_data(mtproxy_cfg=cfg)

    text = '🎁 <b>Подарить прокси</b>\n\n'
    text += 'Введите Telegram ID или @username получателя:'

    buttons = [[types.InlineKeyboardButton(text='◀️ Отмена', callback_data='menu_mtproxy')]]
    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


async def mtproxy_gift_recipient(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession) -> None:
    text_input = message.text.strip().lstrip('@')

    # Find user
    target = None
    try:
        tid = int(text_input)
        target = await get_user_by_telegram_id(db, tid)
    except (ValueError, TypeError):
        pass

    if not target:
        from sqlalchemy import select, func
        from app.database.models import User as UserModel
        result = await db.execute(
            select(UserModel).where(func.lower(UserModel.username) == text_input.lower())
        )
        target = result.scalar_one_or_none()

    if not target:
        await message.answer(f'❌ Пользователь «{text_input}» не найден.\nВведите Telegram ID или @username:')
        return

    if target.telegram_id == db_user.telegram_id:
        await message.answer('❌ Нельзя подарить себе. Используйте кнопку «Купить».')
        return

    # Check recipient limit
    cfg_data = (await state.get_data()).get('mtproxy_cfg', {})
    cfg = await _get_mtproxy_settings(db)
    active = await _get_user_active_count(cfg, target.telegram_id)
    available = MAX_PROXIES_PER_USER - active

    if available <= 0:
        await message.answer(f'❌ У получателя уже {MAX_PROXIES_PER_USER} прокси (лимит).')
        await state.clear()
        return

    name = target.username or target.first_name or str(target.telegram_id)
    await state.update_data(gift_target_id=target.telegram_id, gift_target_name=name, gift_available=available)
    await state.set_state(MTProxyStates.gift_quantity)

    price = cfg['price_30d']
    text = f'🎁 Получатель: <b>{name}</b>\n'
    text += f'📱 Доступно: {available} шт.\n'
    text += f'💰 Цена: {_format_price(price)} / шт.\n\n'
    text += f'Сколько прокси подарить? (1-{min(available, 10)})'
    await message.answer(text, parse_mode=ParseMode.HTML)


async def mtproxy_gift_quantity(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession) -> None:
    data = await state.get_data()
    available = data.get('gift_available', 1)

    try:
        qty = int(message.text.strip())
        if qty < 1 or qty > min(available, 10):
            raise ValueError
    except (ValueError, TypeError):
        await message.answer(f'❌ Введите число от 1 до {min(available, 10)}:')
        return

    cfg = await _get_mtproxy_settings(db)
    price = cfg['price_30d']
    total = price * qty
    is_admin = settings.is_admin(db_user.telegram_id)
    target_id = data['gift_target_id']
    target_name = data['gift_target_name']

    # Deduct balance
    if not is_admin:
        if db_user.balance_kopeks < total:
            await message.answer(f'❌ Недостаточно средств. Нужно {_format_price(total)}, у вас {_format_price(db_user.balance_kopeks)}')
            await state.clear()
            return
        ok = await subtract_user_balance(db, db_user, total, f'Подарок {qty}x Proxy для {target_name}')
        if not ok:
            await message.answer('❌ Ошибка списания')
            await state.clear()
            return
        await create_transaction(
            db, user_id=db_user.id, type=TransactionType.PURCHASE,
            amount_kopeks=-total,
            description=f'Подарок {qty}x Telegram Proxy для {target_name}',
        )

    # Create proxies for recipient
    created = []
    for _ in range(qty):
        result = await _api_call('POST', '/proxy/create', cfg, {
            'user_id': target_id,
            'username': target_name,
            'days': 30,
        })
        if result and 'link' in result:
            created.append(result)

    if not created:
        if not is_admin:
            from app.database.crud.user import add_user_balance
            await add_user_balance(db, db_user, total, 'Возврат: ошибка подарка прокси')
        await db.commit()
        await message.answer('❌ Ошибка создания прокси')
        await state.clear()
        return

    if len(created) < qty and not is_admin:
        refund = price * (qty - len(created))
        from app.database.crud.user import add_user_balance
        await add_user_balance(db, db_user, refund, f'Частичный возврат подарка')

    await db.commit()
    await state.clear()

    # Notify admin
    try:
        notif = AdminNotificationService(callback.bot if hasattr(callback, 'bot') else message.bot)
        await notif.send_mtproxy_purchase_notification(
            user=db_user, qty=len(created), total_kopeks=price * len(created),
            is_gift=True, gift_recipient_name=target_name, gift_recipient_id=target_id,
        )
    except Exception:
        pass

    text = f'🎁 <b>Подарок отправлен!</b>\n\n'
    text += f'👤 Получатель: {target_name}\n'
    text += f'📦 Создано: {len(created)} прокси\n'
    text += f'💰 Потрачено: {_format_price(price * len(created))}\n'
    await message.answer(text, parse_mode=ParseMode.HTML)

    # Notify recipient
    try:
        bot = message.bot
        notify_text = f'🎁 <b>Вам подарили {len(created)} прокси!</b>\n\n'
        notify_text += f'От: {db_user.username or db_user.first_name}\n\n'
        notify_buttons = []
        for i, c in enumerate(created, 1):
            notify_text += f'🔑 #{i}: <code>{c["link"]}</code>\n'
            notify_buttons.append([types.InlineKeyboardButton(text=f'🔗 Подключить #{i}', url=c['link'])])
        notify_buttons.append([types.InlineKeyboardButton(text='📋 Мои прокси', callback_data='mtproxy_my_list')])
        await bot.send_message(
            target_id, notify_text, parse_mode=ParseMode.HTML,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=notify_buttons),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ========================= MY LIST =========================

async def mtproxy_my_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    cfg = await _get_mtproxy_settings(db)
    result = await _api_call('GET', f'/proxy/user/{db_user.telegram_id}', cfg)

    if not result or not result.get('proxies'):
        await callback.answer('У вас нет прокси', show_alert=True)
        return

    proxies = result['proxies']
    active = [p for p in proxies if p['active']]
    expired = [p for p in proxies if not p['active']]

    text = f'📋 <b>Мои прокси</b> ({len(active)} активных)\n\n'

    buttons = []
    for i, p in enumerate(active, 1):
        exp = p.get('expires_at', '')[:10]
        secret_short = p['secret'][:8]
        text += f'🔑 <b>#{i}</b> (…{secret_short})\n'
        text += f'  📅 До: {exp}\n'
        text += f'  🔗 <code>{p["link"]}</code>\n\n'
        buttons.append([
            types.InlineKeyboardButton(text=f'🔗 Подключить #{i}', url=p['link']),
            types.InlineKeyboardButton(text=f'🗑 Удалить #{i}', callback_data=f'mtproxy_delete:{p["secret"][:16]}'),
        ])

    if expired:
        text += f'<i>Истёкших: {len(expired)}</i>\n\n'

    text += '💡 1 прокси = 1 устройство.\n'
    text += '🗑 При удалении — возврат за неиспользованные дни.\n'

    buttons.append([types.InlineKeyboardButton(text='🛒 Купить ещё', callback_data='mtproxy_buy_choose')])
    buttons.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='menu_mtproxy')])

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        disable_web_page_preview=True,
    )
    await callback.answer()


# ========================= DELETE =========================

async def mtproxy_delete_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    secret_prefix = callback.data.split(':')[1]
    cfg = await _get_mtproxy_settings(db)
    result = await _api_call('GET', f'/proxy/user/{db_user.telegram_id}', cfg)
    if not result:
        await callback.answer('Ошибка', show_alert=True)
        return
    target = None
    for p in result.get('proxies', []):
        if p['secret'].startswith(secret_prefix) and p['active']:
            target = p
            break
    if not target:
        await callback.answer('Прокси не найден', show_alert=True)
        return

    from datetime import datetime as dt, timezone
    now = dt.now(timezone.utc)
    expires = dt.fromisoformat(target['expires_at'])
    created = dt.fromisoformat(target.get('created_at', target['expires_at']))
    total_days = max(1, (expires - created).days)
    remaining = max(0, (expires - now).days)
    price = cfg['price_30d']
    refund = int(price * remaining / total_days) if total_days > 0 else 0

    text = f'🗑 <b>Удалить прокси?</b>\n\n'
    text += f'📅 Осталось: {remaining} из {total_days} дней\n'
    text += f'💰 Возврат: {_format_price(refund)}\n\n'
    text += '⚠️ Прокси перестанет работать сразу.'

    buttons = [
        [types.InlineKeyboardButton(text=f'🗑 Удалить ({_format_price(refund)} возврат)', callback_data=f'mtproxy_delete_do:{target["secret"]}')],
        [types.InlineKeyboardButton(text='◀️ Отмена', callback_data='mtproxy_my_list')],
    ]
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


async def mtproxy_delete_do(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    secret = callback.data.split(':')[1]
    cfg = await _get_mtproxy_settings(db)
    is_admin = settings.is_admin(db_user.telegram_id)
    result = await _api_call('POST', '/proxy/delete', cfg, {'user_id': db_user.telegram_id, 'secret': secret})
    if not result or not result.get('deleted'):
        await callback.answer('Ошибка удаления', show_alert=True)
        return
    remaining = result.get('remaining_days', 0)
    total = result.get('total_days', 30)
    price = cfg['price_30d']
    refund = int(price * remaining / total) if total > 0 and not is_admin else 0
    if refund > 0:
        from app.database.crud.user import add_user_balance
        await add_user_balance(db, db_user, refund, f'Возврат за MTProxy ({remaining} дн.)')
        await create_transaction(db, user=db_user, amount_kopeks=refund,
            description=f'Возврат за удалённый Proxy ({remaining} из {total} дн.)', transaction_type=TransactionType.REFUND)
        await db.commit()
    text = f'✅ <b>Прокси удалён</b>\n\n'
    if refund > 0:
        text += f'💰 Возвращено: {_format_price(refund)} ({remaining} дн.)\n'
    buttons = [
        [types.InlineKeyboardButton(text='📋 Мои прокси', callback_data='mtproxy_my_list')],
        [types.InlineKeyboardButton(text='◀️ В меню', callback_data='back_to_menu')],
    ]
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# ========================= ADMIN GET =========================

async def mtproxy_admin_get(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    if not settings.is_admin(db_user.telegram_id):
        await callback.answer('Только для админов', show_alert=True)
        return
    cfg = await _get_mtproxy_settings(db)
    result = await _api_call('POST', '/proxy/create', cfg, {
        'user_id': db_user.telegram_id, 'username': db_user.username or 'admin', 'days': 36500,
    })
    if not result or 'link' not in result:
        await callback.answer('Ошибка', show_alert=True)
        return
    link = result['link']
    text = f'👑 <b>Админ-прокси создан!</b>\n\n🔗 <code>{link}</code>\n\n📅 ~100 лет'
    buttons = [
        [types.InlineKeyboardButton(text='🔗 Подключить', url=link)],
        [types.InlineKeyboardButton(text='◀️ Назад', callback_data='menu_mtproxy')],
    ]
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons), disable_web_page_preview=True)
    await callback.answer()


# ========================= EXPIRY NOTIFICATIONS =========================

async def check_mtproxy_expiry(bot, db_session_factory) -> None:
    import asyncio
    from datetime import datetime as dt, timezone
    while True:
        try:
            async with db_session_factory() as db:
                cfg = await _get_mtproxy_settings(db)
                if not cfg['url']:
                    await asyncio.sleep(21600)
                    continue
                data_resp = await _api_call('GET', '/proxy/all_users', cfg)
                if not data_resp:
                    await asyncio.sleep(21600)
                    continue
                now = dt.now(timezone.utc)
                for user_info in data_resp.get('users', []):
                    user_tg_id = user_info.get('telegram_id')
                    if not user_tg_id:
                        continue
                    for proxy in user_info.get('proxies', []):
                        if not proxy.get('active'):
                            continue
                        try:
                            expires = dt.fromisoformat(proxy['expires_at'])
                            days_left = (expires - now).days
                            if days_left in [3, 1, 0]:
                                if days_left == 0:
                                    txt = '⚠️ <b>Ваш Telegram Proxy истекает сегодня!</b>'
                                elif days_left == 1:
                                    txt = '⏰ <b>Ваш Telegram Proxy истекает завтра!</b>'
                                else:
                                    txt = f'📅 <b>Ваш Telegram Proxy истекает через {days_left} дня</b>'
                                txt += '\n\nПродлите подписку, чтобы сохранить доступ.'
                                kb = types.InlineKeyboardMarkup(inline_keyboard=[
                                    [types.InlineKeyboardButton(text='🛒 Купить новый', callback_data='mtproxy_buy_choose')],
                                    [types.InlineKeyboardButton(text='📋 Мои прокси', callback_data='mtproxy_my_list')],
                                ])
                                try:
                                    await bot.send_message(user_tg_id, txt, parse_mode=ParseMode.HTML, reply_markup=kb)
                                except Exception:
                                    pass
                        except Exception:
                            continue
            # Auto-cleanup expired proxies
            try:
                cleanup_result = await _api_call('POST', '/proxy/cleanup', cfg)
                if cleanup_result and cleanup_result.get('cleaned', 0) > 0:
                    logger.info('MTProxy auto-cleanup', cleaned=cleanup_result['cleaned'])
            except Exception:
                pass

        except Exception as e:
            logger.error('MTProxy expiry check error', error=str(e))
        await asyncio.sleep(21600)





# ========================= QUICK BUY VIA PLATEGA =========================

async def mtproxy_quick_buy_start(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Выбор количества для быстрой покупки прокси через Platega."""
    if not settings.is_platega_enabled():
        await callback.answer('❌ Оплата через Platega временно недоступна', show_alert=True)
        return

    cfg = await _get_mtproxy_settings(db)
    if not cfg['enabled'] or not cfg['url']:
        await callback.answer('MTProxy временно недоступен', show_alert=True)
        return

    is_admin = settings.is_admin(db_user.telegram_id)
    active_count = await _get_user_active_count(cfg, db_user.telegram_id)
    available = MAX_PROXIES_PER_USER - active_count if not is_admin else 10
    available = max(0, min(available, 5))

    if available <= 0:
        await callback.answer(f'Лимит {MAX_PROXIES_PER_USER} прокси достигнут', show_alert=True)
        return

    price_per_proxy = 5000  # 50 рублей в копейках
    text = (
        '🚀 <b>Быстрая покупка прокси</b>\n\n'
        '💳 Оплата через Platega (без пополнения баланса)\n'
        f'💰 Цена: {_format_price(price_per_proxy)} за 1 прокси (30 дней)\n'
        f'📱 Доступно к покупке: {available} шт.\n\n'
        'Выберите количество:'
    )

    buttons = []
    row = []
    for qty in range(1, available + 1):
        total = price_per_proxy * qty
        row.append(types.InlineKeyboardButton(
            text=f'{qty} шт. — {_format_price(total)}',
            callback_data=f'mtproxy_qb_qty:{qty}'
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='menu_mtproxy')])

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


def _get_active_platega_methods() -> list[int]:
    """Получить активные методы оплаты Platega."""
    methods = settings.get_platega_active_methods()
    return [code for code in methods if code in {2, 11, 12, 13}]


async def mtproxy_quick_buy_method(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Выбор способа оплаты Platega для быстрой покупки."""
    qty = int(callback.data.split(':')[1])
    price_per_proxy = 5000
    total = qty * price_per_proxy

    active_methods = _get_active_platega_methods()
    if not active_methods:
        await callback.answer('⚠️ Нет доступных методов оплаты Platega', show_alert=True)
        return

    # Если только 1 метод — сразу переходим к оплате
    if len(active_methods) == 1:
        callback.data = f'mtproxy_qb_pay:{qty}:{active_methods[0]}'
        await mtproxy_quick_buy_pay(callback, db_user, db)
        return

    text = (
        f'🚀 <b>Быстрая покупка: {qty} прокси</b>\n\n'
        f'💰 Итого: {_format_price(total)}\n\n'
        'Выберите способ оплаты:'
    )

    buttons = []
    for method_code in active_methods:
        label = settings.get_platega_method_display_title(method_code)
        buttons.append([types.InlineKeyboardButton(
            text=label,
            callback_data=f'mtproxy_qb_pay:{qty}:{method_code}'
        )])

    buttons.append([types.InlineKeyboardButton(text='◀️ Назад', callback_data='mtproxy_quick_buy')])

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


async def mtproxy_quick_buy_pay(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Создание платежа Platega для быстрой покупки прокси."""
    parts = callback.data.split(':')
    qty = int(parts[1])
    method_code = int(parts[2])
    price_per_proxy = 5000
    total_kopeks = qty * price_per_proxy

    # Проверяем лимит ещё раз
    cfg = await _get_mtproxy_settings(db)
    is_admin = settings.is_admin(db_user.telegram_id)
    active_count = await _get_user_active_count(cfg, db_user.telegram_id)
    if active_count + qty > MAX_PROXIES_PER_USER and not is_admin:
        await callback.answer(f'Превышен лимит. Доступно: {MAX_PROXIES_PER_USER - active_count}', show_alert=True)
        return

    try:
        from app.services.payment_service import PaymentService
        payment_service = PaymentService(callback.bot)
        payment_result = await payment_service.create_platega_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=total_kopeks,
            description=f'Покупка {qty} MTProxy (30 дней)',
            language=db_user.language or 'ru',
            payment_method_code=method_code,
            min_amount_override=5000,  # 50₽ минимум для прокси
            metadata_extra={
                'for_product': 'mtproxy',
                'proxy_quantity': qty,
            },
        )
    except Exception as e:
        logger.exception('Ошибка создания Platega платежа для MTProxy', error=e)
        payment_result = None

    if not payment_result or not payment_result.get('redirect_url'):
        await callback.answer('❌ Ошибка создания платежа. Попробуйте позже.', show_alert=True)
        return

    redirect_url = payment_result['redirect_url']
    local_payment_id = payment_result.get('local_payment_id')
    method_title = settings.get_platega_method_display_title(method_code)

    text = (
        '🚀 <b>Оплата MTProxy</b>\n\n'
        f'📦 Количество: {qty} шт.\n'
        f'💰 Сумма: {_format_price(total_kopeks)}\n'
        f'💳 Способ: {method_title}\n\n'
        '📱 Нажмите «Оплатить» и следуйте инструкциям.\n'
        'После оплаты прокси создадутся автоматически!'
    )

    buttons = [
        [types.InlineKeyboardButton(
            text=f'💳 Оплатить {_format_price(total_kopeks)}',
            url=redirect_url
        )],
        [types.InlineKeyboardButton(
            text='📊 Проверить статус',
            callback_data=f'check_platega_{local_payment_id}'
        )],
        [types.InlineKeyboardButton(text='◀️ Назад', callback_data='menu_mtproxy')],
    ]

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_mtproxy_menu, F.data == 'menu_mtproxy')
    dp.callback_query.register(mtproxy_buy_choose, F.data == 'mtproxy_buy_choose')
    dp.callback_query.register(mtproxy_cant_afford, F.data == 'mtproxy_cant_afford')
    dp.callback_query.register(mtproxy_buy_confirm, F.data.startswith('mtproxy_buy:'))
    dp.callback_query.register(mtproxy_buy_do, F.data.startswith('mtproxy_buy_do:'))
    dp.callback_query.register(mtproxy_gift_start, F.data == 'mtproxy_gift_start')
    dp.callback_query.register(mtproxy_my_list, F.data == 'mtproxy_my_list')
    dp.callback_query.register(mtproxy_admin_get, F.data == 'mtproxy_admin_get')
    dp.callback_query.register(mtproxy_limit_reached, F.data == 'mtproxy_limit_reached')
    dp.callback_query.register(mtproxy_delete_confirm, F.data.startswith('mtproxy_delete:'))
    dp.callback_query.register(mtproxy_delete_do, F.data.startswith('mtproxy_delete_do:'))
    # Quick buy via Platega
    dp.callback_query.register(mtproxy_quick_buy_start, F.data == 'mtproxy_quick_buy')
    dp.callback_query.register(mtproxy_quick_buy_method, F.data.startswith('mtproxy_qb_qty:'))
    dp.callback_query.register(mtproxy_quick_buy_pay, F.data.startswith('mtproxy_qb_pay:'))
    # FSM handlers for gift
    dp.message.register(mtproxy_gift_recipient, MTProxyStates.gift_recipient)
    dp.message.register(mtproxy_gift_quantity, MTProxyStates.gift_quantity)
