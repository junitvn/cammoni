"""
Main bot entrypoint.
"""
import asyncio
import logging
import os
import uuid
from datetime import time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

from parser import parse_message, format_amount
from classifier import (
    classify, category_display, CATEGORY_INFO, CATEGORY_KEYS,
    INCOME_CATEGORY_KEYS, EXPENSE_CATEGORY_KEYS,
)
from sheets import (
    add_transaction, update_transaction_field, now_vn, init_sheets,
    upsert_config_mapping,
)
from stats import compute_stats, format_stats_text, check_budget_warning
from charts import generate_charts
from editor import get_editor_conversation_handler
from budget import get_budget_conversation_handler, budget_menu

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# ── Whitelist ─────────────────────────────────────────────────────────────────

def _load_allowed_users() -> set[int]:
    # From env (comma-separated)
    env_ids = os.getenv("ALLOWED_USERS", "")
    if env_ids:
        try:
            return {int(x.strip()) for x in env_ids.split(",") if x.strip()}
        except ValueError:
            pass

    # From users.yaml
    try:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load(
            (Path(__file__).parent / "config" / "users.yaml").read_text()
        )
        ids = cfg.get("allowed_users") or []
        return set(ids)
    except Exception:
        return set()


ALLOWED_USERS: set[int] = _load_allowed_users()


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True  # no whitelist configured → allow all
    return user_id in ALLOWED_USERS


