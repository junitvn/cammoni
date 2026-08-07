"""
Telegram Mini App auth: validates the `initData` string sent by the frontend
(per https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
and checks the resulting Telegram user id against the same whitelist moni-bot uses
(ALLOWED_USERS env var, falling back to config/users.yaml).
"""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

import yaml
from fastapi import Header, HTTPException

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_AUTH_AGE_SECONDS = 24 * 60 * 60

# Lets you exercise the API from a plain browser during local dev, where there's
# no real Telegram WebApp to produce a signed initData. Only takes effect when
# explicitly enabled — never on by default.
DEV_MODE = os.getenv("WEBAPP_DEV_MODE", "false").lower() == "true"
DEV_USER_ID = os.getenv("WEBAPP_DEV_USER_ID", "0")
DEV_USER_NAME = os.getenv("WEBAPP_DEV_USER_NAME", "Dev")


def _load_allowed_users() -> set[int]:
    env_ids = os.getenv("ALLOWED_USERS", "")
    if env_ids:
        try:
            return {int(x.strip()) for x in env_ids.split(",") if x.strip()}
        except ValueError:
            pass
    try:
        with open("config/users.yaml") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and "users" in data:
            return {int(k) for k in (data["users"] or {}).keys()}
    except Exception:
        pass
    return set()


def _validate_init_data(init_data: str) -> dict:
    """Verify the HMAC signature and freshness of initData, return the decoded `user` dict."""
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN not configured")

    pairs = parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Missing hash in initData")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(401, "Invalid initData signature")

    try:
        auth_date = int(data.get("auth_date", 0))
    except ValueError:
        auth_date = 0
    if time.time() - auth_date > MAX_AUTH_AGE_SECONDS:
        raise HTTPException(401, "initData expired")

    user_raw = data.get("user")
    if not user_raw:
        raise HTTPException(401, "Missing user in initData")
    return json.loads(user_raw)


class CurrentUser:
    def __init__(self, id: int, name: str):
        self.id = id
        self.name = name


async def get_current_user(authorization: str = Header(default="")) -> CurrentUser:
    if not authorization.startswith("tma "):
        if DEV_MODE:
            return CurrentUser(id=int(DEV_USER_ID), name=DEV_USER_NAME)
        raise HTTPException(401, "Missing Telegram authorization header")

    init_data = authorization[len("tma "):]
    user = _validate_init_data(init_data)

    user_id = int(user["id"])
    allowed = _load_allowed_users()
    if allowed and user_id not in allowed:
        raise HTTPException(403, "User not allowed")

    name = user.get("first_name", "") or user.get("username", "") or str(user_id)
    return CurrentUser(id=user_id, name=name)
