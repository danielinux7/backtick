"""Unit tests for the provider-agnostic OAuth upsert and the Apple client
secret JWT. The full Apple round-trip (authorize → form_post callback → token
exchange) needs real Apple Developer credentials + an HTTPS prod redirect, so
it can't run here — these cover the pieces that are testable offline."""
import time

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.auth import upsert_oauth_user
from backend.db import Base
from backend.models import User


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_apple_upsert_creates_then_matches_by_sub(db):
    u1 = await upsert_oauth_user(db, "a@example.com", apple_sub="apple-001")
    assert u1.apple_sub == "apple-001" and u1.email == "a@example.com"
    # a later sign-in with the same sub but NO email (Apple omits it) returns the
    # same user — must not fail email validation or create a duplicate
    u2 = await upsert_oauth_user(db, "", apple_sub="apple-001")
    assert u2.id == u1.id


@pytest.mark.asyncio
async def test_apple_upsert_attaches_to_existing_email(db):
    existing = User(email="b@example.com", email_verified=False)
    db.add(existing)
    await db.commit()
    await db.refresh(existing)

    linked = await upsert_oauth_user(db, "b@example.com", apple_sub="apple-002")
    assert linked.id == existing.id
    assert linked.apple_sub == "apple-002" and linked.email_verified is True


@pytest.mark.asyncio
async def test_google_and_apple_are_independent_subs(db):
    g = await upsert_oauth_user(db, "c@example.com", google_sub="g-1")
    a = await upsert_oauth_user(db, "c@example.com", apple_sub="a-1")  # same email
    assert g.id == a.id                          # attached to the same account
    assert a.google_sub == "g-1" and a.apple_sub == "a-1"


def test_apple_client_secret_is_valid_es256_jwt(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from authlib.jose import jwt as jose_jwt

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    monkeypatch.setenv("APPLE_CLIENT_ID", "com.backtick.web")
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("APPLE_KEY_ID", "KEY1234567")
    monkeypatch.setenv("APPLE_PRIVATE_KEY", pem)

    from backend.routes_auth import _apple_client_secret, _apple_enabled
    assert _apple_enabled() is True

    secret = _apple_client_secret()
    claims = jose_jwt.decode(secret, pub_pem)        # verifies the ES256 signature
    assert claims["iss"] == "TEAM123456"
    assert claims["sub"] == "com.backtick.web"
    assert claims["aud"] == "https://appleid.apple.com"
    assert claims["exp"] > time.time()


def test_apple_disabled_without_full_config(monkeypatch):
    for k in ("APPLE_CLIENT_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    from backend.routes_auth import _apple_enabled
    assert _apple_enabled() is False


# ---- GET /api/auth/providers (drives frontend button visibility) ----------
# Public route, no DB/auth deps, so we hit it over the real app via ASGI.

_OAUTH_ENV = ("APPLE_CLIENT_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY",
              "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")


async def _get_providers():
    from httpx import ASGITransport, AsyncClient
    from backend import main
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/auth/providers")
    assert r.status_code == 200
    return r.json()


@pytest.mark.asyncio
async def test_providers_reports_unconfigured(monkeypatch):
    for k in _OAUTH_ENV:
        monkeypatch.delenv(k, raising=False)
    assert await _get_providers() == {"google": False, "apple": False}


@pytest.mark.asyncio
async def test_providers_reports_apple_enabled(monkeypatch):
    monkeypatch.setenv("APPLE_CLIENT_ID", "com.backtick.web")
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("APPLE_KEY_ID", "KEY1234567")
    monkeypatch.setenv("APPLE_PRIVATE_KEY", "pem")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert await _get_providers() == {"google": False, "apple": True}
