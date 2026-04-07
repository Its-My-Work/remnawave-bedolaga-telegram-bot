from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Tariff
from ..dependencies import get_db_session, require_api_token


router = APIRouter()


class TariffResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    display_order: int
    is_active: bool
    traffic_limit_gb: int
    device_limit: int
    device_price_kopeks: Optional[int]
    period_prices: dict
    tier_level: int
    is_trial_available: bool
    allow_traffic_topup: bool
    created_at: Any
    updated_at: Any

    class Config:
        from_attributes = True


class TariffListResponse(BaseModel):
    items: List[TariffResponse]
    total: int
    limit: int
    offset: int


@router.get("", response_model=TariffListResponse)
async def get_tariffs(
    _: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    is_active: Optional[bool] = None,
) -> TariffListResponse:
    """Get all tariffs"""
    query = select(Tariff)
    count_query = select(func.count(Tariff.id))
    
    if is_active is not None:
        query = query.where(Tariff.is_active == is_active)
        count_query = count_query.where(Tariff.is_active == is_active)
    
    total = await db.scalar(count_query) or 0
    
    result = await db.execute(
        query.order_by(Tariff.display_order, Tariff.id)
        .offset(offset)
        .limit(limit)
    )
    tariffs = result.scalars().all()
    
    return TariffListResponse(
        items=[TariffResponse.model_validate(t) for t in tariffs],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/{tariff_id}", response_model=TariffResponse)
async def get_tariff(
    tariff_id: int,
    _: Any = Depends(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> TariffResponse:
    """Get tariff by ID"""
    tariff = await db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tariff not found")
    return TariffResponse.model_validate(tariff)
