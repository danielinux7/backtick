"""/api/symbols/search and /api/watchlist/*."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import current_user
from .db import get_db
from .exchange_info import search_symbols
from .models import User, WatchlistItem

router = APIRouter(tags=["symbols"])


class AddWatchReq(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    market: str = Field(..., pattern="^(spot|futures)$")


class ReorderReq(BaseModel):
    ids: list[int]


def _norm_symbol(s: str) -> str:
    return s.upper().strip()


@router.get("/api/symbols/search")
async def symbols_search(
    q: str = Query(default="", min_length=0, max_length=32),
    market: str = Query(default="spot", pattern="^(spot|futures)$"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    if not q:
        return {"results": []}
    try:
        results = search_symbols(market, q, limit)
    except Exception as e:
        raise HTTPException(502, f"exchangeInfo fetch failed: {e}") from e
    return {"results": results}


@router.get("/api/watchlist")
async def list_watchlist(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    res = await db.execute(
        select(WatchlistItem)
        .where(WatchlistItem.user_id == user.id)
        .order_by(WatchlistItem.position, WatchlistItem.id)
    )
    items = res.scalars().all()
    return {
        "items": [
            {"id": w.id, "symbol": w.symbol, "market": w.market, "position": w.position}
            for w in items
        ]
    }


@router.post("/api/watchlist")
async def add_watchlist(
    req: AddWatchReq,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sym = _norm_symbol(req.symbol)
    existing = await db.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user.id,
            WatchlistItem.symbol == sym,
            WatchlistItem.market == req.market,
        )
    )
    item = existing.scalar_one_or_none()
    if item is not None:
        return {"id": item.id, "symbol": item.symbol, "market": item.market, "position": item.position}
    max_pos = (await db.execute(
        select(WatchlistItem.position).where(WatchlistItem.user_id == user.id)
    )).scalars().all()
    pos = (max(max_pos) + 1) if max_pos else 0
    item = WatchlistItem(user_id=user.id, symbol=sym, market=req.market, position=pos)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return {"id": item.id, "symbol": item.symbol, "market": item.market, "position": item.position}


@router.delete("/api/watchlist/{item_id}")
async def delete_watchlist(
    item_id: int,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    item = await db.get(WatchlistItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(404, "watchlist item not found")
    await db.delete(item)
    await db.commit()
    return {"ok": True}


@router.post("/api/watchlist/reorder")
async def reorder_watchlist(
    req: ReorderReq,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    res = await db.execute(
        select(WatchlistItem).where(WatchlistItem.user_id == user.id)
    )
    by_id = {item.id: item for item in res.scalars().all()}
    for new_pos, item_id in enumerate(req.ids):
        item = by_id.get(item_id)
        if item is None:
            continue
        item.position = new_pos
    await db.commit()
    return {"ok": True}
