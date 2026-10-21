"""
Change feed for the Mini App: a version string that moves on every sheet write,
whether it came from this process (sheets.py → changes.notify) or from the bot
(POST /api/internal/changed). The Mini App polls /api/changes and refetches its
queries when the version moves — cheap, since it never touches Google Sheets.
"""
import hmac
import itertools
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException

import changes
from webapp.auth import CurrentUser, get_current_user

router = APIRouter(prefix="/api", tags=["changes"])

# The boot id makes the version move across restarts too, so clients never
# mistake a fresh counter for "nothing changed".
_boot_id = uuid.uuid4().hex[:8]
_counter = itertools.count(1)
_version = f"{_boot_id}-0"


def _bump() -> None:
    global _version
    _version = f"{_boot_id}-{next(_counter)}"


changes.on_change(_bump)


@router.get("/changes")
async def get_changes(user: CurrentUser = Depends(get_current_user)):
    return {"version": _version}


@router.post("/internal/changed", status_code=204)
async def internal_changed(x_internal_token: str = Header("")):
    if not hmac.compare_digest(x_internal_token, changes.internal_token()):
        raise HTTPException(403, "Forbidden")
    _bump()
