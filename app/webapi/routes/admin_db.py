"""Admin DB query endpoint - allows admin panel to run SQL queries via HTTP instead of SSH."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Security
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..dependencies import get_db_session, require_api_token

router = APIRouter()


class DbQueryRequest(BaseModel):
    query: str
    format: str = "lines"  # "lines" = newline-separated text (like psql -t -A), "json" = list of dicts


class DbQueryResponse(BaseModel):
    success: bool
    result: str | None = None
    rows: list[dict[str, Any]] | None = None
    error: str | None = None


def _serialize_value(v: Any) -> str:
    """Serialize a value to string, converting dicts/lists to JSON."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


@router.post("/query", response_model=DbQueryResponse)
async def run_db_query(
    payload: DbQueryRequest,
    _: object = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> DbQueryResponse:
    """Execute a raw SQL query and return results.
    
    format=lines: returns result as newline-separated text (compatible with psql -t -A output)
    format=json: returns result as list of dicts
    """
    try:
        result = await db.execute(text(payload.query))
        
        # Check if this is a SELECT query that returns rows
        if result.returns_rows:
            columns = list(result.keys())
            rows_raw = result.fetchall()
            
            if payload.format == "json":
                rows = []
                for row in rows_raw:
                    d = {}
                    for col, val in zip(columns, row):
                        if isinstance(val, (dict, list)):
                            d[col] = val
                        else:
                            d[col] = val
                    rows.append(d)
                return DbQueryResponse(success=True, rows=rows)
            else:
                # Format as newline-separated text like psql -t -A
                lines = []
                for row in rows_raw:
                    if len(row) == 1:
                        lines.append(_serialize_value(row[0]))
                    else:
                        lines.append("|".join(_serialize_value(v) for v in row))
                return DbQueryResponse(success=True, result="\n".join(lines))
        else:
            # INSERT/UPDATE/DELETE
            await db.commit()
            return DbQueryResponse(success=True, result="OK")
    except Exception as e:
        await db.rollback()
        return DbQueryResponse(success=False, error=str(e))
