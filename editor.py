"""
/sua and /xoa flows for editing or deleting past transactions.
"""
import logging
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)

from sheets import get_recent_transactions
from classifier import CATEGORY_INFO
from parser import format_amount, parse_amount_search, normalize_vn
from sheets import parse_ts as _parse_ts
import users as user_store


def _find_category_key(query: str) -> Optional[str]:
    q = normalize_vn(query.lower())
    for key, info in CATEGORY_INFO.items():
        if normalize_vn(info.get("name", "").lower()) == q or normalize_vn(key) == q:
            return key
    return None

logger = logging.getLogger(__name__)

PAGE_SIZE = 10

EDIT_LIST = 0


def _transaction_line(row: dict) -> str:
    ts = str(row.get("timestamp", ""))[:5]  # dd/mm
    tx_type = str(row.get("type", "chi"))
    type_icon = "💰" if tx_type == "thu" else "💸"
    try:
        amt = format_amount(int(float(str(row.get("amount", 0)))))
    except (ValueError, TypeError):
        amt = "?"
    cat = str(row.get("category", "khac"))
    info = CATEGORY_INFO.get(cat, {"emoji": "📦", "name": cat})
    desc = str(row.get("description", ""))
    name = user_store.get_name(row.get("user", ""))
    name_part = f" · {name}" if name else ""
    return f"{type_icon} {ts} · {amt} · {info['emoji']} {desc}{name_part}"


def _page_markup(rows: list, offset: int) -> InlineKeyboardMarkup:
    """Build inline keyboard: one button per item + Load thêm."""
    page = rows[offset:offset + PAGE_SIZE]
    total = len(rows)
    shown = min(offset + PAGE_SIZE, total)
    keyboard = []
    for row in page:
        tx_id = str(row["id"])
        keyboard.append([InlineKeyboardButton(
            _transaction_line(row),
            callback_data=f"editpick_{tx_id}_{offset}",
        )])
    if shown < total:
        keyboard.append([InlineKeyboardButton(
            f"⬇️ Xem thêm ({total - shown})",
            callback_data=f"editmore_{shown}",
        )])
    return InlineKeyboardMarkup(keyboard)


async def _send_edit_page(
    send_target,
    rows: list,
    offset: int,
    search_label: str = "",
    mode: str = "edit",  # kept for compat, unused
) -> None:
    total = len(rows)
    shown = min(offset + PAGE_SIZE, total)
    if search_label:
        header = f"🔍 *\"{search_label}\":* ({shown}/{total})"
    else:
        header = f"📋 *Các khoản gần đây:* ({shown}/{total})"
    await send_target.reply_text(
        header,
        reply_markup=_page_markup(rows, offset),
        parse_mode="Markdown",
    )


async def cmd_sua(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = " ".join(context.args).strip() if context.args else ""
    is_search = update.message.text.strip().startswith("/search")
    keyword = None
    amount_min = amount_max = None
    category = None
    search_label = ""

    if raw:
        cat_key = _find_category_key(raw)
        if cat_key:
            category = cat_key
            info = CATEGORY_INFO.get(cat_key, {"emoji": "📦", "name": raw})
            search_label = f"{info['emoji']} {info['name']}"
        else:
            amt_range = parse_amount_search(raw)
            if amt_range:
                amount_min, amount_max, search_label = amt_range
            else:
                keyword = raw
                search_label = raw

    rows = await get_recent_transactions(
        limit=100, keyword=keyword,
        amount_min=amount_min, amount_max=amount_max,
        category=category,
    )

    if not rows:
        msg = f"Không tìm thấy kết quả cho \"{search_label}\"." if search_label else "Không tìm thấy khoản nào."
        await update.message.reply_text(msg)
        return ConversationHandler.END

    context.user_data["edit_rows"] = {str(r["id"]): r for r in rows}
    context.user_data["edit_all_rows"] = rows
    context.user_data["edit_search_label"] = search_label
    mode = "search" if is_search else "edit"
    context.user_data["edit_mode"] = mode

    # Populate user_data for fix_cat/fix_date callbacks to work from search
    for row in rows:
        tid = str(row.get("id", ""))
        if not tid:
            continue
        context.user_data[f"tx_type_{tid}"] = row.get("type", "chi")
        context.user_data[f"tx_desc_{tid}"] = row.get("description", "")
        ts = _parse_ts(str(row.get("timestamp", "")))
        if ts:
            context.user_data[f"tx_ts_{tid}"] = ts

    await _send_edit_page(update.message, rows, offset=0, search_label=search_label, mode=mode)
    return EDIT_LIST


async def edit_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("editmore_"):
        offset = int(data.replace("editmore_", ""))
        rows = context.user_data.get("edit_all_rows", [])
        search_label = context.user_data.get("edit_search_label", "")
        await _send_edit_page(query.message, rows, offset, search_label=search_label)
        return EDIT_LIST

    if data.startswith("editback_"):
        # Restore the list page — offset encoded in callback data
        offset = int(data.replace("editback_", ""))
        rows = context.user_data.get("edit_all_rows", [])
        await query.edit_message_reply_markup(reply_markup=_page_markup(rows, offset))
        return EDIT_LIST

    if data.startswith("editpick_"):
        # format: editpick_{tx_id}_{page_offset}
        parts = data.split("_")
        tx_id = parts[1]
        page_offset = int(parts[2]) if len(parts) > 2 else 0
        row = context.user_data.get("edit_rows", {}).get(tx_id, {})

        # Populate user_data so standalone callbacks work
        context.user_data[f"tx_type_{tx_id}"] = row.get("type", "chi")
        context.user_data[f"tx_desc_{tx_id}"] = row.get("description", "")
        ts = _parse_ts(str(row.get("timestamp", "")))
        if ts:
            context.user_data[f"tx_ts_{tx_id}"] = ts

        excl = str(row.get("excluded", "")).strip().upper() == "Y"
        excl_label = "✅ Tính lại" if excl else "🚫 Không tính"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Sửa", callback_data=f"edm_enter_{tx_id}")],
            [
                InlineKeyboardButton(excl_label, callback_data=f"qexcl_{tx_id}"),
                InlineKeyboardButton("🗑️ Xóa", callback_data=f"qdel_{tx_id}"),
            ],
            [InlineKeyboardButton("❌ Hủy", callback_data=f"editback_{page_offset}")],
        ])
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return EDIT_LIST

    return EDIT_LIST


def get_editor_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("edit", cmd_sua),
            CommandHandler("search", cmd_sua),
            MessageHandler(filters.Regex(r"^✏️ Sửa/Xóa$"), cmd_sua),
        ],
        states={
            EDIT_LIST: [
                CallbackQueryHandler(edit_list_callback, pattern="^(noop|editmore_|editpick_|editback_)")
            ],
        },
        fallbacks=[CommandHandler("edit", cmd_sua)],
    )
