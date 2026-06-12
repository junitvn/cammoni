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

from sheets import (
    get_recent_transactions, update_transaction_field,
    delete_transaction, parse_ts, format_ts, upsert_config_mapping,
)
from classifier import CATEGORY_INFO, CATEGORY_KEYS, INCOME_CATEGORY_KEYS, EXPENSE_CATEGORY_KEYS
from parser import format_amount, parse_amount_search, format_amount_range, normalize_vn
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

# Conversation states
(
    EDIT_LIST,
    EDIT_CHOOSE_FIELD,
    EDIT_ENTER_VALUE,
    EDIT_CHOOSE_CATEGORY,
    DELETE_CONFIRM,
) = range(5)


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
            f"⬇️ Load thêm ({total - shown} còn lại)",
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
            [
                InlineKeyboardButton("🗑️ Xóa", callback_data=f"qdel_{tx_id}"),
                InlineKeyboardButton("✏️ Sửa", callback_data=f"editfields_{tx_id}_{page_offset}"),
            ],
            [
                InlineKeyboardButton(excl_label, callback_data=f"qexcl_{tx_id}"),
                InlineKeyboardButton("❌ Hủy", callback_data=f"editback_{page_offset}"),
            ],
        ])
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return EDIT_LIST

    if data.startswith("editfields_"):
        parts = data.split("_")
        tx_id = parts[1]
        page_offset = int(parts[2]) if len(parts) > 2 else 0
        context.user_data["edit_tx_id"] = tx_id
        context.user_data["edit_return_offset"] = page_offset
        row = context.user_data.get("edit_rows", {}).get(tx_id, {})
        desc = str(row.get("description", ""))[:25]
        header = f'✏️ *{desc}*\nChọn trường cần sửa:' if desc else "✏️ Chọn trường cần sửa:"
        keyboard = [
            [
                InlineKeyboardButton("💰 Số tiền", callback_data="field_amount"),
                InlineKeyboardButton("🏷 Phân loại", callback_data="field_category"),
            ],
            [
                InlineKeyboardButton("📅 Ngày", callback_data="field_timestamp"),
                InlineKeyboardButton("📝 Mô tả", callback_data="field_description"),
            ],
            [InlineKeyboardButton("👤 Người", callback_data="field_user_name")],
            [InlineKeyboardButton("❌ Hủy", callback_data="field_cancel")],
        ]
        await query.edit_message_text(
            header,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return EDIT_CHOOSE_FIELD

    if data.startswith("edit_"):
        tx_id = data.replace("edit_", "")
        context.user_data["edit_tx_id"] = tx_id
        keyboard = [
            [
                InlineKeyboardButton("💰 Số tiền", callback_data="field_amount"),
                InlineKeyboardButton("🏷 Phân loại", callback_data="field_category"),
            ],
            [
                InlineKeyboardButton("📅 Ngày", callback_data="field_timestamp"),
                InlineKeyboardButton("📝 Mô tả", callback_data="field_description"),
            ],
            [InlineKeyboardButton("👤 Người", callback_data="field_user_name")],
            [InlineKeyboardButton("❌ Hủy", callback_data="field_cancel")],
        ]
        await query.edit_message_text(
            "✏️ Chọn trường cần sửa:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return EDIT_CHOOSE_FIELD

    elif data.startswith("del_"):
        tx_id = data.replace("del_", "")
        context.user_data["del_tx_id"] = tx_id
        row = context.user_data.get("edit_rows", {}).get(tx_id, {})
        label = _transaction_line(row)
        keyboard = [[
            InlineKeyboardButton("✅ Xóa", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ Hủy", callback_data="cancel_delete"),
        ]]
        await query.edit_message_text(
            f"Xóa khoản:\n*{label}*\nBạn chắc chắn?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return DELETE_CONFIRM

    return EDIT_LIST


async def edit_choose_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.replace("field_", "")
    context.user_data["edit_field"] = field

    tx_id = context.user_data.get("edit_tx_id", "")
    row = context.user_data.get("edit_rows", {}).get(tx_id, {})

    if field == "cancel":
        offset = context.user_data.get("edit_return_offset", 0)
        rows = context.user_data.get("edit_all_rows", [])
        search_label = context.user_data.get("edit_search_label", "")
        total = len(rows)
        shown = min(offset + PAGE_SIZE, total)
        header = f"🔍 *\"{search_label}\":* ({shown}/{total})" if search_label else f"📋 *Các khoản gần đây:* ({shown}/{total})"
        await query.edit_message_text(header, reply_markup=_page_markup(rows, offset), parse_mode="Markdown")
        return EDIT_LIST

    if field == "category":
        cur_cat = str(row.get("category", "khac"))
        cur_info = CATEGORY_INFO.get(cur_cat, {"emoji": "📦", "name": cur_cat})
        tx_type = str(row.get("type", "chi"))
        cat_keys = INCOME_CATEGORY_KEYS if tx_type == "thu" else EXPENSE_CATEGORY_KEYS
        row_size = 2 if tx_type == "thu" else 3
        keyboard = []
        btn_row = []
        for key in cat_keys:
            info = CATEGORY_INFO[key]
            label = f"✓ {info['emoji']} {info['name']}" if key == cur_cat else f"{info['emoji']} {info['name']}"
            btn_row.append(InlineKeyboardButton(label, callback_data=f"setcat_{key}"))
            if len(btn_row) == row_size:
                keyboard.append(btn_row)
                btn_row = []
        if btn_row:
            keyboard.append(btn_row)
        await query.edit_message_text(
            f"Phân loại hiện tại: {cur_info['emoji']} *{cur_info['name']}*\nChọn mục mới:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return EDIT_CHOOSE_CATEGORY

    # Build prompt with current value
    if field == "amount":
        try:
            cur = format_amount(int(float(str(row.get("amount", 0)))))
        except (ValueError, TypeError):
            cur = "?"
        prompt = f"Số tiền hiện tại: *{cur}*\n\nNhập số tiền mới (VD: `150` = 150.000đ):"
    elif field == "description":
        cur = str(row.get("description", "")) or "(trống)"
        prompt = f"Mô tả hiện tại: *{cur}*\n\nNhập mô tả mới:"
    elif field == "timestamp":
        cur = str(row.get("timestamp", ""))[:16]
        prompt = f"Ngày hiện tại: *{cur}*\n\nNhập ngày mới (VD: `15/06` hoặc `15/06 14:30`):"
    elif field == "user_name":
        cur = str(row.get("user_name", "")) or "(trống)"
        prompt = f"Người hiện tại: *{cur}*\n\nNhập tên mới:"
    else:
        prompt = "Nhập giá trị mới:"

    await query.edit_message_text(prompt, parse_mode="Markdown")
    return EDIT_ENTER_VALUE


async def edit_enter_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    field = context.user_data.get("edit_field")
    tx_id = context.user_data.get("edit_tx_id")

    if field == "amount":
        num = text.replace(".", "").replace(",", "")
        try:
            value = int(num) * 1000
        except ValueError:
            await update.message.reply_text("Số không hợp lệ, nhập lại:")
            return EDIT_ENTER_VALUE
    elif field == "timestamp":
        dt = parse_ts(text)
        if not dt:
            await update.message.reply_text("Không nhận ra ngày, nhập lại (VD: `15/06` hoặc `15/06 14:30`):", parse_mode="Markdown")
            return EDIT_ENTER_VALUE
        value = format_ts(dt)
    elif field == "user_name":
        value = text.strip()
    else:
        value = text

    ok = await update_transaction_field(tx_id, field, value)
    if ok:
        await update.message.reply_text("✅ Đã cập nhật!")
    else:
        await update.message.reply_text("❌ Không tìm thấy khoản để sửa.")
    return ConversationHandler.END


async def edit_choose_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    new_cat = query.data.replace("setcat_", "")
    tx_id = context.user_data.get("edit_tx_id")
    row = context.user_data.get("edit_rows", {}).get(tx_id, {})

    ok = await update_transaction_field(tx_id, "category", new_cat)
    info = CATEGORY_INFO.get(new_cat, {"emoji": "📦", "name": new_cat})
    if ok:
        desc = str(row.get("description", ""))
        if desc:
            import asyncio
            asyncio.create_task(upsert_config_mapping(desc, new_cat))
        await query.edit_message_text(f"✅ Đã cập nhật: {info['emoji']} {info['name']}")
    else:
        await query.edit_message_text("❌ Không tìm thấy khoản để sửa.")
    return ConversationHandler.END


async def delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_delete":
        tx_id = context.user_data.get("del_tx_id")
        ok = await delete_transaction(tx_id)
        if ok:
            await query.edit_message_text("🗑 Đã xóa khoản.")
        else:
            await query.edit_message_text("❌ Không tìm thấy khoản để xóa.")
    else:
        await query.edit_message_text("Đã hủy.")

    return ConversationHandler.END


def get_editor_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("edit", cmd_sua),
            CommandHandler("search", cmd_sua),
            MessageHandler(filters.Regex(r"^✏️ Sửa/Xóa$"), cmd_sua),
        ],
        states={
            EDIT_LIST: [
                CallbackQueryHandler(edit_list_callback, pattern="^(edit_|del_|noop|editmore_|editpick_|editback_|editfields_)")
            ],
            EDIT_CHOOSE_FIELD: [
                CallbackQueryHandler(edit_choose_field_callback, pattern="^field_")
            ],
            EDIT_ENTER_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_enter_value)
            ],
            EDIT_CHOOSE_CATEGORY: [
                CallbackQueryHandler(edit_choose_category_callback, pattern="^setcat_")
            ],
            DELETE_CONFIRM: [
                CallbackQueryHandler(delete_confirm_callback, pattern="^(confirm_delete|cancel_delete)")
            ],
        },
        fallbacks=[CommandHandler("edit", cmd_sua)],
    )
