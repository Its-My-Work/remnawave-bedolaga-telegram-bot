"""Email binding handler — allows Telegram users to bind email for cabinet access."""

from __future__ import annotations

import random
import string
import re
import asyncio
from datetime import datetime, timedelta, UTC

from aiogram import Dispatcher, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import structlog

from app.database.models import User
from app.config import settings

logger = structlog.get_logger()

# ──────────────────────────── FSM States ────────────────────────────

class EmailBindStates(StatesGroup):
    waiting_for_email = State()
    waiting_for_code = State()


# ──────────────────────────── Helpers ────────────────────────────

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

def _generate_code() -> str:
    return ''.join(random.choices(string.digits, k=6))

def _generate_password(length: int = 12) -> str:
    chars = string.ascii_letters + string.digits + '!@#$%'
    return ''.join(random.choices(chars, k=length))


async def _send_verification_code(to_email: str, code: str, username: str | None = None) -> bool:
    """Send 6-digit code to email using existing email service."""
    try:
        from app.cabinet.services.email_service import EmailService
        email_service = EmailService()
        
        safe_name = username or ''
        subject = 'Код привязки email — YOUR_BRAND'
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #333;">Привязка email к YOUR_BRAND</h2>
            <p>Здравствуйте{', ' + safe_name if safe_name else ''}!</p>
            <p>Вы запросили привязку email в боте YOUR_BRAND. Ваш код подтверждения:</p>
            <div style="background: #f5f5f5; border-radius: 8px; padding: 20px; text-align: center; margin: 20px 0;">
                <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #333;">{code}</span>
            </div>
            <p style="color: #666;">Код действителен 10 минут.</p>
            <p style="color: #999; font-size: 12px;">Если вы не запрашивали привязку, проигнорируйте это письмо.</p>
        </div>
        """
        
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, email_service.send_email, to_email, subject, html_body)
        return result
    except Exception as e:
        logger.error('Failed to send verification code', error=str(e))
        return False


async def _send_credentials_email(to_email: str, password: str, username: str | None = None) -> bool:
    """Send login credentials to email."""
    try:
        from app.cabinet.services.email_service import EmailService
        email_service = EmailService()
        
        cabinet_url = settings.CABINET_URL or 'https://your-cabinet.example.com'
        safe_name = username or ''
        subject = 'Ваши данные для входа — YOUR_BRAND'
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #333;">Добро пожаловать в YOUR_BRAND!</h2>
            <p>Здравствуйте{', ' + safe_name if safe_name else ''}!</p>
            <p>Email успешно привязан к вашему аккаунту. Теперь вы можете войти в личный кабинет через сайт:</p>
            <div style="background: #f5f5f5; border-radius: 8px; padding: 20px; margin: 20px 0;">
                <p><strong>Сайт:</strong> <a href="{cabinet_url}">{cabinet_url}</a></p>
                <p><strong>Email:</strong> {to_email}</p>
                <p><strong>Пароль:</strong> <code style="background: #e0e0e0; padding: 2px 6px; border-radius: 4px;">{password}</code></p>
            </div>
            <p style="color: #666;">Сохраните эти данные. Если Telegram станет недоступен, вы сможете войти через сайт и управлять подпиской.</p>
            <p style="color: #999; font-size: 12px;">Вы можете изменить пароль в личном кабинете.</p>
        </div>
        """
        
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, email_service.send_email, to_email, subject, html_body)
        return result
    except Exception as e:
        logger.error('Failed to send credentials email', error=str(e))
        return False


# ──────────────────────────── Handlers ────────────────────────────

async def start_email_bind(callback: types.CallbackQuery, state: FSMContext, db_user: User, **kwargs) -> None:
    """Start email binding flow."""
    # Check if already has email
    if db_user.email and db_user.email_verified:
        await callback.answer('✅ Email уже привязан!', show_alert=True)
        return
    
    await state.set_state(EmailBindStates.waiting_for_email)
    
    text = (
        '📧 <b>Привязка email</b>\n\n'
        'Email нужен на случай, если Telegram станет недоступен — '
        'вы сможете войти в личный кабинет через сайт и не потеряете подписку.\n\n'
        '📝 Введите ваш email адрес:'
    )
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text='❌ Отмена', callback_data='email_bind_cancel')]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def email_bind_cancel(callback: types.CallbackQuery, state: FSMContext, db_user: User, db=None, **kwargs) -> None:
    """Cancel email binding."""
    await state.clear()
    await callback.answer('Отменено')
    
    # Return to main menu
    from app.handlers.menu import show_main_menu
    if db:
        await show_main_menu(callback, db_user, db)
    else:
        await callback.message.delete()


