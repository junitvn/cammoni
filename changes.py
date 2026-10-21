"""
"Data changed" signal shared by the bot and the webapp process.

sheets.py calls notify() after every write. Each process decides what that means:
- webapp: bumps the version that the Mini App polls via /api/changes (webapp/routes_changes.py)
- bot: install_webapp_notifier() forwards the signal to the webapp over the internal
  docker network, so edits made in Telegram show up in an open Mini App too.
"""
import asyncio
import hashlib
import logging
import os
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)

_listeners: list[Callable[[], None]] = []


def on_change(fn: Callable[[], None]) -> None:
    _listeners.append(fn)


def notify() -> None:
    for fn in _listeners:
        try:
            fn()
        except Exception:
            logger.exception("[changes] listener failed")


def internal_token() -> str:
    """Shared secret for bot → webapp calls, derived from BOT_TOKEN (both read the same .env)."""
    return hashlib.sha256(f"moni-internal:{os.getenv('BOT_TOKEN', '')}".encode()).hexdigest()


# ── Bot side: forward changes to the webapp ──────────────────────────────────

WEBAPP_INTERNAL_URL = os.getenv("WEBAPP_INTERNAL_URL", "http://webapp:8000").rstrip("/")
_DEBOUNCE_SECONDS = 0.5

_pending: Optional[asyncio.TimerHandle] = None
_client: Optional[httpx.AsyncClient] = None


async def _post_changed() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5)
    try:
        r = await _client.post(
            f"{WEBAPP_INTERNAL_URL}/api/internal/changed",
            headers={"X-Internal-Token": internal_token()},
        )
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"[changes] notify webapp failed: {e}")


def _schedule_post() -> None:
    # One edit is usually several sheet writes — coalesce them into a single POST.
    global _pending
    if _pending is not None:
        return
    loop = asyncio.get_running_loop()

    def _fire() -> None:
        global _pending
        _pending = None
        loop.create_task(_post_changed())

    _pending = loop.call_later(_DEBOUNCE_SECONDS, _fire)


def install_webapp_notifier() -> None:
    on_change(_schedule_post)
