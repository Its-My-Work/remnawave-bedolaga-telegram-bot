"""Cabinet API routes for MTProxy — user proxy management."""
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.config import settings
from app.database.crud.system_setting import get_setting_value
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import TransactionType, User

logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/mtproxy', tags=['mtproxy'])


# --- Shared helpers (same as bot handler) ---
import aiohttp

async def _get_cfg(db: AsyncSession) -> dict:
    url = await get_setting_value(db, 'MTPROXY_API_URL') or ''
    token = await get_setting_value(db, 'MTPROXY_API_TOKEN') or ''
    price_raw = await get_setting_value(db, 'MTPROXY_PRICE_30D') or '4900'
    enabled = (await get_setting_value(db, 'MTPROXY_ENABLED') or 'true').lower() == 'true'
    try:
        price = int(price_raw)
    except ValueError:
        price = 4900
    return {'url': url.rstrip('/'), 'token': token, 'price_30d': price, 'enabled': enabled}


async def _api(method: str, path: str, cfg: dict, json_data: dict | None = None) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            headers = {'Authorization': f'Bearer {cfg["token"]}', 'Content-Type': 'application/json'}
            url = f'{cfg["url"]}{path}'
            kw = {'headers': headers, 'timeout': aiohttp.ClientTimeout(total=10)}
            if method == 'GET':
                async with session.get(url, **kw) as resp:
                    return await resp.json() if resp.status == 200 else None
            else:
                async with session.post(url, json=json_data, **kw) as resp:
                    return await resp.json() if resp.status == 200 else None
    except Exception as e:
        logger.error('MTProxy API error', error=str(e))
    return None


# --- Response models ---
class ProxyItem(BaseModel):
    secret: str
    link: str
    active: bool
    created_at: str | None = None
    expires_at: str | None = None
    days: int | None = None

class MTProxyStatusResponse(BaseModel):
    enabled: bool
    price_30d: int
    proxies: list[ProxyItem]
    active_count: int
    total_count: int
    is_admin: bool

class PurchaseResponse(BaseModel):
    success: bool
    link: str | None = None
    expires_at: str | None = None
    error: str | None = None

class DeleteResponse(BaseModel):
    success: bool
    refund_kopeks: int = 0
    remaining_days: int = 0
    total_days: int = 0


# --- Endpoints ---

