"""
The bot's confirmation message for a transaction ("✅ 21/10 Chi tiêu (Lâm): ...").

The message's chat_id/message_id are stored on the sheet row, so whichever side edits
the transaction (bot or Mini App) can re-render the message from the row. Edits go
through the Bot API with httpx directly rather than python-telegram-bot, so the webapp
process — which has BOT_TOKEN but no telegram Application — can use this too.
"""
import logging
import os
from typing import Optional

import httpx

from classifier import category_display
from parser import format_amount
from sheets import get_transaction_by_id, parse_ts

logger = logging.getLogger(__name__)

MINI_APP_URL = os.getenv("MINI_APP_URL", "https://cammoni-app.vercel.app")

_client: Optional[httpx.AsyncClient] = None

# Telegram answers these when the message can't/needn't change — nothing to do.
_IGNORED_ERRORS = ("message is not modified", "message to edit not found", "message can't be edited")


def format_text(row: dict, deleted: bool = False) -> str:
    ts = parse_ts(str(row.get("timestamp", "")))
    date_str = f"{ts.day}/{ts.month}" if ts else str(row.get("timestamp", ""))[:5]
    type_label = "Thu nhập" if row.get("type") == "thu" else "Chi tiêu"
    name = str(row.get("user_name", "") or "")
    name_part = f" ({name})" if name else ""
    try:
        amt_str = format_amount(int(float(str(row.get("amount", 0)))))
    except (ValueError, TypeError):
        amt_str = "?"
    head = "🗑️ Đã xóa" if deleted else "✅"
    lines = [
        f"{head} {date_str} {type_label}{name_part}:",
        f"  {category_display(str(row.get('category', 'khac')))} · {row.get('description', '')}",
        f"  💰 {amt_str}",
    ]
    if not deleted and str(row.get("excluded", "")).strip().upper() == "Y":
        lines.append("🚫 Không tính vào ngân sách")
    return "\n".join(lines)


def reply_markup(tx_id: str) -> Optional[dict]:
    """Bot API JSON form of bot.py's _action_sheet_kb (see there for why only https)."""
    if not MINI_APP_URL.startswith("https://"):
        return None
    return {"inline_keyboard": [[
        {"text": "✏️ Sửa", "web_app": {"url": f"{MINI_APP_URL}/?tx={tx_id}"}},
    ]]}


def _message_ref(row: dict) -> Optional[tuple[int, int]]:
    try:
        chat_id, message_id = int(row.get("chat_id") or 0), int(row.get("message_id") or 0)
    except (ValueError, TypeError):
        return None
    return (chat_id, message_id) if chat_id and message_id else None


async def _edit(row: dict, deleted: bool, skip_message_id: Optional[int]) -> None:
    ref = _message_ref(row)
    if not ref:
        return  # created from the Mini App, or before message refs were stored
    chat_id, message_id = ref
    if skip_message_id is not None and message_id == skip_message_id:
        return  # the caller is already editing this very message itself
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        return

    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": format_text(row, deleted)}
    markup = None if deleted else reply_markup(str(row.get("id", "")))
    if markup:
        payload["reply_markup"] = markup

    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10)
    try:
        r = await _client.post(f"https://api.telegram.org/bot{token}/editMessageText", json=payload)
        data = r.json()
        if not data.get("ok"):
            desc = str(data.get("description", ""))
            if not any(e in desc.lower() for e in _IGNORED_ERRORS):
                logger.warning(f"[tx_message] edit failed for tx={row.get('id')}: {desc}")
    except Exception as e:
        logger.warning(f"[tx_message] edit failed for tx={row.get('id')}: {e}")


async def sync(row: dict, skip_message_id: Optional[int] = None) -> None:
    """Re-render the confirmation message from the (already updated) row."""
    await _edit(row, deleted=False, skip_message_id=skip_message_id)


async def sync_by_id(tx_id: str, skip_message_id: Optional[int] = None) -> None:
    result = await get_transaction_by_id(tx_id)
    if result:
        await sync(result[1], skip_message_id=skip_message_id)


async def mark_deleted(row: dict, skip_message_id: Optional[int] = None) -> None:
    await _edit(row, deleted=True, skip_message_id=skip_message_id)
