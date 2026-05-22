"""/api/auth/* — register, login, logout, me, guest, upgrade, Google OAuth."""
from __future__ import annotations

import os
import re
import secrets

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import (
    COOKIE_NAME,
    _cookie_kwargs,
    create_guest,
    create_session_token,
    current_user,
    hash_password,
    is_guest,
    is_production,
    revoke_session_token,
    upsert_oauth_user,
    verify_password,
)
from .db import get_db
from .models import SessionToken, User

router = APIRouter(prefix="/api/auth", tags=["auth"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterReq(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., min_length=8, max_length=128)


class LoginReq(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=128)


@router.post("/register")
async def register(req: RegisterReq, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    email = req.email.lower().strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "invalid email")
    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "email already registered")
    user = User(email=email, password_hash=hash_password(req.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token, _ = await create_session_token(db, user.id)
    resp = JSONResponse({"id": user.id, "email": user.email})
    resp.set_cookie(value=token, **_cookie_kwargs(secure=is_production()))
    return resp


@router.post("/login")
async def login(req: LoginReq, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    email = req.email.lower().strip()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "invalid credentials")
    token, _ = await create_session_token(db, user.id)
    resp = JSONResponse({"id": user.id, "email": user.email})
    resp.set_cookie(value=token, **_cookie_kwargs(secure=is_production()))
    return resp


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    tok = request.cookies.get(COOKIE_NAME)
    if tok:
        await revoke_session_token(db, tok)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict:
    guest = is_guest(user)
    return {
        "id": user.id,
        # Hide the synthetic guest-XXXX@guest.local email from the UI.
        "email": None if guest else user.email,
        "is_guest": guest,
        "email_verified": user.email_verified,
        "linked_google": user.google_sub is not None,
    }


@router.post("/guest")
async def guest(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Start an anonymous session with a fresh User row. The user can later
    convert this row into a real account via /api/auth/upgrade without losing
    their trades, watchlist, or persisted replay snapshots."""
    user, token = await create_guest(db)
    resp = JSONResponse({"id": user.id, "is_guest": True})
    resp.set_cookie(value=token, **_cookie_kwargs(secure=is_production()))
    return resp


@router.post("/upgrade")
async def upgrade(
    req: RegisterReq,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Convert the current guest account into a permanent one. Same user_id,
    so all existing snapshots / watchlist / trades carry over."""
    if not is_guest(user):
        raise HTTPException(400, "already a permanent account")
    email = req.email.lower().strip()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "invalid email")
    clash = (await db.execute(
        select(User).where(User.email == email, User.id != user.id)
    )).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(409, "email already registered")
    user.email = email
    user.password_hash = hash_password(req.password)
    await db.commit()
    return JSONResponse({"id": user.id, "email": user.email, "is_guest": False})


# ---- Google OAuth ---------------------------------------------------------

_oauth = OAuth()
_google_registered = False


def _ensure_google_registered() -> bool:
    global _google_registered
    if _google_registered:
        return True
    cid = os.environ.get("GOOGLE_CLIENT_ID")
    csec = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not cid or not csec:
        return False
    _oauth.register(
        name="google",
        client_id=cid,
        client_secret=csec,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    _google_registered = True
    return True


@router.get("/google/start")
async def google_start(request: Request) -> RedirectResponse:
    if not _ensure_google_registered():
        raise HTTPException(503, "Google OAuth is not configured on this server")
    redirect_uri = str(request.url_for("google_callback"))
    return await _oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/google/callback", name="google_callback")
async def google_callback(request: Request, db: AsyncSession = Depends(get_db)):
    if not _ensure_google_registered():
        raise HTTPException(503, "Google OAuth is not configured on this server")
    token = await _oauth.google.authorize_access_token(request)
    info = token.get("userinfo") or await _oauth.google.parse_id_token(request, token)
    sub = info.get("sub")
    email = info.get("email")
    if not sub or not email:
        raise HTTPException(400, "google did not return an email")

    # If the visitor was already a guest, attach the Google identity to their
    # existing row so they keep their trades/snapshots. We only do this when
    # the Google account isn't already linked to a different non-guest user.
    existing_cookie = request.cookies.get(COOKIE_NAME)
    user = None
    if existing_cookie:
        tok = await db.get(SessionToken, existing_cookie)
        if tok is not None:
            current = await db.get(User, tok.user_id)
            if current is not None and is_guest(current):
                claimed_by_sub = (await db.execute(
                    select(User).where(User.google_sub == sub, User.id != current.id)
                )).scalar_one_or_none()
                claimed_by_email = (await db.execute(
                    select(User).where(User.email == email.lower().strip(), User.id != current.id)
                )).scalar_one_or_none()
                if claimed_by_sub is None and claimed_by_email is None:
                    current.email = email.lower().strip()
                    current.google_sub = sub
                    current.email_verified = True
                    await db.commit()
                    user = current
    if user is None:
        user = await upsert_oauth_user(db, email, sub)
    session_token, _ = await create_session_token(db, user.id)
    resp = RedirectResponse(url="/")
    resp.set_cookie(value=session_token, **_cookie_kwargs(secure=is_production()))
    return resp
