"""
Budget management: set/view budgets per category or total.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from sheets import set_budget, get_budgets
from classifier import CATEGORY_INFO, CATEGORY_KEYS
from parser import format_amount

BUDGET_SET_SCOPE, BUDGET_SET_AMOUNT = range(2)


async def _budget_summary_text() -> str:
    from stats import compute_stats
    stats = await compute_stats("month")
    budgets = stats.get("budgets", {})
    if not budgets:
        return ""
    lines = []
    for scope, b in budgets.items():
        label = "Tổng chi" if scope == "chung" else CATEGORY_INFO.get(scope, {}).get("name", scope)
        pct = b["pct"]
        icon = "🔴" if pct >= 100 else ("⚠️" if pct >= 80 else "✅")
        lines.append(f"{icon} {label}: {format_amount(b['used'])} / {format_amount(b['limit'])} ({pct:.0f}%)")
    return "\n".join(lines)


async def budget_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("⚙️ Đặt ngân sách chung", callback_data="budget_set_chung")],
        [InlineKeyboardButton("⚙️ Đặt ngân sách từng mục", callback_data="budget_set_cat")],
        [InlineKeyboardButton("📊 Xem tình hình ngân sách", callback_data="budget_view")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    summary = await _budget_summary_text()
    msg = f"💰 *Quản lý ngân sách*\n\n{summary}\n\nChọn thao tác:" if summary else "💰 *Quản lý ngân sách*\n\nChọn thao tác:"
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")


async def budget_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "budget_set_chung":
        context.user_data["budget_scope"] = "chung"
        await query.edit_message_text(
            "💰 *Ngân sách tổng*\n\nNhập hạn mức chi tháng (đơn vị: nghìn đồng)\nVí dụ: `15000` = 15.000.000đ/tháng",
            parse_mode="Markdown",
        )
        return BUDGET_SET_AMOUNT

    elif data == "budget_set_cat":
        keyboard = []
        row = []
        for i, key in enumerate(CATEGORY_KEYS):
            info = CATEGORY_INFO[key]
            row.append(InlineKeyboardButton(
                f"{info['emoji']} {info['name']}",
                callback_data=f"budget_scope_{key}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        await query.edit_message_text(
            "Chọn mục cần đặt ngân sách:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return BUDGET_SET_SCOPE

    elif data == "budget_view":
        await _show_budget_status(query)
        return ConversationHandler.END

    elif data.startswith("budget_scope_"):
        scope = data.replace("budget_scope_", "")
        context.user_data["budget_scope"] = scope
        info = CATEGORY_INFO.get(scope, {"emoji": "📦", "name": scope})
        await query.edit_message_text(
            f"{info['emoji']} *{info['name']}*\n\nNhập hạn mức chi tháng (đơn vị: nghìn đồng)\nVí dụ: `3000` = 3.000.000đ/tháng",
            parse_mode="Markdown",
        )
        return BUDGET_SET_AMOUNT

    return ConversationHandler.END


async def budget_receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(".", "").replace(",", "")
    try:
        amount = int(text) * 1000
    except ValueError:
        await update.message.reply_text("Số không hợp lệ. Nhập lại (VD: `3000`):", parse_mode="Markdown")
        return BUDGET_SET_AMOUNT

    scope = context.user_data.get("budget_scope", "chung")
    await set_budget(scope, amount)

    label = "Tổng chi" if scope == "chung" else CATEGORY_INFO.get(scope, {}).get("name", scope)
    await update.message.reply_text(
        f"✅ Đã đặt ngân sách *{label}*: {format_amount(amount)}/tháng",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def _show_budget_status(query_or_update) -> None:
    from stats import compute_stats
    stats = await compute_stats("month")

    budgets = stats.get("budgets", {})
    if not budgets:
        text = "Chưa đặt ngân sách nào. Dùng /budget để đặt."
    else:
        lines = ["💰 *Tình hình ngân sách tháng này*\n"]
        for scope, b in budgets.items():
            label = "Tổng chi" if scope == "chung" else CATEGORY_INFO.get(scope, {}).get("name", scope)
            pct = b["pct"]
            bar_filled = int(pct / 10)
            bar = "█" * bar_filled + "░" * (10 - bar_filled)
            icon = "🔴" if pct >= 100 else ("⚠️" if pct >= 80 else "✅")
            lines.append(
                f"{icon} *{label}*\n"
                f"  {bar} {pct:.0f}%\n"
                f"  {format_amount(b['used'])} / {format_amount(b['limit'])}"
            )
        text = "\n\n".join(lines)

    if hasattr(query_or_update, "edit_message_text"):
        await query_or_update.edit_message_text(text, parse_mode="Markdown")
    else:
        await query_or_update.message.reply_text(text, parse_mode="Markdown")


def get_budget_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("budget", budget_menu),
            CallbackQueryHandler(budget_callback, pattern="^budget_"),
        ],
        states={
            BUDGET_SET_SCOPE: [
                CallbackQueryHandler(budget_callback, pattern="^budget_"),
            ],
            BUDGET_SET_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, budget_receive_amount),
            ],
        },
        fallbacks=[CommandHandler("budget", budget_menu)],
    )
