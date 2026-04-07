"""Admin MTProxy management — settings, grant days, stats."""
import structlog
from aiogram import Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.system_setting import get_setting_value, upsert_system_setting
from app.database.crud.user import get_user_by_telegram_id
from app.database.models import User
from app.handlers.mtproxy import _api_call, _format_price, _get_mtproxy_settings

logger = structlog.get_logger(__name__)


class MTProxyAdminStates(StatesGroup):
    waiting_price = State()
    waiting_grant_user_id = State()
    waiting_grant_days = State()
    waiting_revoke_user_id = State()


async def admin_mtproxy_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Admin MTProxy dashboard."""
    if not settings.is_admin(db_user.telegram_id):
        await callback.answer('Только для админов', show_alert=True)
        return

    cfg = await _get_mtproxy_settings(db)

    # Get stats
    stats = await _api_call('GET', '/proxy/stats', cfg)
    health = await _api_call('GET', '/health', cfg) if cfg['url'] else None

    text = '🔧 <b>MTProxy — Управление</b>\n\n'

    if health:
        status_emoji = '🟢' if health.get('status') == 'running' else '🔴'
        text += f'{status_emoji} Сервер: {health.get("status", "unknown")}\n'
    else:
        text += '🔴 Сервер: не подключён\n'

    if stats:
        text += f'👥 Пользователей с прокси: {stats.get("users_with_proxy", 0)}\n'
        text += f'🔑 Активных ключей: {stats.get("active_secrets", 0)}\n'
        text += f'📊 Всего выдано: {stats.get("total_secrets", 0)}\n\n'

    text += f'💰 Цена (30 дней): {_format_price(cfg["price_30d"])}\n'
    text += f'📡 Статус: {"✅ Включён" if cfg["enabled"] else "❌ Выключен"}\n'
    text += f'🌐 API: {cfg["url"] or "не настроен"}\n'

    buttons = [
        [types.InlineKeyboardButton(text='💰 Изменить цену', callback_data='admin_mtproxy_price')],
        [
            types.InlineKeyboardButton(
                text='❌ Выключить' if cfg['enabled'] else '✅ Включить',
                callback_data='admin_mtproxy_toggle'
            ),
        ],
        [types.InlineKeyboardButton(text='🎁 Выдать прокси', callback_data='admin_mtproxy_grant')],
        [types.InlineKeyboardButton(text='🚫 Отозвать прокси', callback_data='admin_mtproxy_revoke')],
        [types.InlineKeyboardButton(text='🧹 Очистить истёкшие', callback_data='admin_mtproxy_cleanup')],
        [types.InlineKeyboardButton(text='◀️ Назад', callback_data='admin_panel')],
    ]

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


async def admin_mtproxy_toggle(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    if not settings.is_admin(db_user.telegram_id):
        return
    cfg = await _get_mtproxy_settings(db)
    new_val = 'false' if cfg['enabled'] else 'true'
    await upsert_system_setting(db, 'MTPROXY_ENABLED', new_val)
    await db.commit()
    await callback.answer(f'MTProxy {"включён" if new_val == "true" else "выключен"}', show_alert=True)
    await admin_mtproxy_menu(callback, db_user, db)


async def admin_mtproxy_price_start(callback: types.CallbackQuery, state: FSMContext, db_user: User) -> None:
    if not settings.is_admin(db_user.telegram_id):
        return
    await state.set_state(MTProxyAdminStates.waiting_price)
    await callback.message.edit_text(
        '💰 Введите новую цену за 30 дней <b>в рублях</b> (например: 49):',
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


async def admin_mtproxy_price_set(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession) -> None:
    if not settings.is_admin(db_user.telegram_id):
        return
    try:
        price_rub = float(message.text.strip().replace(',', '.'))
        price_kopeks = int(price_rub * 100)
        if price_kopeks <= 0:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer('❌ Неверный формат. Введите число (например: 49):')
        return

    await upsert_system_setting(db, 'MTPROXY_PRICE_30D', str(price_kopeks))
    await db.commit()
    await state.clear()
    buttons = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text='◀️ Назад в MTProxy', callback_data='admin_mtproxy')],
    ])
    await message.answer(
        f'✅ Цена MTProxy обновлена: {_format_price(price_kopeks)} / 30 дней',
        parse_mode=ParseMode.HTML,
        reply_markup=buttons,
    )


async def admin_mtproxy_grant_start(callback: types.CallbackQuery, state: FSMContext, db_user: User) -> None:
    if not settings.is_admin(db_user.telegram_id):
        return
    await state.set_state(MTProxyAdminStates.waiting_grant_user_id)
    await callback.message.edit_text(
        '🎁 Введите Telegram ID или @username пользователя:',
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


async def admin_mtproxy_grant_user_id(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession) -> None:
    if not settings.is_admin(db_user.telegram_id):
        return
    
    text = message.text.strip().lstrip('@')
    target_user = None
    
    # Try as telegram_id first
    try:
        target_id = int(text)
        target_user = await get_user_by_telegram_id(db, target_id)
    except (ValueError, TypeError):
        pass
    
    # Try as username
    if not target_user:
        from sqlalchemy import select, func
        from app.database.models import User as UserModel
        result = await db.execute(
            select(UserModel).where(func.lower(UserModel.username) == text.lower())
        )
        target_user = result.scalar_one_or_none()
    
    if not target_user:
        await message.answer(f'❌ Пользователь «{text}» не найден.\nВведите Telegram ID или @username:')
        return

    await state.update_data(grant_target_id=target_user.telegram_id, grant_target_name=target_user.username or target_user.first_name)
    await state.set_state(MTProxyAdminStates.waiting_grant_days)
    name = target_user.username or target_user.first_name or str(target_user.telegram_id)
    await message.answer(f'Пользователь: <b>{name}</b> (ID: {target_user.telegram_id})\nВведите количество дней:', parse_mode=ParseMode.HTML)


async def admin_mtproxy_grant_days(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession) -> None:
    if not settings.is_admin(db_user.telegram_id):
        return
    try:
        days = int(message.text.strip())
        if days <= 0 or days > 36500:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer('❌ Введите кол-во дней (1-36500):')
        return

    data = await state.get_data()
    target_id = data['grant_target_id']
    target_name = data.get('grant_target_name', str(target_id))

    cfg = await _get_mtproxy_settings(db)
    result = await _api_call('POST', '/proxy/create', cfg, {
        'user_id': target_id,
        'username': target_name,
        'days': days,
    })

    await state.clear()

    if not result or 'link' not in result:
        await message.answer('❌ Ошибка создания прокси')
        return

    link = result['link']
    expires = result.get('expires_at', '')[:10]
    buttons = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text='🔗 Подключить', url=link)],
        [types.InlineKeyboardButton(text='◀️ Назад в MTProxy', callback_data='admin_mtproxy')],
    ])
    await message.answer(
        f'✅ Прокси выдан!\n\n'
        f'👤 {target_name} (ID: {target_id})\n'
        f'📅 {days} дней (до {expires})\n'
        f'🔗 <code>{link}</code>',
        parse_mode=ParseMode.HTML,
        reply_markup=buttons,
    )


async def admin_mtproxy_revoke_start(callback: types.CallbackQuery, state: FSMContext, db_user: User) -> None:
    if not settings.is_admin(db_user.telegram_id):
        return
    await state.set_state(MTProxyAdminStates.waiting_revoke_user_id)
    await callback.message.edit_text(
        '🚫 Введите Telegram ID или @username для отзыва прокси:',
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


async def admin_mtproxy_revoke_do(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession) -> None:
    if not settings.is_admin(db_user.telegram_id):
        return
    
    text = message.text.strip().lstrip('@')
    target_id = None
    
    try:
        target_id = int(text)
    except (ValueError, TypeError):
        # Try username
        from sqlalchemy import select, func
        from app.database.models import User as UserModel
        result = await db.execute(
            select(UserModel).where(func.lower(UserModel.username) == text.lower())
        )
        found = result.scalar_one_or_none()
        if found:
            target_id = found.telegram_id
    
    if not target_id:
        await message.answer(f'❌ Пользователь «{text}» не найден.\nВведите Telegram ID или @username:')
        return

    cfg = await _get_mtproxy_settings(db)
    result = await _api_call('POST', '/proxy/revoke', cfg, {'user_id': target_id})

    await state.clear()

    buttons = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text='◀️ Назад в MTProxy', callback_data='admin_mtproxy')],
    ])
    if result:
        await message.answer(f'✅ Отозвано прокси: {result.get("revoked", 0)}', reply_markup=buttons)
    else:
        await message.answer('❌ Ошибка отзыва', reply_markup=buttons)


async def admin_mtproxy_cleanup(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    if not settings.is_admin(db_user.telegram_id):
        return
    cfg = await _get_mtproxy_settings(db)
    result = await _api_call('POST', '/proxy/cleanup', cfg)
    if result:
        await callback.answer(f'Очищено: {result.get("cleaned", 0)} истёкших', show_alert=True)
    else:
        await callback.answer('Ошибка очистки', show_alert=True)
    await admin_mtproxy_menu(callback, db_user, db)


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(admin_mtproxy_menu, F.data == 'admin_mtproxy')
    dp.callback_query.register(admin_mtproxy_toggle, F.data == 'admin_mtproxy_toggle')
    dp.callback_query.register(admin_mtproxy_price_start, F.data == 'admin_mtproxy_price')
    dp.callback_query.register(admin_mtproxy_grant_start, F.data == 'admin_mtproxy_grant')
    dp.callback_query.register(admin_mtproxy_revoke_start, F.data == 'admin_mtproxy_revoke')
    dp.callback_query.register(admin_mtproxy_cleanup, F.data == 'admin_mtproxy_cleanup')

    dp.message.register(admin_mtproxy_price_set, MTProxyAdminStates.waiting_price)
    dp.message.register(admin_mtproxy_grant_user_id, MTProxyAdminStates.waiting_grant_user_id)
    dp.message.register(admin_mtproxy_grant_days, MTProxyAdminStates.waiting_grant_days)
    dp.message.register(admin_mtproxy_revoke_do, MTProxyAdminStates.waiting_revoke_user_id)