# ── Keyboards ─────────────────────────────────────────────────────────────────

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Hôm nay"), KeyboardButton("📅 Tuần này")],
        [KeyboardButton("🗓 Tháng này"), KeyboardButton("📆 Khoảng tg")],
        [KeyboardButton("💰 Ngân sách"), KeyboardButton("✏️ Sửa/Xóa")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

def make_category_keyboard(tx_id: str, tx_type: str = "chi") -> InlineKeyboardMarkup:
    keys = INCOME_CATEGORY_KEYS if tx_type == "thu" else EXPENSE_CATEGORY_KEYS
    buttons = [
        InlineKeyboardButton(
            f"{CATEGORY_INFO[k]['emoji']} {CATEGORY_INFO[k]['name']}",
            callback_data=f"recat_{tx_id}_{k}",
        )
        for k in keys
    ]
    row_size = 2 if tx_type == "thu" else 3
    rows = [buttons[i:i + row_size] for i in range(0, len(buttons), row_size)]
    return InlineKeyboardMarkup(rows)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 Chào mừng đến với *Moni Bot*!\n\n"
        "Nhắn tin để ghi chi tiêu:\n"
        "  `39 cơm trưa` → chi 39.000đ\n"
        "  `.500 lương` → thu 500.000đ\n"
        "  `+200 mẹ cho` → thu 200.000đ\n\n"
        "Dùng menu bên dưới để xem thống kê.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("📊 Menu thống kê:", reply_markup=MAIN_KEYBOARD)


async def handle_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any text message that looks like a transaction."""
    if not is_allowed(update.effective_user.id):
        return

    text = update.message.text
    result = parse_message(text)
    if not result:
        return  # not a transaction, ignore

    user_id = str(update.effective_user.id)

    # Classify
    cat_key, ai_used = await classify(result.description, result.amount, result.tx_type)

    cat_disp = category_display(cat_key)
    amt_str = format_amount(result.amount)
    tx_id = str(uuid.uuid4())[:8]

    # Store for later use in recat handler
    context.user_data[f"tx_desc_{tx_id}"] = result.description
    context.user_data[f"tx_type_{tx_id}"] = result.tx_type

    if result.tx_type == "thu":
        reply_text = (
            f"💰 Thu nhập: {amt_str}\n"
            f"{cat_disp} — \"{result.description}\""
        )
    else:
        reply_text = (
            f"✅ Đã ghi: {amt_str}\n"
            f"{cat_disp} — \"{result.description}\""
        )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Sửa phân loại", callback_data=f"fix_cat_{tx_id}")
    ]])
    await update.message.reply_text(reply_text, reply_markup=keyboard)

    # Write to sheet in background (async, no executor needed)
    async def _save():
        try:
            await add_transaction(
                user_id=user_id,
                tx_type=result.tx_type,
                amount=result.amount,
                category=cat_key,
                description=result.description,
                auto_classified=True,
                tx_id=tx_id,
            )
            logger.info(f"[bot] saved tx_id={tx_id}")
            if result.tx_type == "chi":
                warning = await check_budget_warning(cat_key, result.amount)
                if warning:
                    await update.message.reply_text(warning)
        except Exception as e:
            logger.exception(f"[bot] _save FAILED: {e}")
            await update.message.reply_text(f"⚠️ Lưu sheet thất bại: {e}")

    asyncio.create_task(_save())


async def handle_fix_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show category selection buttons when user taps 'Sửa phân loại'."""
    query = update.callback_query
    await query.answer()
    tx_id = query.data.replace("fix_cat_", "")
    context.user_data["fix_cat_tx_id"] = tx_id

    tx_type = context.user_data.get(f"tx_type_{tx_id}", "chi")
    await query.edit_message_reply_markup(reply_markup=make_category_keyboard(tx_id, tx_type))


async def handle_recat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle category re-selection: recat_{tx_id}_{cat_key}."""
    query = update.callback_query
    await query.answer()
    # data format: "recat_{8-char tx_id}_{cat_key}"
    without_prefix = query.data[len("recat_"):]  # "{tx_id}_{cat_key}"
    tx_id = without_prefix[:8]
    cat_key = without_prefix[9:]  # skip the underscore after tx_id

    await update_transaction_field(tx_id, "category", cat_key)

    # Save learned mapping to Config sheet
    desc = context.user_data.get(f"tx_desc_{tx_id}", "")
    if desc:
        asyncio.create_task(upsert_config_mapping(desc, cat_key))

    cat_disp = category_display(cat_key)
    old_text = query.message.text
    first_line = old_text.split("\n")[0]
    await query.edit_message_text(
        f"{first_line}\n{cat_disp} ✓ (đã cập nhật)",
    )


# ── Stats keyboard handlers ───────────────────────────────────────────────────

async def handle_stats_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    text = update.message.text.strip()
    period_map = {
        "📊 Hôm nay": "today",
        "📅 Tuần này": "week",
        "🗓 Tháng này": "month",
    }

    if text in period_map:
        await _send_stats(update, context, period_map[text])
    elif text == "📆 Khoảng tg":
        context.user_data["waiting_custom_range"] = True
        await update.message.reply_text(
            "Nhập khoảng thời gian (VD: `01/06 - 09/06` hoặc `01/06/2025 - 09/06/2025`):",
            parse_mode="Markdown",
        )
    elif text == "💰 Ngân sách":
        await budget_menu(update, context)
    elif context.user_data.get("waiting_custom_range"):
        context.user_data["waiting_custom_range"] = False
        await _handle_custom_range(update, context, text)


async def _handle_custom_range(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    from sheets import parse_ts
    import re
    # Expect "DD/MM - DD/MM" or "DD/MM/YYYY - DD/MM/YYYY"
    parts = re.split(r"\s*[-–]\s*", text.strip())
    if len(parts) != 2:
        await update.message.reply_text("Không nhận ra định dạng. Thử: `01/06 - 09/06`", parse_mode="Markdown")
        return

    start = parse_ts(parts[0].strip())
    end = parse_ts(parts[1].strip())
    if not start or not end:
        await update.message.reply_text("Không nhận ra ngày. Thử: `01/06 - 09/06`", parse_mode="Markdown")
        return

    if end < start:
        start, end = end, start

    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    end = end.replace(hour=23, minute=59, second=59)

    user_id = str(update.effective_user.id)
    await _send_stats(update, context, "custom", custom_start=start, custom_end=end)


async def _send_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    period: str,
    user_id_filter: str = None,
    custom_start=None,
    custom_end=None,
) -> None:
    uid = update.effective_user.id
    scope_key = f"stats_scope_{uid}"
    current_scope = context.user_data.get(scope_key, "all")  # "all" or "me"

    filter_uid = str(uid) if current_scope == "me" else None

    msg = await update.message.reply_text("⏳ Đang tải...")

    try:
        stats = await compute_stats(
            period,
            user_id=filter_uid,
            custom_start=custom_start,
            custom_end=custom_end,
        )
    except Exception as e:
        await msg.edit_text(f"❌ Lỗi: {e}")
        return

    text = format_stats_text(stats)
    scope_label = "👤 Chỉ tôi" if current_scope == "all" else "👨‍👩‍👧 Cả nhà"
    toggle_data = f"scope_toggle_{period}_{uid}"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(scope_label, callback_data=toggle_data)
    ]])

    await msg.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

    # Send charts
    try:
        pie_bytes, bar_bytes = generate_charts(stats)
        await update.message.reply_photo(pie_bytes, caption="Pie chart chi tiêu")
        await update.message.reply_photo(bar_bytes, caption="Bar chart theo ngày")
    except Exception as e:
        logger.warning(f"Chart generation failed: {e}")


async def handle_scope_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # scope_toggle_{period}_{uid}
    parts = query.data.split("_", 3)
    period = parts[2]
    uid = int(parts[3])

    scope_key = f"stats_scope_{uid}"
    current = context.user_data.get(scope_key, "all")
    new_scope = "me" if current == "all" else "all"
    context.user_data[scope_key] = new_scope

    filter_uid = str(uid) if new_scope == "me" else None
    stats = await compute_stats(period, user_id=filter_uid)
    text = format_stats_text(stats)

    scope_label = "👤 Chỉ tôi" if new_scope == "all" else "👨‍👩‍👧 Cả nhà"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(scope_label, callback_data=query.data)
    ]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ── Daily reminder ─────────────────────────────────────────────────────────────

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_USERS:
        return

    stats = compute_stats("today")
    yesterday_text = ""
    try:
        from datetime import timedelta
        from sheets import now_vn
        yesterday_stats = compute_stats("today")  # simplified; for full yesterday use custom range
        if yesterday_stats["total_chi"] > 0:
            yesterday_text = f"\nHôm qua chi: {format_amount(yesterday_stats['total_chi'])}"
    except Exception:
        pass

    for user_id in ALLOWED_USERS:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"☀️ 10h rồi! Hôm nay còn khoản chi nào chưa ghi không? Nhắn ngay nhé.{yesterday_text}",
            )
        except Exception as e:
            logger.warning(f"Could not send reminder to {user_id}: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN not set in .env")

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))

    # Conversation handlers (must be added before generic handlers)
    app.add_handler(get_editor_conversation_handler())
    app.add_handler(get_budget_conversation_handler())

    # Inline callbacks
    app.add_handler(CallbackQueryHandler(handle_fix_category, pattern=r"^fix_cat_"))
    app.add_handler(CallbackQueryHandler(handle_recat, pattern=r"^recat_"))
    app.add_handler(CallbackQueryHandler(handle_scope_toggle, pattern=r"^scope_toggle_"))

    # Stats keyboard + transaction input
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        _combined_text_handler,
    ))

    # Daily reminder at 10:00 Asia/Ho_Chi_Minh
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_daily(
            daily_reminder,
            time=dtime(hour=10, minute=0, tzinfo=TZ),
        )

    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


async def _post_init(app) -> None:
    """Called after bot initializes but before polling starts."""
    try:
        await init_sheets()
    except Exception as e:
        logger.warning(f"init_sheets failed (non-fatal): {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            f"❌ Lỗi: {context.error}"
        )


async def _combined_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route text: stats keyboard buttons → handle_stats_keyboard, else → handle_transaction."""
    uid = update.effective_user.id
    text = update.message.text.strip()
    logger.info(f"MSG from {uid} (allowed={is_allowed(uid)}): {repr(text)}")

    if not is_allowed(uid):
        logger.warning(f"Rejected user {uid} — not in whitelist {ALLOWED_USERS}")
        return

    STATS_BUTTONS = {"📊 Hôm nay", "📅 Tuần này", "🗓 Tháng này", "📆 Khoảng tg", "💰 Ngân sách"}

    if text in STATS_BUTTONS or context.user_data.get("waiting_custom_range"):
        await handle_stats_keyboard(update, context)
    else:
        result = parse_message(text)
        logger.info(f"Parse result: {result}")
        if result is None:
            await update.message.reply_text(
                "❓ Không hiểu. Thử: `39 cơm trưa` (chi) hoặc `.500 lương` (thu)",
                parse_mode="Markdown",
            )
            return
        await handle_transaction(update, context)


if __name__ == "__main__":
    main()
