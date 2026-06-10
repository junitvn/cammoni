"""
/sua and /xoa flows for editing or deleting past transactions.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)

from sheets import (
    get_recent_transactions, update_transaction_field,
    delete_transaction, parse_ts, format_ts,
)
from classifier import CATEGORY_INFO, CATEGORY_KEYS
from parser import format_amount

logger = logging.getLogger(__name__)

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
    try:
        amt = format_amount(int(float(str(row.get("amount", 0)))))
    except (ValueError, TypeError):
        amt = "?"
    cat = str(row.get("category", "khac"))
    info = CATEGORY_INFO.get(cat, {"emoji": "📦", "name": cat})
    desc = str(row.get("description", ""))
    return f"{ts} {amt} {info['emoji']} {desc}"


async def cmd_sua(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyword = " ".join(context.args) if context.args else None
    user_id = str(update.effective_user.id)

    rows = await get_recent_transactions(limit=10, keyword=keyword)

    if not rows:
        await update.message.reply_text(
            "Không tìm thấy khoản nào." + (" Thử từ khóa khác." if keyword else "")
        )
        return ConversationHandler.END

    context.user_data["edit_rows"] = {str(r["id"]): r for r in rows}

    keyboard = []
    for row in rows:
        tx_id = str(row["id"])
        label = _transaction_line(row)
        keyboard.append([
            InlineKeyboardButton(label, callback_data=f"noop"),
            InlineKeyboardButton("✏️", callback_data=f"edit_{tx_id}"),
            InlineKeyboardButton("🗑", callback_data=f"del_{tx_id}"),
        ])

    await update.message.reply_text(
        "📋 *Các khoản gần đây:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return EDIT_LIST


async def edit_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("edit_"):
        tx_id = data.replace("edit_", "")
        context.user_data["edit_tx_id"] = tx_id
        keyboard = [
            [
                InlineKeyboardButton("💰 Số tiền", callback_data="field_amount"),
                InlineKeyboardButton("🏷 Phân loại", callback_data="field_category"),
            ],
            [
                InlineKeyboardButton("📝 Mô tả", callback_data="field_description"),
                InlineKeyboardButton("🕐 Ngày giờ", callback_data="field_timestamp"),
            ],
        ]
        await query.edit_message_text(
            "Chọn trường cần sửa:",
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

    if field == "category":
        cur_cat = str(row.get("category", "khac"))
        cur_info = CATEGORY_INFO.get(cur_cat, {"emoji": "📦", "name": cur_cat})
        keyboard = []
        btn_row = []
        for key in CATEGORY_KEYS:
            info = CATEGORY_INFO[key]
            label = f"✓ {info['emoji']} {info['name']}" if key == cur_cat else f"{info['emoji']} {info['name']}"
            btn_row.append(InlineKeyboardButton(label, callback_data=f"setcat_{key}"))
            if len(btn_row) == 3:
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
        prompt = f"Số tiền hiện tại: *{cur}*\nNhập số tiền mới (nghìn đồng, VD: `150` = 150.000đ):"
    elif field == "description":
        cur = str(row.get("description", ""))
        prompt = f"Mô tả hiện tại: *{cur}*\nNhập mô tả mới:"
    elif field == "timestamp":
        cur = str(row.get("timestamp", ""))
        prompt = f"Ngày giờ hiện tại: *{cur}*\nNhập ngày giờ mới (VD: `15/06` hoặc `15/06 14:30`):"
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
            await update.message.reply_text("Không nhận ra ngày giờ, nhập lại (VD: `15/06 14:30`):", parse_mode="Markdown")
            return EDIT_ENTER_VALUE
        value = format_ts(dt)
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

    ok = await update_transaction_field(tx_id, "category", new_cat)
    info = CATEGORY_INFO.get(new_cat, {"emoji": "📦", "name": new_cat})
    if ok:
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
            CommandHandler("sua", cmd_sua),
            MessageHandler(filters.Regex(r"^✏️ Sửa/Xóa$"), cmd_sua),
        ],
        states={
            EDIT_LIST: [
                CallbackQueryHandler(edit_list_callback, pattern="^(edit_|del_|noop)")
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
        fallbacks=[CommandHandler("sua", cmd_sua)],
    )
