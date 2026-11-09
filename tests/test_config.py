from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from dividend.config import Settings


def policy_file(tmp_path: Path, measured: bool) -> Path:
    path = tmp_path / f"allocation_policy_{measured}.yaml"
    path.write_text(yaml.safe_dump({"measured": measured}), encoding="utf-8")
    return path


def base(tmp_path: Path, **values) -> dict:
    settings = {
        "ADMIN_TOKEN": "test-admin-token",
        "PUBLIC_BASE_URL": "https://example.test",
        "PAYMENTS_MODE": "fake",
        "LLM_LIVE_ENABLED": False,
        "allocation_policy_path": policy_file(tmp_path, False),
    }
    settings.update(values)
    return settings


def test_valid_fake_settings_without_optional_credentials(tmp_path):
    settings = Settings(**base(tmp_path))
    assert settings.PAYMENTS_MODE == "fake"
    assert settings.ENRICH_MODE == "fixtures"
    assert settings.PAYPAL_CLIENT_SECRET is None
    assert settings.GITHUB_TOKEN is None


def test_paypal_requires_credentials_but_not_measured_policy(tmp_path):
    settings = Settings(**base(
        tmp_path,
        PAYMENTS_MODE="paypal",
        PAYPAL_CLIENT_ID="sandbox-id",
        PAYPAL_CLIENT_SECRET="sandbox-secret",
        allocation_policy_path=policy_file(tmp_path, False),
    ))
    assert settings.PAYMENTS_MODE == "paypal"


@pytest.mark.parametrize("missing", ["PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"])
def test_paypal_still_requires_credentials(tmp_path, missing):
    credentials = {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": "secret"}
    credentials.pop(missing)
    with pytest.raises(ValidationError, match=missing):
        Settings(**base(tmp_path, PAYMENTS_MODE="paypal", **credentials))


def test_fake_mode_works_with_actual_unmeasured_policy():
    from dividend.config import DEFAULT_POLICY_PATH

    assert Settings(**base(
        DEFAULT_POLICY_PATH.parent,
        allocation_policy_path=DEFAULT_POLICY_PATH,
    )).PAYMENTS_MODE == "fake"


def test_non_sandbox_and_invalid_payment_mode_rejected(tmp_path):
    with pytest.raises(ValidationError):
        Settings(**base(tmp_path, PAYMENTS_MODE="paypal", PAYPAL_ENV="production",
                        PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="secret"))
    with pytest.raises(ValidationError):
        Settings(**base(tmp_path, PAYMENTS_MODE="other"))


def test_groq_only_and_live_key_requirement(tmp_path):
    with pytest.raises(ValidationError):
        Settings(**base(tmp_path, LLM_PROVIDER="other"))
    with pytest.raises(ValidationError, match="GROQ_API_KEY"):
        Settings(**base(tmp_path, LLM_LIVE_ENABLED=True))
    assert Settings(**base(tmp_path, LLM_LIVE_ENABLED=True, LLM_KILL_SWITCH=True)).LLM_KILL_SWITCH


def test_fixture_enrichment_needs_no_live_credentials(tmp_path):
    assert Settings(**base(tmp_path, ENRICH_MODE="fixtures")).UDD_DSN is None


@pytest.mark.parametrize("missing", ["GITHUB_TOKEN", "UDD_DSN"])
def test_live_enrichment_requires_both_credentials(tmp_path, missing):
    values = {"ENRICH_MODE": "live", "GITHUB_TOKEN": "token", "UDD_DSN": "dsn"}
    values.pop(missing)
    with pytest.raises(ValidationError, match=missing):
        Settings(**base(tmp_path, **values))


@pytest.mark.parametrize("missing", ["ADMIN_TOKEN", "PUBLIC_BASE_URL"])
def test_required_application_settings(tmp_path, missing):
    values = base(tmp_path)
    values.pop(missing)
    with pytest.raises(ValidationError):
        Settings(**values)


def test_invalid_cors_and_database_settings_rejected(tmp_path):
    with pytest.raises(ValidationError):
        Settings(**base(tmp_path, CORS_ORIGINS=["not a url"]))
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(**base(tmp_path, DATABASE_URL="not-a-database-url"))
