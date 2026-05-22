"""Async SQLAlchemy engine + session factory.

DATABASE_URL drives backend choice:
  - sqlite+aiosqlite:///./backtick.db  (local dev default)
  - postgresql+asyncpg://user:pw@host/db (Render production)

A bare 'postgres://...' or 'postgresql://...' (the form Render exports) is
normalized to the async asyncpg driver automatically.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


def _normalize_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _normalize_url(
    os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./backtick.db")
)

engine = create_async_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
