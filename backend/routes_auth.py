"""/api/auth/* — register, login, logout, me, guest, upgrade, Google OAuth."""
from __future__ import annotations

import os
import re
import secrets
import time

import httpx
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
        "linked_apple": user.apple_sub is not None,
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
        user = await upsert_oauth_user(db, email, google_sub=sub)
    session_token, _ = await create_session_token(db, user.id)
    resp = RedirectResponse(url="/")
    resp.set_cookie(value=session_token, **_cookie_kwargs(secure=is_production()))
    return resp


# ---- Apple Sign In ---------------------------------------------------------
# Apple requires response_mode=form_post when the name/email scope is requested,
# so the callback is a cross-site POST. SameSite=lax cookies aren't sent on that
# POST, so we can't use the SessionMiddleware for CSRF state — instead we carry a
# signed, timestamped `state` token (itsdangerous) that we minted in /start. The
# client secret is a short-lived ES256 JWT signed with the .p8 key.

_APPLE_AUTH_URL = "https://appleid.apple.com/auth/authorize"
_APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
_APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"


def _apple_cfg() -> dict:
    return {
        "client_id": os.environ.get("APPLE_CLIENT_ID"),   # Services ID, e.g. com.backtick.web
        "team_id": os.environ.get("APPLE_TEAM_ID"),
        "key_id": os.environ.get("APPLE_KEY_ID"),
        "private_key": os.environ.get("APPLE_PRIVATE_KEY"),  # .p8 PEM contents
    }


def _apple_enabled() -> bool:
    return all(_apple_cfg().values())


def _apple_client_secret() -> str:
    """Apple's client_secret is an ES256 JWT signed with the team's .p8 key."""
    from authlib.jose import jwt as jose_jwt
    cfg = _apple_cfg()
    now = int(time.time())
    header = {"alg": "ES256", "kid": cfg["key_id"]}
    payload = {
        "iss": cfg["team_id"],
        "iat": now,
        "exp": now + 3600,                         # short-lived; only used during this flow
        "aud": "https://appleid.apple.com",
        "sub": cfg["client_id"],
    }
    return jose_jwt.encode(header, payload, cfg["private_key"]).decode("ascii")


def _apple_state_serializer() -> "URLSafeTimedSerializer":
    from itsdangerous import URLSafeTimedSerializer
    secret = os.environ.get("SESSION_SECRET", "dev-only-not-secret-change-me")
    return URLSafeTimedSerializer(secret, salt="apple-oauth-state")


@router.get("/apple/start")
async def apple_start(request: Request) -> RedirectResponse:
    if not _apple_enabled():
        raise HTTPException(503, "Apple Sign In is not configured on this server")
    from urllib.parse import urlencode
    redirect_uri = str(request.url_for("apple_callback"))
    params = {
        "response_type": "code",
        "response_mode": "form_post",
        "client_id": _apple_cfg()["client_id"],
        "redirect_uri": redirect_uri,
        "scope": "name email",
        "state": _apple_state_serializer().dumps("apple"),
    }
    return RedirectResponse(f"{_APPLE_AUTH_URL}?{urlencode(params)}")


@router.post("/apple/callback", name="apple_callback")
async def apple_callback(request: Request, db: AsyncSession = Depends(get_db)):
    if not _apple_enabled():
        raise HTTPException(503, "Apple Sign In is not configured on this server")
    from authlib.jose import JsonWebKey, jwt as jose_jwt
    from itsdangerous import BadSignature, SignatureExpired

    form = await request.form()
    code = form.get("code")
    state = form.get("state")
    try:
        _apple_state_serializer().loads(state or "", max_age=600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(400, "invalid or expired Apple state")
    if not code:
        raise HTTPException(400, "missing Apple authorization code")

    redirect_uri = str(request.url_for("apple_callback"))
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": _apple_cfg()["client_id"],
        "client_secret": _apple_client_secret(),
    }
    async with httpx.AsyncClient(timeout=10) as cx:
        tok = await cx.post(_APPLE_TOKEN_URL, data=data)
        if tok.status_code != 200:
            raise HTTPException(400, "Apple token exchange failed")
        id_token = tok.json().get("id_token")
        if not id_token:
            raise HTTPException(400, "Apple did not return an id_token")
        jwks = (await cx.get(_APPLE_KEYS_URL)).json()

    try:
        claims = jose_jwt.decode(id_token, JsonWebKey.import_key_set(jwks))
        claims.validate()
    except Exception:
        raise HTTPException(400, "could not verify Apple identity token")
    sub = claims.get("sub")
    email = claims.get("email")
    if not sub:
        raise HTTPException(400, "Apple identity token missing subject")

    # No guest-attach here: a cross-site POST doesn't carry the lax `auth` cookie,
    # so we can't see the current guest. That's fine — on login the frontend
    # restores this account's own last session anyway.
    user = await upsert_oauth_user(db, email or "", apple_sub=sub)
    session_token, _ = await create_session_token(db, user.id)
    resp = RedirectResponse(url="/", status_code=303)   # 303: POST → GET
    resp.set_cookie(value=session_token, **_cookie_kwargs(secure=is_production()))
    return resp
