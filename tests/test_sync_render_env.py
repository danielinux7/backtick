"""Unit tests for the secrets→Render env-var planner (pure part)."""
from scripts.sync_render_env import _mask, _plan


def test_plan_splits_meta_env_and_blanks():
    secrets = {
        "_comment": "ignore me",
        "_render_api_key": "rnd_key",
        "_render_service_name": "backtick",
        "DATABASE_URL": "sqlite+aiosqlite:////var/data/backtick.db",
        "GOOGLE_CLIENT_SECRET": "g-secret",
        "SESSION_SECRET": "",          # blank → deferred to host default
        "APPLE_PRIVATE_KEY": "   ",    # whitespace-only also counts as blank
    }
    meta, env, skipped = _plan(secrets)

    assert meta == {
        "_comment": "ignore me",
        "_render_api_key": "rnd_key",
        "_render_service_name": "backtick",
    }
    assert env == {
        "DATABASE_URL": "sqlite+aiosqlite:////var/data/backtick.db",
        "GOOGLE_CLIENT_SECRET": "g-secret",
    }
    assert sorted(skipped) == ["APPLE_PRIVATE_KEY", "SESSION_SECRET"]


def test_plan_empty_when_all_blank():
    meta, env, skipped = _plan({"_render_api_key": "k", "A": "", "B": ""})
    assert env == {}
    assert sorted(skipped) == ["A", "B"]


def test_mask_hides_long_values_but_shows_shape():
    assert _mask("secret-value-1234") == "sec…34 (17 chars)"
    assert _mask("short") == "short"      # too short to mask meaningfully
