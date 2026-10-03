#!/usr/bin/env python3
"""Operator tool: obtain a PERSONAL VK user token for video, without ever printing it.

Why this exists: `video.get` / `video.save` are user-scope methods — a community token is refused with
error 5 (measured on our own key), so inbound video and native video upload need a token that belongs to
a person. Getting one by hand (implicit flow, the dev.vk.com UI) is what the owner could not do, so this
tool covers the Authorization Code Flow as well:

    # 1. the app's secure key lives with the owner; it never travels through chat or argv:
    install -m 600 /dev/stdin ~/.hermes/vk-app-secret      # paste the key, Ctrl-D
    # 2. print the consent URL (no secrets in it for the implicit flow):
    python scripts/vk-user-token.py authorize --app-id 1234567 --flow implicit
    # 3. open it, approve, copy `access_token=…` (implicit) or `code=…` from the address bar:
    python scripts/vk-user-token.py store --token <access_token>                    # implicit
    python scripts/vk-user-token.py exchange --app-id 1234567 --code <code>         # code flow
    # 4. afterwards, any time:
    python scripts/vk-user-token.py status          # is the key accepted? does it have the video scope?
    python scripts/vk-user-token.py refresh --app-id 1234567   # when VK's short-lived token expires

Commands write tokens straight into the profile `.env` (mode 600) and print only lengths and VK's own
answers. A refusal writes nothing. The app secret is read from ``VK_APP_SECRET`` or the 0600 file — never
from a command-line argument, which would land in the shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

HERMES_HOME = pathlib.Path(os.environ.get("HERMES_HOME") or pathlib.Path.home() / ".hermes")
ENV_FILE = HERMES_HOME / ".env"
SECRET_FILE = HERMES_HOME / "vk-app-secret"
VK_ID_ENDPOINT = "https://id.vk.com/oauth2/auth"
LEGACY_TOKEN_ENDPOINT = "https://oauth.vk.com/access_token"
DEFAULT_REDIRECT = "https://oauth.vk.com/blank.html"
DEFAULT_API_VERSION = "5.199"


def redact(text: str, *secrets: str) -> str:
    """Same rule as the adapter's: credentials must not survive into output."""
    out = str(text or "")
    for secret in secrets:
        secret = str(secret or "")
        if len(secret) >= 8:
            out = out.replace(secret, "[REDACTED]")
    return re.sub(r"(access_token=)[^&\s]+", r"\1[REDACTED]", out)


