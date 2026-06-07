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
GUEST_EMAIL_SUFFIX = "@guest.local"
# bcrypt only hashes the first 72 bytes; cap up front so longer passwords don't
# silently truncate to the same hash as a shorter prefix.
_BCRYPT_MAX = 72


def is_guest(user: "User | None") -> bool:
    return bool(user and user.email and user.email.endswith(GUEST_EMAIL_SUFFIX))


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
    """True when the app is served over HTTPS in production, so cookies get the
    Secure flag. Provider-neutral: set `ENV=production` (or `HTTPS_ONLY=true`) on
    any host. `RENDER` is kept as a legacy alias since Render sets it automatically
    — on other providers (Hetzner/Oracle/Fly/…) use ENV/HTTPS_ONLY instead."""
    env = os.environ
    return (
        env.get("ENV", "").lower() == "production"
        or env.get("HTTPS_ONLY", "").lower() in {"1", "true"}
        or env.get("RENDER", "").lower() in {"1", "true"}
    )


async def create_guest(db: AsyncSession) -> tuple[User, str]:
    """Create an anonymous User + opaque session token. Used by /api/auth/guest
    and by the / landing route to auto-provision a visitor so the chart loads
    immediately, no login wall. The synthetic email is unguessable; the random
    password hash makes the row internally consistent but unloginable by
    password (the user can later convert via /api/auth/upgrade)."""
    seed = secrets.token_hex(6)
    email = f"guest-{seed}{GUEST_EMAIL_SUFFIX}"
    user = User(email=email, password_hash=hash_password(secrets.token_hex(16)))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token, _ = await create_session_token(db, user.id)
    return user, token


async def upsert_oauth_user(
    db: AsyncSession, email: str, *, google_sub: str | None = None, apple_sub: str | None = None
) -> User:
    """Link or create the user identified by an OAuth provider's `sub` claim
    (Google or Apple). Resolution order: (1) existing row for this sub — returned
    as-is, so returning users don't need a fresh email (Apple omits it after the
    first sign-in); (2) existing row for this email — attach the sub; (3) create.
    Exactly one of google_sub / apple_sub must be set."""
    import re
    sub_col = User.google_sub if google_sub else User.apple_sub
    sub_val = google_sub or apple_sub
    if sub_val:
        existing = (await db.execute(select(User).where(sub_col == sub_val))).scalar_one_or_none()
        if existing is not None:
            return existing
    email = (email or "").lower().strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "invalid email from oauth provider")
    by_email = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if by_email is not None:
        if google_sub:
            by_email.google_sub = google_sub
        else:
            by_email.apple_sub = apple_sub
        by_email.email_verified = True
        await db.commit()
        return by_email
    user = User(email=email, google_sub=google_sub, apple_sub=apple_sub, email_verified=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
