"""Password hashing, opaque cookie session tokens, FastAPI current_user dep."""
from __future__ import annotations

import datetime as dt
import os
import secrets

import bcrypt
from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .models import SessionToken, User

COOKIE_NAME = "auth"
SESSION_DAYS = 30
# bcrypt only hashes the first 72 bytes; cap up front so longer passwords don't
# silently truncate to the same hash as a shorter prefix.
_BCRYPT_MAX = 72


def _to_b(s: str) -> bytes:
    return s.encode("utf-8")[:_BCRYPT_MAX]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_to_b(plain), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_to_b(plain), hashed.encode("ascii"))
    except Exception:
        return False


async def create_session_token(db: AsyncSession, user_id: int) -> tuple[str, dt.datetime]:
    token = secrets.token_hex(32)
    now = dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(days=SESSION_DAYS)
    db.add(SessionToken(token=token, user_id=user_id, created_at=now, expires_at=expires, last_seen=now))
    await db.commit()
    return token, expires


async def revoke_session_token(db: AsyncSession, token: str) -> None:
    row = await db.get(SessionToken, token)
    if row is not None:
        await db.delete(row)
        await db.commit()


async def current_user(
    auth: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not auth:
        raise HTTPException(401, "not authenticated")
    tok = await db.get(SessionToken, auth)
    if tok is None:
        raise HTTPException(401, "session not found")
    now = dt.datetime.now(dt.timezone.utc)
    expires = tok.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    if expires < now:
        await db.delete(tok)
        await db.commit()
        raise HTTPException(401, "session expired")
    tok.last_seen = now
    await db.commit()
    user = await db.get(User, tok.user_id)
    if user is None:
        raise HTTPException(401, "user not found")
    return user


async def current_user_optional(
    auth: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    if not auth:
        return None
    tok = await db.get(SessionToken, auth)
    if tok is None:
        return None
    expires = tok.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    if expires < dt.datetime.now(dt.timezone.utc):
        return None
    return await db.get(User, tok.user_id)


def _cookie_kwargs(secure: bool) -> dict:
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
        "max_age": SESSION_DAYS * 86400,
    }


def is_production() -> bool:
    return os.environ.get("RENDER", "").lower() in {"1", "true"} or os.environ.get("ENV", "") == "production"


async def upsert_oauth_user(db: AsyncSession, email: str, google_sub: str) -> User:
    """Link or create the user identified by Google's sub claim. If an account
    already exists for this email (password-only), attach the google_sub to it
    rather than creating a duplicate."""
    import re
    email = (email or "").lower().strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "invalid email from oauth provider")
    res = await db.execute(select(User).where(User.google_sub == google_sub))
    user = res.scalar_one_or_none()
    if user is not None:
        return user
    res = await db.execute(select(User).where(User.email == email))
    user = res.scalar_one_or_none()
    if user is not None:
        user.google_sub = google_sub
        user.email_verified = True
        await db.commit()
        return user
    user = User(email=email, google_sub=google_sub, email_verified=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
