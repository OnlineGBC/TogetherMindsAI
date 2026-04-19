"""
tests/test_config.py
--------------------
Unit tests for config.py — platform detection, flag derivation, and
startup validation.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import importlib
import pytest
from unittest.mock import patch


def _reload_config(env_overrides: dict) -> object:
    """Reload config with a custom environment and return the module."""
    base_env = {
        "TESTING": "1",
        "SECRET_KEY": "test-key",
        "ANTHROPIC_API_KEY": "test-api-key",
        "FLASK_DEBUG": "false",
        "CORS_ALLOWED_ORIGINS": "http://localhost:5001",
    }
    base_env.update(env_overrides)
    with patch.dict(os.environ, base_env, clear=True):
        import config as cfg
        importlib.reload(cfg)
        return cfg


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

class TestPlatformDetection:
    def test_sqlite_url_sets_is_sqlite(self):
        cfg = _reload_config({"DATABASE_URL": "sqlite:///test.db"})
        assert cfg.IS_SQLITE is True

    def test_postgres_url_clears_is_sqlite(self):
        cfg = _reload_config({"DATABASE_URL": "postgresql://user:pass@localhost/db"})
        assert cfg.IS_SQLITE is False

    def test_testing_flag_respected(self):
        cfg = _reload_config({"TESTING": "1"})
        assert cfg.IS_TESTING is True

    def test_testing_false_when_unset(self):
        cfg = _reload_config({"TESTING": "0"})
        assert cfg.IS_TESTING is False

    def test_is_production_false_in_tests(self):
        # IS_PRODUCTION requires not testing AND not SQLite
        cfg = _reload_config({"TESTING": "1", "DATABASE_URL": "postgresql://x/y"})
        assert cfg.IS_PRODUCTION is False

    def test_is_production_false_for_sqlite(self):
        cfg = _reload_config({"TESTING": "0", "DATABASE_URL": "sqlite:///test.db"})
        assert cfg.IS_PRODUCTION is False

    def test_is_production_true_for_postgres_non_test(self):
        cfg = _reload_config({"TESTING": "0", "DATABASE_URL": "postgresql://x:y@z/db"})
        assert cfg.IS_PRODUCTION is True


# ---------------------------------------------------------------------------
# SQLAlchemy engine options
# ---------------------------------------------------------------------------

class TestEngineOptions:
    def test_sqlite_does_not_use_static_pool(self):
        # StaticPool is for in-memory test fixtures only, not file-based SQLite
        cfg = _reload_config({"DATABASE_URL": "sqlite:///test.db"})
        assert "poolclass" not in cfg.SQLALCHEMY_ENGINE_OPTIONS

    def test_sqlite_sets_check_same_thread(self):
        cfg = _reload_config({"DATABASE_URL": "sqlite:///test.db"})
        assert cfg.SQLALCHEMY_ENGINE_OPTIONS["connect_args"]["check_same_thread"] is False

    def test_postgres_uses_pool_size(self):
        cfg = _reload_config({"DATABASE_URL": "postgresql://u:p@h/d"})
        assert "pool_size" in cfg.SQLALCHEMY_ENGINE_OPTIONS
        assert "poolclass" not in cfg.SQLALCHEMY_ENGINE_OPTIONS


# ---------------------------------------------------------------------------
# Async mode
# ---------------------------------------------------------------------------

class TestAsyncMode:
    def test_testing_uses_threading(self):
        cfg = _reload_config({"TESTING": "1", "FLASK_DEBUG": "false"})
        assert cfg.ASYNC_MODE == "threading"

    def test_debug_uses_threading(self):
        cfg = _reload_config({"TESTING": "0", "FLASK_DEBUG": "true"})
        assert cfg.ASYNC_MODE == "threading"

    def test_production_uses_eventlet(self):
        cfg = _reload_config({"TESTING": "0", "FLASK_DEBUG": "false"})
        assert cfg.ASYNC_MODE == "eventlet"


# ---------------------------------------------------------------------------
# Rate limiting defaults
# ---------------------------------------------------------------------------

class TestRateLimitDefaults:
    def test_defaults_present(self):
        cfg = _reload_config({})
        assert cfg.RATE_WINDOW_SECONDS == 60
        assert cfg.RATE_MAX_MESSAGES == 20
        assert cfg.MAX_MESSAGE_LENGTH == 8000

    def test_overrides_respected(self):
        cfg = _reload_config({
            "RATE_WINDOW_SECONDS": "30",
            "RATE_MAX_MESSAGES": "5",
            "MAX_MESSAGE_LENGTH": "500",
        })
        assert cfg.RATE_WINDOW_SECONDS == 30
        assert cfg.RATE_MAX_MESSAGES == 5
        assert cfg.MAX_MESSAGE_LENGTH == 500

    def test_ai_cooldown_default(self):
        cfg = _reload_config({})
        assert cfg.AI_COOLDOWN_SECONDS == 20

    def test_ai_cooldown_override(self):
        cfg = _reload_config({"AI_COOLDOWN_SECONDS": "10"})
        assert cfg.AI_COOLDOWN_SECONDS == 10


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------

class TestValidateConfig:
    def test_no_error_in_testing_mode(self):
        cfg = _reload_config({"TESTING": "1", "SECRET_KEY": "", "ANTHROPIC_API_KEY": ""})
        # Should not raise — validation is skipped in test mode
        cfg.validate_config()

    def test_raises_when_secret_key_missing(self):
        cfg = _reload_config({"TESTING": "0", "SECRET_KEY": ""})
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            cfg.validate_config()

    def test_raises_when_anthropic_key_missing(self):
        cfg = _reload_config({"TESTING": "0", "ANTHROPIC_API_KEY": ""})
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            cfg.validate_config()

    def test_no_error_when_all_local_vars_present(self):
        cfg = _reload_config({
            "TESTING": "0",
            "SECRET_KEY": "abc",
            "ANTHROPIC_API_KEY": "xyz",
            "DATABASE_URL": "sqlite:///test.db",
        })
        # SQLite + all required vars — should not raise
        cfg.validate_config()

    def test_raises_for_postgres_url_without_pg_keyword(self):
        cfg = _reload_config({
            "TESTING": "0",
            "SECRET_KEY": "abc",
            "ANTHROPIC_API_KEY": "xyz",
            "DATABASE_URL": "mysql://user:pass@host/db",
            "CORS_ALLOWED_ORIGINS": "https://example.run.app",
        })
        # IS_PRODUCTION=True, DATABASE_URL doesn't contain "postgresql"
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            cfg.validate_config()
