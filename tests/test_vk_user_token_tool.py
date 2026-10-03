"""Tests for scripts/vk-user-token.py — the personal-token helper.

Hermetic: no network, no real `.env`, no real secret. `VK_*` variables are stripped at import so a
developer's own profile can never steer a case (the same rule the other suites follow).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import stat
import sys

import pytest

for _key in [key for key in os.environ if key.startswith("VK_")]:
    os.environ.pop(_key, None)

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_DIR / "scripts" / "vk-user-token.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("vk_user_token_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vk_user_token_tool"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool(tmp_path, monkeypatch):
    module = _load_tool()
    monkeypatch.setattr(module, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(module, "SECRET_FILE", tmp_path / "vk-app-secret")
    monkeypatch.delenv("VK_APP_SECRET", raising=False)
    return module


# --- ссылка согласия -------------------------------------------------------------------

def test_implicit_url_asks_for_a_token_and_the_video_scope(tool):
    url = tool.build_authorize_url("1234567", flow="implicit")
    assert url.startswith("https://oauth.vk.com/authorize?")
    assert "response_type=token" in url and "scope=video" in url and "client_id=1234567" in url
    assert "client_secret" not in url


def test_code_flow_url_asks_for_a_code(tool):
    url = tool.build_authorize_url("1234567", flow="code")
    assert "response_type=code" in url and url.startswith("https://oauth.vk.com/authorize?")


def test_vkid_url_uses_the_vkid_host_and_carries_state(tool):
    url = tool.build_authorize_url("1234567", flow="vkid")
    assert url.startswith("https://id.vk.com/authorize?")
    assert "response_type=code" in url and "state=hermes-vk" in url and "secret" not in url


# --- обмен кода -----------------------------------------------------------------------

def test_legacy_exchange_posts_the_secret_to_the_legacy_endpoint(tool):
    url, form = tool.exchange_request("code", app_id="1234567", code="THE_CODE",
                                      redirect_uri="https://oauth.vk.com/blank.html", secret="THE_SECRET")
    assert url == tool.LEGACY_TOKEN_ENDPOINT
    assert form["client_secret"] == "THE_SECRET" and form["code"] == "THE_CODE"
    assert form["redirect_uri"] == "https://oauth.vk.com/blank.html"


def test_vkid_exchange_sends_device_id_and_grant_and_drops_empty_values(tool):
    url, form = tool.exchange_request("vkid", app_id="1234567", code="THE_CODE", redirect_uri="https://x/y",
                                      secret="THE_SECRET", device_id="DEV42")
    assert url == tool.VK_ID_ENDPOINT
    assert form["device_id"] == "DEV42" and form["grant_type"] == "authorization_code"
    assert "code_verifier" not in form  # пустые значения не уезжают


# --- запись в .env ---------------------------------------------------------------------

def test_write_env_value_replaces_only_its_own_line_and_keeps_the_rest(tool, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# комментарий\nVK_TOKEN=community\nVK_USER_TOKEN=old\nVK_HOME_CHANNEL=13580122\n",
                   encoding="utf-8")
    tool.write_env_value(env, "VK_USER_TOKEN", "brand-new-token")
    text = env.read_text(encoding="utf-8")
    assert "VK_USER_TOKEN=brand-new-token" in text and "old" not in text
    assert "VK_TOKEN=community" in text and "# комментарий" in text and "VK_HOME_CHANNEL=13580122" in text


def test_write_env_value_appends_when_the_key_is_absent(tool, tmp_path):
    env = tmp_path / ".env"
    env.write_text("VK_TOKEN=community\n", encoding="utf-8")
    tool.write_env_value(env, "VK_USER_TOKEN", "t")
    assert "VK_USER_TOKEN=t" in env.read_text(encoding="utf-8")


def test_write_env_value_makes_the_file_owner_only(tool, tmp_path):
    env = tmp_path / ".env"
    tool.write_env_value(env, "VK_USER_TOKEN", "t")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


# --- секрет приложения -----------------------------------------------------------------

def test_secret_comes_from_the_environment_first(tool, monkeypatch, tmp_path):
    (tmp_path / "vk-app-secret").write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("VK_APP_SECRET", "from-env")
    assert tool.load_app_secret() == "from-env"


def test_secret_falls_back_to_the_owner_only_file(tool):
    tool.SECRET_FILE.write_text("from-file\n", encoding="utf-8")
    assert tool.load_app_secret() == "from-file"


def test_missing_secret_stops_with_instructions(tool):
    with pytest.raises(SystemExit) as exc:
        tool.load_app_secret()
    assert "VK_APP_SECRET" in str(exc.value) and "600" in str(exc.value)


def test_loose_secret_file_is_flagged(tool, capsys):
    tool.SECRET_FILE.write_text("s", encoding="utf-8")
    tool.SECRET_FILE.chmod(0o644)
    assert tool.load_app_secret() == "s"
    assert "ПРЕДУПРЕЖДЕНИЕ" in capsys.readouterr().out


# --- маскирование ----------------------------------------------------------------------

def test_redact_hides_secrets_and_access_tokens(tool):
    text = tool.redact("boom access_token=SECRETVALUE and key KKKKKKKKKKKK", "KKKKKKKKKKKK")
    assert "SECRETVALUE" not in text and "KKKKKKKKKKKK" not in text
    assert "[REDACTED]" in text


# --- CLI -------------------------------------------------------------------------------

def test_store_writes_the_token_without_printing_it(tool, capsys):
    assert tool.main(["store", "--token", "a-brand-new-personal-token"]) == 0
    out = capsys.readouterr().out
    assert "a-brand-new-personal-token" not in out
    assert f"длина {len('a-brand-new-personal-token')}" in out
    assert tool.ENV_FILE.read_text(encoding="utf-8").strip() == "VK_USER_TOKEN=a-brand-new-personal-token"


def test_exchange_dry_run_lists_parameters_but_no_values(tool, capsys):
    tool.SECRET_FILE.write_text("THE_APP_SECRET", encoding="utf-8")
    assert tool.main(["exchange", "--app-id", "1", "--code", "THE_CODE", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "THE_APP_SECRET" not in out and "THE_CODE" not in out
    assert "client_secret" in out and tool.LEGACY_TOKEN_ENDPOINT in out


def test_exchange_refusal_from_vk_names_the_error_without_echoing_the_secret(tool, monkeypatch, capsys):
    tool.SECRET_FILE.write_text("THE_APP_SECRET", encoding="utf-8")
    monkeypatch.setattr(tool, "_post", lambda url, form, **kw: {"error": "invalid_client",
                                                               "error_description": "client_secret is incorrect"})
    assert tool.main(["exchange", "--app-id", "1", "--code", "THE_CODE"]) == 1
    out = capsys.readouterr().out
    assert "invalid_client" in out and "THE_APP_SECRET" not in out
    assert not tool.ENV_FILE.exists()  # отказ не должен ничего записывать


def test_exchange_writes_both_tokens_when_vk_returns_a_refresh_token(tool, monkeypatch, capsys):
    tool.SECRET_FILE.write_text("THE_APP_SECRET", encoding="utf-8")
    monkeypatch.setattr(tool, "_post", lambda url, form, **kw: {
        "access_token": "ACCESS", "refresh_token": "REFRESH", "expires_in": 3600, "user_id": 1})
    assert tool.main(["exchange", "--app-id", "1", "--code", "THE_CODE"]) == 0
    text = tool.ENV_FILE.read_text(encoding="utf-8")
    assert "VK_USER_TOKEN=ACCESS" in text and "VK_USER_REFRESH_TOKEN=REFRESH" in text
    out = capsys.readouterr().out
    assert "ACCESS" not in out and "REFRESH" not in out
    assert "3600" in out


def test_status_fails_loudly_when_the_token_is_missing(tool):
    assert tool.main(["status"]) == 1


# --- обновление по refresh_token (шаг, без которого VK ID нежизнеспособен) -----------------

def test_refresh_request_uses_the_refresh_grant_and_the_secret(tool):
    url, form = tool.refresh_request("code", app_id="1", refresh_token="R", secret="S")
    assert url == tool.LEGACY_TOKEN_ENDPOINT and form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "R" and form["client_secret"] == "S"


def test_vkid_refresh_carries_the_device_id(tool):
    url, form = tool.refresh_request("vkid", app_id="1", refresh_token="R", secret="S", device_id="D")
    assert url == tool.VK_ID_ENDPOINT and form["device_id"] == "D" and form["grant_type"] == "refresh_token"


def test_refresh_without_a_stored_refresh_token_says_what_is_missing(tool, capsys):
    tool.ENV_FILE.write_text("VK_USER_TOKEN=old\n", encoding="utf-8")
    assert tool.main(["refresh", "--app-id", "1"]) == 1
    assert "VK_USER_REFRESH_TOKEN" in capsys.readouterr().out


def test_refresh_takes_the_app_id_and_flow_from_the_env_when_not_given(tool, monkeypatch, capsys):
    tool.ENV_FILE.write_text("VK_USER_REFRESH_TOKEN=R\nVK_APP_ID=42\nVK_AUTH_FLOW=vkid\n", encoding="utf-8")
    tool.SECRET_FILE.write_text("THE_APP_SECRET", encoding="utf-8")
    seen = {}

    def fake_post(url, form, **kw):
        seen.update({"url": url, "form": form})
        return {"access_token": "NEW", "refresh_token": "R2", "expires_in": 3600}

    monkeypatch.setattr(tool, "_post", fake_post)
    assert tool.main(["refresh"]) == 0
    assert seen["url"] == tool.VK_ID_ENDPOINT and seen["form"]["client_id"] == "42"
    text = tool.ENV_FILE.read_text(encoding="utf-8")
    assert "VK_USER_TOKEN=NEW" in text and "VK_USER_REFRESH_TOKEN=R2" in text
    out = capsys.readouterr().out
    assert "NEW" not in out and "R2" not in out


def test_refresh_refusal_keeps_the_old_token_and_writes_nothing(tool, monkeypatch, capsys):
    tool.ENV_FILE.write_text("VK_USER_REFRESH_TOKEN=R\nVK_APP_ID=42\n", encoding="utf-8")
    tool.SECRET_FILE.write_text("THE_APP_SECRET", encoding="utf-8")
    monkeypatch.setattr(tool, "_post", lambda url, form, **kw: {"error": "invalid_grant",
                                                               "error_description": "refresh token expired"})
    assert tool.main(["refresh"]) == 1
    assert tool.ENV_FILE.read_text(encoding="utf-8").strip() == "VK_USER_REFRESH_TOKEN=R\nVK_APP_ID=42".strip()
    out = capsys.readouterr().out
    assert "invalid_grant" in out and "THE_APP_SECRET" not in out


def test_exchange_remembers_the_app_id_and_flow_for_later_refreshes(tool, monkeypatch):
    tool.SECRET_FILE.write_text("THE_APP_SECRET", encoding="utf-8")
    monkeypatch.setattr(tool, "_post", lambda url, form, **kw: {"access_token": "A", "refresh_token": "R"})
    assert tool.main(["exchange", "--app-id", "777", "--code", "C", "--flow", "vkid",
                      "--device-id", "D"]) == 0
    text = tool.ENV_FILE.read_text(encoding="utf-8")
    assert "VK_APP_ID=777" in text and "VK_AUTH_FLOW=vkid" in text