async def process_email_input(message: types.Message, state: FSMContext, db_user: User, **kwargs) -> None:
    """Process email address input."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.database.crud.user import get_user_by_email
    
    current_state = await state.get_state()
    if current_state != EmailBindStates.waiting_for_email.state:
        return
    
    email = message.text.strip().lower()
    
    # Validate email
    if not EMAIL_REGEX.match(email):
        await message.answer(
            '❌ Неверный формат email. Попробуйте ещё раз:\n\n'
            '📝 Введите корректный email адрес:',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data='email_bind_cancel')]
            ]),
            parse_mode='HTML'
        )
        return
    
    # Check if email already taken
    db: AsyncSession = kwargs.get('db')
    if db:
        existing = await get_user_by_email(db, email)
        if existing and existing.id != db_user.id:
            await message.answer(
                '❌ Этот email уже используется другим аккаунтом.\n\n'
                '📝 Введите другой email:',
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text='❌ Отмена', callback_data='email_bind_cancel')]
                ]),
                parse_mode='HTML'
            )
            return
    
    # Generate and send code
    code = _generate_code()
    username = db_user.first_name or db_user.username or ''
    
    sent = await _send_verification_code(email, code, username)
    if not sent:
        await message.answer(
            '❌ Не удалось отправить код. Проверьте email и попробуйте позже.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 Попробовать снова', callback_data='email_bind_start')],
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data='email_bind_cancel')]
            ])
        )
        return
    
    # Save state
    await state.update_data(
        email=email,
        code=code,
        code_expires=datetime.now(UTC).isoformat(),
        attempts=0
    )
    await state.set_state(EmailBindStates.waiting_for_code)
    
    masked_email = email[:3] + '***' + email[email.index('@'):]
    
    await message.answer(
        f'📨 Код отправлен на <b>{masked_email}</b>\n\n'
        f'Введите 6-значный код из письма:',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='🔄 Отправить заново', callback_data='email_bind_resend')],
            [types.InlineKeyboardButton(text='❌ Отмена', callback_data='email_bind_cancel')]
        ]),
        parse_mode='HTML'
    )


async def email_bind_resend(callback: types.CallbackQuery, state: FSMContext, db_user: User, **kwargs) -> None:
    """Resend verification code."""
    data = await state.get_data()
    email = data.get('email')
    if not email:
        await callback.answer('Ошибка. Начните сначала.')
        await state.clear()
        return
    
    code = _generate_code()
    username = db_user.first_name or db_user.username or ''
    
    sent = await _send_verification_code(email, code, username)
    if not sent:
        await callback.answer('❌ Не удалось отправить. Попробуйте позже.', show_alert=True)
        return
    
    await state.update_data(code=code, code_expires=datetime.now(UTC).isoformat(), attempts=0)
    await callback.answer('✅ Код отправлен заново!')


async def process_code_input(message: types.Message, state: FSMContext, db_user: User, **kwargs) -> None:
    """Process verification code input."""
    from sqlalchemy.ext.asyncio import AsyncSession
    
    current_state = await state.get_state()
    if current_state != EmailBindStates.waiting_for_code.state:
        return
    
    data = await state.get_data()
    email = data.get('email')
    expected_code = data.get('code')
    code_time = data.get('code_expires')
    attempts = data.get('attempts', 0)
    
    if not email or not expected_code:
        await state.clear()
        await message.answer('❌ Сессия истекла. Начните сначала.')
        return
    
    # Check expiry (10 minutes)
    if code_time:
        expires = datetime.fromisoformat(code_time) + timedelta(minutes=10)
        if datetime.now(UTC) > expires:
            await state.clear()
            await message.answer(
                '⏰ Код истёк. Начните сначала.',
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 Начать заново', callback_data='email_bind_start')]
                ])
            )
            return
    
    input_code = message.text.strip()
    
    if input_code != expected_code:
        attempts += 1
        await state.update_data(attempts=attempts)
        
        if attempts >= 5:
            await state.clear()
            await message.answer(
                '❌ Слишком много попыток. Начните сначала.',
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 Начать заново', callback_data='email_bind_start')]
                ])
            )
            return
        
        await message.answer(
            f'❌ Неверный код. Попробуйте ещё ({5 - attempts} попыток осталось):',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text='🔄 Отправить заново', callback_data='email_bind_resend')],
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data='email_bind_cancel')]
            ])
        )
        return
    
    # Code is correct! Bind email
    db: AsyncSession = kwargs.get('db')
    if not db:
        await message.answer('❌ Ошибка базы данных.')
        await state.clear()
        return
    
    password = _generate_password()
    
    try:
        from app.cabinet.auth.password_utils import hash_password
        hashed = hash_password(password)
        
        db_user.email = email
        db_user.email_verified = True
        db_user.email_verified_at = datetime.now(UTC)
        db_user.password_hash = hashed
        
        await db.commit()
        
        logger.info('Email bound via bot', user_id=db_user.id, email=email)
    except Exception as e:
        logger.error('Failed to bind email', error=str(e))
        await db.rollback()
        await message.answer('❌ Ошибка сохранения. Попробуйте позже.')
        await state.clear()
        return
    
    await state.clear()
    
    # Send credentials to email
    username = db_user.first_name or db_user.username or ''
    await _send_credentials_email(email, password, username)
    
    cabinet_url = settings.CABINET_URL or 'https://your-cabinet.example.com'
    
    # Send credentials message and pin it
    cred_msg = await message.answer(
        f'✅ <b>Email успешно привязан!</b>\n\n'
        f'📧 <b>Email:</b> {email}\n'
        f'🔑 <b>Пароль:</b> <code>{password}</code>\n\n'
        f'🌐 <b>Сайт:</b> {cabinet_url}\n\n'
        f'💡 Сохраните эти данные! Если Telegram станет недоступен, '
        f'войдите через сайт по email и паролю.\n\n'
        f'📨 Данные также отправлены на вашу почту.',
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='🏠 Главное меню', callback_data='back_to_menu')]
        ])
    )
    
    # Pin the credentials message
    try:
        await cred_msg.pin(disable_notification=True)
    except Exception as e:
        logger.warning('Could not pin email credentials message', error=str(e))


# ──────────────────────────── Register ────────────────────────────

def register_handlers(dp: Dispatcher) -> None:
    """Register email binding handlers."""
    dp.callback_query.register(start_email_bind, lambda c: c.data == 'email_bind_start')
    dp.callback_query.register(email_bind_cancel, lambda c: c.data == 'email_bind_cancel')
    dp.callback_query.register(email_bind_resend, lambda c: c.data == 'email_bind_resend')
    
    # Message handlers for FSM
    dp.message.register(
        process_email_input,
        EmailBindStates.waiting_for_email,
    )
    dp.message.register(
        process_code_input,
        EmailBindStates.waiting_for_code,
    )