@router.get('/status', response_model=MTProxyStatusResponse)
async def get_mtproxy_status(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    cfg = await _get_cfg(db)
    is_admin = settings.is_admin(user.telegram_id)
    proxies = []
    active_count = 0

    if cfg['enabled'] and cfg['url']:
        result = await _api('GET', f'/proxy/user/{user.telegram_id}', cfg)
        if result:
            for p in result.get('proxies', []):
                proxies.append(ProxyItem(**p))
            active_count = result.get('count', 0)

    return MTProxyStatusResponse(
        enabled=cfg['enabled'],
        price_30d=cfg['price_30d'],
        proxies=proxies,
        active_count=active_count,
        total_count=len(proxies),
        is_admin=is_admin,
    )


@router.post('/purchase', response_model=PurchaseResponse)
async def purchase_mtproxy(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    cfg = await _get_cfg(db)
    if not cfg['enabled']:
        raise HTTPException(400, 'MTProxy disabled')

    is_admin = settings.is_admin(user.telegram_id)
    price = cfg['price_30d']

    if not is_admin:
        if user.balance_kopeks < price:
            raise HTTPException(400, f'Insufficient balance: need {price}, have {user.balance_kopeks}')

        ok = await subtract_user_balance(db, user, price, 'Покупка Telegram Proxy (30 дней)')
        if not ok:
            raise HTTPException(400, 'Balance deduction failed')
        await create_transaction(
            db, user=user, amount_kopeks=-price,
            description='Покупка Telegram Proxy (30 дней)',
            transaction_type=TransactionType.PURCHASE,
        )

    result = await _api('POST', '/proxy/create', cfg, {
        'user_id': user.telegram_id,
        'username': user.username or user.first_name,
        'days': 30 if not is_admin else 36500,
    })

    if not result or 'link' not in result:
        if not is_admin:
            from app.database.crud.user import add_user_balance
            await add_user_balance(db, user, price, 'Возврат: ошибка создания прокси')
        await db.commit()
        return PurchaseResponse(success=False, error='Failed to create proxy')

    await db.commit()
    return PurchaseResponse(
        success=True,
        link=result['link'],
        expires_at=result.get('expires_at'),
    )


@router.post('/delete/{secret}', response_model=DeleteResponse)
async def delete_mtproxy(
    secret: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    cfg = await _get_cfg(db)
    is_admin = settings.is_admin(user.telegram_id)

    result = await _api('POST', '/proxy/delete', cfg, {
        'user_id': user.telegram_id,
        'secret': secret,
    })

    if not result or not result.get('deleted'):
        raise HTTPException(400, 'Failed to delete proxy')

    remaining = result.get('remaining_days', 0)
    total = result.get('total_days', 30)
    price = cfg['price_30d']
    refund = int(price * remaining / total) if total > 0 and not is_admin else 0

    if refund > 0:
        from app.database.crud.user import add_user_balance
        await add_user_balance(db, user, refund, f'Возврат за MTProxy ({remaining} дн.)')
        await create_transaction(
            db, user=user, amount_kopeks=refund,
            description=f'Возврат за удалённый Telegram Proxy ({remaining} дн. из {total})',
            transaction_type=TransactionType.REFUND,
        )
        await db.commit()

    return DeleteResponse(
        success=True,
        refund_kopeks=refund,
        remaining_days=remaining,
        total_days=total,
    )


# --- Gift proxy ---

class GiftProxyRequest(BaseModel):
    recipient_username: str
    quantity: int = 1
    message: str | None = None

class GiftProxyResponse(BaseModel):
    success: bool
    created: int = 0
    total_cost: int = 0
    recipient_name: str | None = None
    error: str | None = None


@router.post('/gift', response_model=GiftProxyResponse)
async def gift_proxy(
    body: GiftProxyRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Gift proxy to another user. Refund goes to buyer on delete."""
    cfg = await _get_cfg(db)
    if not cfg['enabled']:
        raise HTTPException(400, 'MTProxy disabled')

    qty = max(1, min(body.quantity, 10))
    price = cfg['price_30d']
    total = price * qty
    is_admin = settings.is_admin(user.telegram_id)

    # Find recipient
    recipient_input = body.recipient_username.strip().lstrip('@')
    target = None

    try:
        tid = int(recipient_input)
        target = await db.execute(
            __import__('sqlalchemy').select(User).where(User.telegram_id == tid)
        )
        target = target.scalar_one_or_none()
    except (ValueError, TypeError):
        pass

    if not target:
        from sqlalchemy import select, func
        result = await db.execute(
            select(User).where(func.lower(User.username) == recipient_input.lower())
        )
        target = result.scalar_one_or_none()

    if not target:
        return GiftProxyResponse(success=False, error=f'Пользователь "{recipient_input}" не найден')

    if target.telegram_id == user.telegram_id:
        return GiftProxyResponse(success=False, error='Нельзя подарить себе')

    # Check recipient limit (10 max)
    import aiohttp
    active_count_resp = await _api('GET', f'/proxy/user/{target.telegram_id}', cfg)
    current_active = active_count_resp.get('count', 0) if active_count_resp else 0
    if current_active + qty > 10:
        avail = 10 - current_active
        return GiftProxyResponse(success=False, error=f'У получателя лимит. Доступно: {avail}')

    # Deduct buyer balance
    if not is_admin:
        if user.balance_kopeks < total:
            return GiftProxyResponse(success=False, error=f'Недостаточно средств')
        ok = await subtract_user_balance(db, user, total, f'Подарок {qty}x Proxy для {target.username or target.first_name}')
        if not ok:
            return GiftProxyResponse(success=False, error='Ошибка списания')
        await create_transaction(
            db, user=user, amount_kopeks=-total,
            description=f'Подарок {qty}x Telegram Proxy для {target.username or target.first_name}',
            transaction_type=TransactionType.PURCHASE,
        )

    # Create proxies
    created = []
    for _ in range(qty):
        result = await _api('POST', '/proxy/create', cfg, {
            'user_id': target.telegram_id,
            'username': target.username or target.first_name,
            'days': 30,
            'buyer_id': user.telegram_id,
        })
        if result and 'link' in result:
            created.append(result)

    if not created and not is_admin:
        from app.database.crud.user import add_user_balance
        await add_user_balance(db, user, total, 'Возврат: ошибка подарка прокси')

    # Partial refund
    if len(created) < qty and not is_admin:
        refund = price * (qty - len(created))
        from app.database.crud.user import add_user_balance
        await add_user_balance(db, user, refund, 'Частичный возврат подарка прокси')

    await db.commit()

    target_name = target.username or target.first_name or str(target.telegram_id)
    return GiftProxyResponse(
        success=True,
        created=len(created),
        total_cost=price * len(created),
        recipient_name=target_name,
    )



# ===== TEMPORARY PROXY KEY (public, no auth, for login page) =====
import time as _time
from fastapi import Request as _Request

# Anti-abuse: per-IP rate limit + global daily limit
_temp_key_requests: dict[str, float] = {}  # ip -> last_request_time
_temp_key_daily_count: int = 0
_temp_key_daily_reset: float = _time.time()
_TEMP_KEY_COOLDOWN = 300  # 5 min per IP
_TEMP_KEY_DAILY_LIMIT = 200  # max 200 temp keys per day globally
_TEMP_KEY_DURATION_DAYS = 1  # MTProxy API needs at least 1 day


@router.post('/temp-key')
async def get_temp_proxy_key(request: _Request):
    """Get a temporary MTProxy key for 30 minutes (no auth required).
    Anti-abuse: 1 per 30min per IP, max 50/day globally.
    Key auto-deletes after 30 minutes.
    """
    import httpx
    global _temp_key_daily_count, _temp_key_daily_reset

    client_ip = request.headers.get('x-real-ip') or request.headers.get('x-forwarded-for', '').split(',')[0].strip() or (request.client.host if request.client else 'unknown')
    now = _time.time()

    # Reset daily counter
    if now - _temp_key_daily_reset > 86400:
        _temp_key_daily_count = 0
        _temp_key_daily_reset = now

    # Global daily limit
    if _temp_key_daily_count >= _TEMP_KEY_DAILY_LIMIT:
        raise HTTPException(429, 'Лимит временных ключей исчерпан. Попробуйте завтра.')

    # Per-IP rate limit
    last_req = _temp_key_requests.get(client_ip, 0)
    if now - last_req < _TEMP_KEY_COOLDOWN:
        remaining = int(_TEMP_KEY_COOLDOWN - (now - last_req))
        mins = remaining // 60
        raise HTTPException(429, f'Подождите {mins} мин. перед повторным запросом')

    # Get MTProxy settings from DB
    from app.database.database import AsyncSessionLocal
    from app.database.crud.system_setting import get_setting_value

    async with AsyncSessionLocal() as db:
        api_url = await get_setting_value(db, 'MTPROXY_API_URL') or ''
        api_token = await get_setting_value(db, 'MTPROXY_API_TOKEN') or ''

    if not api_url or not api_token:
        raise HTTPException(503, 'MTProxy не настроен')

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f'{api_url}/proxy/create',
                json={'user_id': 0, 'username': f'temp-{client_ip[:15]}', 'days': _TEMP_KEY_DURATION_DAYS},
                headers={'Authorization': f'Bearer {api_token}'}
            )
            if resp.status_code != 200:
                raise HTTPException(502, 'Ошибка сервера прокси')
            data = resp.json()

        # Schedule cleanup after 30 minutes
        import asyncio

        async def _cleanup():
            await asyncio.sleep(1800)
            try:
                secret = data.get('secret', '')
                if secret:
                    async with httpx.AsyncClient(timeout=30.0) as c:
                        await c.post(
                            f'{api_url}/proxy/delete',
                            json={'user_id': 0, 'secret': secret},
                            headers={'Authorization': f'Bearer {api_token}'}
                        )
            except Exception:
                pass
            _temp_key_requests.pop(client_ip, None)

        asyncio.create_task(_cleanup())

        _temp_key_requests[client_ip] = now
        _temp_key_daily_count += 1

        return {
            'link': data.get('link', ''),
            'duration_minutes': 30,
        }
    except httpx.HTTPError:
        raise HTTPException(502, 'Не удалось подключиться к серверу прокси')


# --- Direct Platega purchase ---

class DirectPurchaseRequest(BaseModel):
    quantity: int = 1
    payment_method_code: int = 2  # 2=СБП, 10/11=Карты, 13=Крипта

class DirectPurchaseResponse(BaseModel):
    success: bool
    redirect_url: str | None = None
    transaction_id: str | None = None
    total_kopeks: int = 0
    error: str | None = None


@router.post('/purchase-direct', response_model=DirectPurchaseResponse)
async def purchase_mtproxy_direct(
    body: DirectPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Прямая покупка прокси через Platega без пополнения баланса."""
    cfg = await _get_cfg(db)
    if not cfg['enabled']:
        raise HTTPException(400, 'MTProxy disabled')
    if not settings.is_platega_enabled():
        raise HTTPException(400, 'Platega not configured')

    qty = max(1, min(body.quantity, 10))
    PRICE_PER_PROXY = 5000  # 50₽
    total_kopeks = PRICE_PER_PROXY * qty

    # Проверка лимита прокси
    is_admin = settings.is_admin(user.telegram_id)
    if not is_admin:
        result = await _api('GET', f'/proxy/user/{user.telegram_id}', cfg)
        active_count = len([p for p in (result or {}).get('proxies', []) if p.get('active')]) if result else 0
        if active_count + qty > 10:
            raise HTTPException(400, f'Proxy limit exceeded: have {active_count}, want +{qty}, max 10')

    # Проверка метода оплаты
    allowed_methods = [2, 11, 13]
    if body.payment_method_code not in allowed_methods:
        raise HTTPException(400, f'Invalid payment method: {body.payment_method_code}')

    # Создаём платёж через Platega
    from app.services.payment.platega import PlategaPaymentHandler
    handler = PlategaPaymentHandler()

    payment_result = await handler.create_platega_payment(
        db,
        user_id=user.id,
        amount_kopeks=total_kopeks,
        payment_method_code=body.payment_method_code,
        description=f'Покупка {qty} MTProxy',
        language='ru',
        min_amount_override=5000,
        metadata_extra={'for_product': 'mtproxy', 'proxy_quantity': qty},
    )

    if not payment_result:
        return DirectPurchaseResponse(success=False, error='Failed to create payment')

    return DirectPurchaseResponse(
        success=True,
        redirect_url=payment_result.get('redirect_url'),
        transaction_id=payment_result.get('transaction_id'),
        total_kopeks=total_kopeks,
    )


@router.get('/payment-methods')
async def get_payment_methods(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Доступные методы оплаты Platega для прокси."""
    if not settings.is_platega_enabled():
        return {'methods': [], 'enabled': False}

    methods = []
    definitions = settings.get_platega_method_definitions()
    active_codes = settings.get_platega_active_methods()

    for code in active_codes:
        if code in definitions:
            methods.append({
                'code': code,
                'name': definitions[code]['name'],
                'title': definitions[code]['title'],
            })

    return {
        'methods': methods,
        'enabled': True,
        'price_per_proxy': 5000,
        'max_quantity': 10,
    }