def load_app_secret(path: "pathlib.Path | None" = None) -> str:
    """The app's secure key: from ``VK_APP_SECRET`` or the 0600 file. Never from an argument.

    The path is resolved per call, not bound as a default at import time — otherwise patching
    ``SECRET_FILE`` (tests, or a different profile home) would silently keep reading the old one.
    """
    path = pathlib.Path(path or SECRET_FILE)
    secret = str(os.environ.get("VK_APP_SECRET") or "").strip()
    if secret:
        return secret
    if not path.exists():
        raise SystemExit(
            f"Секрет приложения не найден.\n"
            f"  Вариант 1: экспортировать VK_APP_SECRET перед запуском.\n"
            f"  Вариант 2: сохранить в {path} с правами 600:\n"
            f"      install -m 600 /dev/stdin {path}   # вставить ключ, Ctrl-D")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        print(f"  ПРЕДУПРЕЖДЕНИЕ: {path} доступен не только владельцу (права {oct(mode)}) — "
              f"исправьте: chmod 600 {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit(f"{path} пуст")
    return value


def read_env_values(path: "pathlib.Path | None" = None) -> dict:
    """Parse the profile ``.env`` leniently (same rule as the tool's writer). Path resolved per call."""
    path = pathlib.Path(path or ENV_FILE)
    values: dict = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def build_authorize_url(app_id: str, *, flow: str, redirect_uri: str = DEFAULT_REDIRECT,
                        scope: str = "video", state: str = "hermes-vk") -> str:
    """The consent URL the owner opens. Contains no secret in any flow."""
    response_type = "token" if flow == "implicit" else "code"
    query = {
        "client_id": app_id, "display": "page", "redirect_uri": redirect_uri, "scope": scope,
        "response_type": response_type, "v": DEFAULT_API_VERSION,
    }
    if flow != "vkid":  # и implicit, и обычный code-flow живут на легаси-хосте
        return "https://oauth.vk.com/authorize?" + urllib.parse.urlencode(query)
    query["state"] = state
    return "https://id.vk.com/authorize?" + urllib.parse.urlencode(query)


def exchange_request(flow: str, *, app_id: str, code: str, redirect_uri: str, secret: str,
                     device_id: str = "", code_verifier: str = "") -> tuple:
    """(url, form) for the code→token exchange. VK's endpoints differ, and the parameters are measured:
    the legacy one answers ``client_secret is incorrect`` without a secret, the VK ID one answers
    ``device id is missing`` without a device id (it expects the VK ID SDK to supply one)."""
    if flow == "vkid":
        form = {
            "grant_type": "authorization_code", "client_id": app_id, "code": code,
            "redirect_uri": redirect_uri, "client_secret": secret, "device_id": device_id,
            "code_verifier": code_verifier, "state": "hermes-vk",
        }
        return VK_ID_ENDPOINT, {k: v for k, v in form.items() if v}
    form = {"client_id": app_id, "client_secret": secret, "redirect_uri": redirect_uri,
            "code": code, "v": DEFAULT_API_VERSION}
    return LEGACY_TOKEN_ENDPOINT, {k: v for k, v in form.items() if v}


def refresh_request(flow: str, *, app_id: str, refresh_token: str, secret: str, device_id: str = "") -> tuple:
    """(url, form) for the refresh_token grant — the step that makes a short-lived VK ID token usable."""
    if flow == "vkid":
        form = {"grant_type": "refresh_token", "client_id": app_id, "refresh_token": refresh_token,
                "client_secret": secret, "device_id": device_id}
        return VK_ID_ENDPOINT, {k: v for k, v in form.items() if v}
    form = {"grant_type": "refresh_token", "client_id": app_id, "client_secret": secret,
            "refresh_token": refresh_token, "v": DEFAULT_API_VERSION}
    return LEGACY_TOKEN_ENDPOINT, {k: v for k, v in form.items() if v}


def write_env_value(path: pathlib.Path, key: str, value: str) -> None:
    """Replace ``key=`` in the profile ``.env`` (or append it), keeping every other line intact.

    The value is written, never echoed: this is the whole point — a personal token must not land in a
    terminal history, a log, or a chat transcript.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{key}=") and not line.strip().startswith("#"):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _post(url: str, form: dict, *, timeout: float = 30.0) -> dict:
    data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "hermes-agent/vk-tool"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed hosts)
            return json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:  # VK answers 4xx with a JSON body
        raw = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"error": f"HTTP {exc.code}", "error_description": raw[:200]}


def _plugin_import():
    tests_dir = pathlib.Path(__file__).resolve().parents[1] / "tests"
    if not (tests_dir / "_paths.py").is_file():
        raise SystemExit("Рядом со скриптом нет tests/_paths.py — запускайте инструмент из каталога плагина "
                         "(он подключает runtime Hermes; для команд authorize/store он не нужен).")
    sys.path.insert(0, str(tests_dir))
    import _paths  # noqa: F401  (runtime + plugin registration)
    from vk.vk_api import VkApiError, VkClient  # noqa: F401
    return VkApiError, VkClient


async def verify(token: str, community_token: str, group_id: int) -> int:
    """Report what this token can do — user id, and whether video.get answers a permissions error."""
    VkApiError, VkClient = _plugin_import()
    client = VkClient(community_token, group_id=group_id, user_token=token)
    try:
        try:
            who = await client.call_as(token, "users.get", timeout=20) or []
            account = (who[0] if isinstance(who, list) and who else {}) or {}
            print(f"  users.get: ключ принят, id={account.get('id')}")
        except VkApiError as exc:
            print(f"  users.get: ОТКАЗ, код {exc.code} — {redact(exc.message, token)}")
            return 1
        try:
            await client.call_as(token, "video.get", videos="1_1", timeout=20)
            print("  video.get: вызов принят (пробная ссылка неважна — важно, что ключ не отвергнут)")
        except VkApiError as exc:
            print(f"  video.get: код {exc.code} — {redact(exc.message, token)}")
            if exc.code == 5:
                print("  → у ключа нет права «Видео»: при авторизации нужен scope=video")
                return 1
        return 0
    finally:
        await client.close()


def _store_answer(answer: dict, *, note: str = "") -> int:
    """Write what VK returned (never echoing it) and report the measurable facts: length, lifetime."""
    token = answer.get("access_token") or ""
    if not token:
        print("  ОТКАЗ: в ответе нет access_token")
        return 1
    write_env_value(ENV_FILE, "VK_USER_TOKEN", token)
    print(f"  токен получен (длина {len(token)}) и записан в {ENV_FILE}; значение не печаталось{note}")
    print(f"  срок жизни по ответу VK: {answer.get('expires_in', 'бессрочно/не указан')} с; "
          f"refresh_token: {'есть' if answer.get('refresh_token') else 'нет'}")
    if answer.get("refresh_token"):
        write_env_value(ENV_FILE, "VK_USER_REFRESH_TOKEN", answer["refresh_token"])
        print(f"  refresh_token записан в {ENV_FILE} — обновляйте командой refresh, без новой авторизации")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Получение личного токена VK для видео (без печати секретов)")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("authorize", help="напечатать ссылку согласия")
    auth.add_argument("--app-id", required=True)
    auth.add_argument("--flow", choices=["implicit", "code", "vkid"], default="implicit")
    auth.add_argument("--redirect", default=DEFAULT_REDIRECT)
    auth.add_argument("--scope", default="video")

    exch = sub.add_parser("exchange", help="обменять код на токен и записать его в .env")
    exch.add_argument("--app-id", required=True)
    exch.add_argument("--code", required=True)
    exch.add_argument("--flow", choices=["code", "vkid"], default="code")
    exch.add_argument("--redirect", default=DEFAULT_REDIRECT)
    exch.add_argument("--device-id", default="")
    exch.add_argument("--code-verifier", default="")
    exch.add_argument("--dry-run", action="store_true", help="только показать, что и куда уйдёт")

    store = sub.add_parser("store", help="сохранить готовый токен (implicit flow) в .env")
    store.add_argument("--token", required=True)

    ref = sub.add_parser("refresh", help="обновить токен по refresh_token из .env")
    ref.add_argument("--app-id", default="", help="по умолчанию берётся VK_APP_ID из .env")
    ref.add_argument("--flow", choices=["", "code", "vkid"], default="",
                     help="по умолчанию берётся VK_AUTH_FLOW из .env")
    ref.add_argument("--device-id", default="")
    ref.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="проверить токен из .env")
    args = parser.parse_args(argv)

    if args.command == "authorize":
        print(build_authorize_url(args.app_id, flow=args.flow, redirect_uri=args.redirect, scope=args.scope))
        print("\nПосле подтверждения скопируйте из адресной строки:")
        print("  implicit → значение после `access_token=` (до `&`) — это уже токен; сохраните его "
              "командой store")
        print("  code/vkid → значение после `code=` (до `&`) — его обменяет команда exchange")
        if args.flow == "vkid":
            print("  VK ID дополнительно требует device_id от SDK и code_verifier (PKCE): "
                  "без них обмен вернёт «device id is missing»")
        return 0

    if args.command == "store":
        write_env_value(ENV_FILE, "VK_USER_TOKEN", args.token)
        print(f"  VK_USER_TOKEN записан в {ENV_FILE} (длина {len(args.token)}, значение не печаталось)")
        return 0

    if args.command == "exchange":
        secret = load_app_secret()
        url, form = exchange_request(args.flow, app_id=args.app_id, code=args.code,
                                     redirect_uri=args.redirect, secret=secret,
                                     device_id=args.device_id, code_verifier=args.code_verifier)
        if args.dry_run:
            print(f"  POST {url}")
            print(f"  параметры: {', '.join(sorted(form))} (значения, включая секрет, не печатаются)")
            return 0
        print(f"  POST {url}")
        answer = _post(url, form)
        if "error" in answer:
            print(f"  ОТКАЗ: {answer.get('error')} — {redact(str(answer.get('error_description')), secret)}")
            return 1
        # Запоминаем, чем получен ключ: refresh и status больше не нужно настраивать вручную.
        write_env_value(ENV_FILE, "VK_APP_ID", args.app_id)
        write_env_value(ENV_FILE, "VK_AUTH_FLOW", args.flow)
        return _store_answer(answer)

    if args.command == "refresh":
        values = read_env_values()
        refresh_token = values.get("VK_USER_REFRESH_TOKEN", "")
        if not refresh_token:
            print("  В .env нет VK_USER_REFRESH_TOKEN — обновлять нечего. "
                  "Нужен новый код авторизации (authorize → exchange).")
            return 1
        app_id = args.app_id or values.get("VK_APP_ID", "")
        if not app_id:
            print("  Не указан id приложения: передайте --app-id или сохраните VK_APP_ID в .env")
            return 1
        flow = args.flow or values.get("VK_AUTH_FLOW", "code")
        secret = load_app_secret()
        url, form = refresh_request(flow, app_id=app_id, refresh_token=refresh_token, secret=secret,
                                    device_id=args.device_id)
        if args.dry_run:
            print(f"  POST {url}")
            print(f"  параметры: {', '.join(sorted(form))} (значения не печатаются)")
            return 0
        print(f"  POST {url}")
        answer = _post(url, form)
        if "error" in answer:
            print(f"  ОТКАЗ: {answer.get('error')} — {redact(str(answer.get('error_description')), secret)}")
            return 1
        return _store_answer(answer, note=" (обновление по refresh_token)")

    if args.command == "status":
        values = read_env_values()
        token = values.get("VK_USER_TOKEN", "")
        print(f"  VK_USER_TOKEN: длина {len(token)}")
        print(f"  VK_USER_REFRESH_TOKEN: {'есть' if values.get('VK_USER_REFRESH_TOKEN') else 'нет'} | "
              f"поток: {values.get('VK_AUTH_FLOW', 'неизвестен')}")
        if not token:
            return 1
        return asyncio.run(verify(token, values.get("VK_TOKEN", ""), int(values.get("VK_GROUP_ID") or 0)))

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
