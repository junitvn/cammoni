"""
Main bot entrypoint.
"""
import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

from parser import parse_message, parse_batch_message, format_amount
import classifier
from classifier import (
    classify, category_display, CATEGORY_INFO, CATEGORY_KEYS,
    INCOME_CATEGORY_KEYS, EXPENSE_CATEGORY_KEYS,
)
from sheets import (
    add_transaction, update_transaction_field, now_vn, format_ts, init_sheets,
    upsert_config_mapping, load_categories_from_sheet, load_users_from_sheet,
    load_user_names_from_sheet, TZ,
)
import users as user_store
from stats import compute_stats, format_stats_text, check_budget_warning, format_top_text
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

def _load_allowed_users_from_env() -> set[int]:
    env_ids = os.getenv("ALLOWED_USERS", "")
    if env_ids:
        try:
            return {int(x.strip()) for x in env_ids.split(",") if x.strip()}
        except ValueError:
            pass
    return set()


def _load_allowed_users_from_yaml() -> set[int]:
    try:
        import yaml
        with open("config/users.yaml") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return set()
        # New format: {users: {id: name}}
        if "users" in data:
            id_map = data["users"] or {}
            names = {int(k): str(v) for k, v in id_map.items() if v}
            user_store.set_names(names)
            return set(names.keys())
        # Legacy format: {allowed_users: [id, ...]}
        ids = data.get("allowed_users", [])
        return {int(x) for x in ids if str(x).strip()}
    except Exception:
        return set()


ALLOWED_USERS: set[int] = _load_allowed_users_from_env()


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True  # no whitelist configured → allow all
    return user_id in ALLOWED_USERS


# ── Bot commands (shown in Telegram's blue menu button) ──────────────────────

BOT_COMMANDS = [
    BotCommand("month", "Thống kê tháng này"),
    BotCommand("week", "Thống kê tuần này"),
    BotCommand("today", "Thống kê hôm nay"),
    BotCommand("range", "Thống kê khoảng thời gian"),
    BotCommand("topmonth", "Top chi tiêu tháng này"),
    BotCommand("topweek", "Top chi tiêu tuần này"),
    BotCommand("budget", "Quản lý ngân sách"),
    BotCommand("edit", "Sửa hoặc xóa giao dịch"),
    BotCommand("search", "Tìm theo từ khoá hoặc khoảng tiền"),
    BotCommand("menu", "Tất cả tùy chọn thống kê"),
    BotCommand("start", "Khởi động bot"),
]

# ── Keyboards ─────────────────────────────────────────────────────────────────

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🗓 Tháng này"), KeyboardButton("🏆 Top tháng")],
        [KeyboardButton("💰 Ngân sách"), KeyboardButton("✏️ Sửa/Xóa")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

MENU_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Hôm nay", callback_data="menu_today"),
     InlineKeyboardButton("📅 Tuần này", callback_data="menu_week")],
    [InlineKeyboardButton("🗓 Tháng này", callback_data="menu_month"),
     InlineKeyboardButton("📆 Khoảng tg", callback_data="menu_custom")],
    [InlineKeyboardButton("🏆 Top tuần", callback_data="menu_topweek"),
     InlineKeyboardButton("🏆 Top tháng", callback_data="menu_topmonth")],
])

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
    await update.effective_message.reply_text("📊 Chọn thống kê:", reply_markup=MENU_KEYBOARD)


async def cmd_thang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await _send_stats(update, context, "month")

async def cmd_tuan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await _send_stats(update, context, "week")

async def cmd_homnay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await _send_stats(update, context, "today")

async def cmd_khoang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    context.user_data["waiting_custom_range"] = True
    await update.message.reply_text(
        "Nhập khoảng thời gian (VD: `01/06 - 09/06` hoặc `01/06/2025 - 09/06/2025`):",
        parse_mode="Markdown",
    )

async def cmd_topthang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await _send_top(update, context, "month")

async def cmd_toptuan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await _send_top(update, context, "week")

async def cmd_ngansach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await budget_menu(update, context)


def _resolve_timestamp(day: int, month: int | None) -> datetime:
    """Build a datetime for a given day (and optional month), using current time."""
    now = now_vn()
    month = month or now.month
    year = now.year
    try:
        return now.replace(day=day, month=month, year=year)
    except ValueError:
        return now


async def handle_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any text message that looks like a transaction."""
    if not is_allowed(update.effective_user.id):
        return

    text = update.message.text
    result = parse_message(text)
    if not result:
        return  # not a transaction, ignore

    user_id = str(update.effective_user.id)

    # Resolve timestamp (may include date from message prefix)
    if result.date_day:
        timestamp = _resolve_timestamp(result.date_day, result.date_month)
    else:
        timestamp = now_vn()

    # Classify
    cat_key, ai_used = await classify(result.description, result.amount, result.tx_type)

    cat_disp = category_display(cat_key)
    amt_str = format_amount(result.amount)
    tx_id = str(uuid.uuid4())[:8]

    # Store for later use in recat/date handlers
    context.user_data[f"tx_desc_{tx_id}"] = result.description
    context.user_data[f"tx_type_{tx_id}"] = result.tx_type
    context.user_data[f"tx_ts_{tx_id}"] = timestamp

    date_line = f"\n📅 {timestamp.day}/{timestamp.month}" if result.date_day else ""
    display_name = user_store.get_name(
        update.effective_user.id, update.effective_user.first_name or ""
    )
    name_tag = f" _({display_name})_" if display_name else ""
    if result.tx_type == "thu":
        reply_text = f"💰 Thu nhập: {amt_str}{name_tag}\n{cat_disp} — \"{result.description}\"{date_line}"
    else:
        reply_text = f"✅ Đã ghi: {amt_str}{name_tag}\n{cat_disp} — \"{result.description}\"{date_line}"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Sửa phân loại", callback_data=f"fix_cat_{tx_id}"),
        InlineKeyboardButton("📅 Sửa ngày", callback_data=f"fix_date_{tx_id}"),
    ]])
    await update.message.reply_text(reply_text, reply_markup=keyboard, parse_mode="Markdown")

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
                timestamp=timestamp,
                tx_id=tx_id,
                user_name=display_name,
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


async def _handle_batch_transactions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg,
    items: list,
) -> None:
    """Classify and save a list of parsed transactions; send one message per transaction."""
    uid = str(update.effective_user.id)
    uname = user_store.get_name(update.effective_user.id, update.effective_user.first_name or "")

    parsed = []
    for item in items:
        if isinstance(item, dict):
            amount = int(item.get("amount_k", 0)) * 1000
            description = item.get("description", "")
            tx_type = item.get("type", "chi")
            date_day = item.get("date_day")
            date_month = item.get("date_month")
            date_offset = item.get("date_offset", 0)
        else:
            amount = item.amount
            description = item.description
            tx_type = item.tx_type
            date_day = item.date_day
            date_month = item.date_month
            date_offset = 0
        if amount > 0 and description:
            parsed.append((tx_type, amount, description, date_day, date_month, date_offset))

    if not parsed:
        await status_msg.edit_text("❓ Không ghi được khoản nào.")
        return

    await status_msg.edit_text(f"📝 Đã ghi {len(parsed)} khoản:")

    for tx_type, amount, description, date_day, date_month, date_offset in parsed:
        cat_key, _ = await classify(description, amount, tx_type)
        cat_disp = category_display(cat_key)
        amt_str = format_amount(amount)
        tx_id = str(uuid.uuid4())[:8]
        if date_day:
            timestamp = _resolve_timestamp(date_day, date_month)
        elif date_offset:
            timestamp = now_vn() + timedelta(days=date_offset)
        else:
            timestamp = now_vn()

        context.user_data[f"tx_desc_{tx_id}"] = description
        context.user_data[f"tx_type_{tx_id}"] = tx_type
        context.user_data[f"tx_ts_{tx_id}"] = timestamp

        name_tag = f" _({uname})_" if uname else ""
        date_line = f"\n📅 {timestamp.day}/{timestamp.month}" if (date_day or date_offset) else ""
        if tx_type == "thu":
            text = f"💰 Thu nhập: {amt_str}{name_tag}\n{cat_disp} — \"{description}\"{date_line}"
        else:
            text = f"✅ Đã ghi: {amt_str}{name_tag}\n{cat_disp} — \"{description}\"{date_line}"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Sửa phân loại", callback_data=f"fix_cat_{tx_id}"),
            InlineKeyboardButton("📅 Sửa ngày", callback_data=f"fix_date_{tx_id}"),
        ]])
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

        async def _save(u=uid, un=uname, tt=tx_type, a=amount, ck=cat_key,
                        d=description, ts=timestamp, tid=tx_id):
            try:
                await add_transaction(
                    user_id=u, tx_type=tt, amount=a, category=ck,
                    description=d, auto_classified=True,
                    timestamp=ts, tx_id=tid, user_name=un,
                )
                logger.info(f"[bot] saved batch tx_id={tid}")
                if tt == "chi":
                    warning = await check_budget_warning(ck, a)
                    if warning:
                        await update.effective_message.reply_text(warning)
            except Exception as e:
                logger.exception(f"[bot] batch save FAILED: {e}")

        asyncio.create_task(_save())


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe via Gemini and record transactions."""
    if not is_allowed(update.effective_user.id):
        return

    msg = await update.message.reply_text("🎤 Đang nhận dạng giọng nói...")

    try:
        import io
        from voice import transcribe_to_transactions

        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        audio_bytes = buf.getvalue()

        transactions = await transcribe_to_transactions(audio_bytes)

        if not transactions:
            await msg.edit_text(
                "❓ Không nhận ra khoản thu/chi nào.\n"
                "Thử nói rõ hơn, ví dụ: \"ba mươi cơm, năm mươi cháo\"."
            )
            return

        await _handle_batch_transactions(update, context, msg, transactions)

    except Exception as e:
        logger.exception(f"Voice handling failed: {e}")
        await msg.edit_text(f"❌ Lỗi xử lý giọng nói: {e}")


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


# ── Date picker handlers ──────────────────────────────────────────────────────

def _date_picker_keyboard(tx_id: str, center: datetime) -> InlineKeyboardMarkup:
    """7-day date picker centered on `center`, plus manual entry."""
    row1, row2 = [], []
    for delta in range(-3, 4):
        d = center + timedelta(days=delta)
        label = f"✓{d.day}/{d.month}" if delta == 0 else f"{d.day}/{d.month}"
        cb = f"setdate_{tx_id}_{d.day}_{d.month}_{d.year}"
        (row1 if delta < 1 else row2).append(InlineKeyboardButton(label, callback_data=cb))
    return InlineKeyboardMarkup([
        row1,
        row2,
        [InlineKeyboardButton("✏️ Nhập ngày", callback_data=f"inputdate_{tx_id}")],
    ])


async def handle_fix_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tx_id = query.data.replace("fix_date_", "")
    center = context.user_data.get(f"tx_ts_{tx_id}", now_vn())
    await query.edit_message_reply_markup(reply_markup=_date_picker_keyboard(tx_id, center))


async def handle_setdate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # format: setdate_{tx_id}_{day}_{month}_{year}
    parts = query.data.split("_")
    tx_id, day, month, year = parts[1], int(parts[2]), int(parts[3]), int(parts[4])

    ts = context.user_data.get(f"tx_ts_{tx_id}", now_vn())
    try:
        new_ts = ts.replace(day=day, month=month, year=year)
    except ValueError:
        await query.answer("Ngày không hợp lệ.", show_alert=True)
        return

    ok = await update_transaction_field(tx_id, "timestamp", format_ts(new_ts))
    if ok:
        context.user_data[f"tx_ts_{tx_id}"] = new_ts
        old_text = query.message.text.split("\n")[0]
        await query.edit_message_text(f"{old_text}\n📅 {day}/{month} ✓")
    else:
        await query.answer("Không tìm thấy khoản.", show_alert=True)


async def handle_inputdate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tx_id = query.data.replace("inputdate_", "")
    context.user_data["waiting_date_tx_id"] = tx_id
    old_text = query.message.text
    await query.edit_message_text(
        f"{old_text}\n\n✏️ Nhập ngày (`15` hoặc `15/6` hoặc `15-6`):",
        parse_mode="Markdown",
    )


async def handle_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tx_id = context.user_data.pop("waiting_date_tx_id")
    text = update.message.text.strip()

    ts = context.user_data.get(f"tx_ts_{tx_id}", now_vn())

    m = re.match(r'^(\d{1,2})[/\-](\d{1,2})$', text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
    else:
        m = re.match(r'^(\d{1,2})$', text)
        if m:
            day, month = int(m.group(1)), ts.month
        else:
            await update.message.reply_text(
                "Không nhận ra. Nhập `15` hoặc `15/6`.", parse_mode="Markdown"
            )
            context.user_data["waiting_date_tx_id"] = tx_id
            return

    try:
        new_ts = ts.replace(day=day, month=month)
    except ValueError:
        await update.message.reply_text("Ngày không hợp lệ.")
        context.user_data["waiting_date_tx_id"] = tx_id
        return

    ok = await update_transaction_field(tx_id, "timestamp", format_ts(new_ts))
    if ok:
        context.user_data[f"tx_ts_{tx_id}"] = new_ts
        await update.message.reply_text(f"✅ Đã đổi ngày: {day}/{month}")
    else:
        await update.message.reply_text("❌ Không tìm thấy khoản.")


# ── Stats keyboard handlers ───────────────────────────────────────────────────

async def handle_stats_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    text = update.message.text.strip()

    if text == "🗓 Tháng này":
        await _send_stats(update, context, "month")
    elif text == "🏆 Top tháng":
        await _send_top(update, context, "month")
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


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "menu_today":
        await _send_stats(update, context, "today")
    elif action == "menu_week":
        await _send_stats(update, context, "week")
    elif action == "menu_month":
        await _send_stats(update, context, "month")
    elif action == "menu_topweek":
        await _send_top(update, context, "week")
    elif action == "menu_topmonth":
        await _send_top(update, context, "month")
    elif action == "menu_custom":
        context.user_data["waiting_custom_range"] = True
        await query.message.reply_text(
            "Nhập khoảng thời gian (VD: `01/06 - 09/06` hoặc `01/06/2025 - 09/06/2025`):",
            parse_mode="Markdown",
        )


async def _send_top(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    period: str,
) -> None:
    from sheets import get_transactions_range
    from stats import _date_range
    period_label = "tuần này" if period == "week" else "tháng này"
    msg = await update.effective_message.reply_text("⏳ Đang tải...")
    try:
        start, end = _date_range(period)
        rows = await get_transactions_range(start, end)
        text = format_top_text(rows, period_label)
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Lỗi: {e}")


_PAGE_SIZE = 10


def _tx_list_line(row: dict) -> str:
    ts = str(row.get("timestamp", ""))[:5]
    icon = "💰" if str(row.get("type", "chi")) == "thu" else "💸"
    try:
        amt = format_amount(int(float(str(row.get("amount", 0)))))
    except (ValueError, TypeError):
        amt = "?"
    cat = str(row.get("category", "khac"))
    info = CATEGORY_INFO.get(cat, {"emoji": "📦"})
    desc = str(row.get("description", ""))
    name = user_store.get_name(row.get("user", ""))
    name_part = f" _({name})_" if name else ""
    return f"{icon} {ts} {amt} {info['emoji']} {desc}{name_part}"


def _stats_keyboard(period: str, uid: int, has_data: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📋 Danh sách", callback_data=f"txlist_{uid}_0")]]
    if has_data:
        rows.append([InlineKeyboardButton("📊 Xem biểu đồ", callback_data=f"chart_{uid}")])
    return InlineKeyboardMarkup(rows)


async def _send_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    period: str,
    custom_start=None,
    custom_end=None,
) -> None:
    from stats import PERIODS
    uid = update.effective_user.id

    msg = await update.effective_message.reply_text("⏳ Đang tải...")

    try:
        stats = await compute_stats(period, custom_start=custom_start, custom_end=custom_end)
    except Exception as e:
        await msg.edit_text(f"❌ Lỗi: {e}")
        return

    has_data = stats["total_chi"] > 0 or stats["total_thu"] > 0
    if has_data:
        context.user_data[f"chart_params_{uid}"] = {
            "period": period, "custom_start": custom_start,
            "custom_end": custom_end, "filter_uid": None,
        }
        # Store rows for Danh sách view (newest first)
        context.user_data[f"txlist_rows_{uid}"] = list(reversed(stats["transactions"]))
        context.user_data[f"txlist_label_{uid}"] = PERIODS.get(period, "Khoảng")

    await msg.edit_text(
        format_stats_text(stats),
        reply_markup=_stats_keyboard(period, uid, has_data),
        parse_mode="Markdown",
    )


async def handle_txlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # format: txlist_{uid}_{offset}
    parts = query.data.split("_")
    uid, offset = int(parts[1]), int(parts[2])

    rows = context.user_data.get(f"txlist_rows_{uid}", [])
    label = context.user_data.get(f"txlist_label_{uid}", "Giao dịch")
    total = len(rows)

    if not rows:
        await query.answer("Không có giao dịch nào.", show_alert=True)
        return

    shown = min(offset + _PAGE_SIZE, total)
    lines = [f"📋 *Danh sách — {label}* ({shown}/{total})\n"]
    for row in rows[:shown]:
        lines.append(_tx_list_line(row))
    text = "\n".join(lines)

    kb = []
    if shown < total:
        kb.append([InlineKeyboardButton(
            f"⬇️ Load thêm ({total - shown} còn lại)",
            callback_data=f"txlist_{uid}_{shown}",
        )])

    markup = InlineKeyboardMarkup(kb) if kb else None
    if offset == 0:
        await query.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")


async def handle_chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Đang tạo biểu đồ...")
    uid = int(query.data.replace("chart_", ""))

    params = context.user_data.get(f"chart_params_{uid}")
    if not params:
        await query.message.reply_text("❌ Hãy xem thống kê lại rồi bấm xem biểu đồ.")
        return

    try:
        stats = await compute_stats(
            params["period"],
            user_id=params["filter_uid"],
            custom_start=params.get("custom_start"),
            custom_end=params.get("custom_end"),
        )
        pie_bytes, bar_bytes = generate_charts(stats)
        await query.message.reply_photo(pie_bytes, caption="Chi tiêu theo danh mục")
        await query.message.reply_photo(bar_bytes, caption="Chi tiêu theo ngày")
    except Exception as e:
        logger.warning(f"Chart generation failed: {e}")
        await query.message.reply_text(f"❌ Lỗi tạo biểu đồ: {e}")


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
    app.add_handler(CommandHandler("month", cmd_thang))
    app.add_handler(CommandHandler("week", cmd_tuan))
    app.add_handler(CommandHandler("today", cmd_homnay))
    app.add_handler(CommandHandler("range", cmd_khoang))
    app.add_handler(CommandHandler("topmonth", cmd_topthang))
    app.add_handler(CommandHandler("topweek", cmd_toptuan))
    app.add_handler(CommandHandler("budget", cmd_ngansach))

    # Conversation handlers (must be added before generic handlers)
    app.add_handler(get_editor_conversation_handler())
    app.add_handler(get_budget_conversation_handler())

    # Inline callbacks
    app.add_handler(CallbackQueryHandler(handle_fix_category, pattern=r"^fix_cat_"))
    app.add_handler(CallbackQueryHandler(handle_recat, pattern=r"^recat_"))
    app.add_handler(CallbackQueryHandler(handle_fix_date, pattern=r"^fix_date_"))
    app.add_handler(CallbackQueryHandler(handle_setdate, pattern=r"^setdate_"))
    app.add_handler(CallbackQueryHandler(handle_inputdate, pattern=r"^inputdate_"))
    app.add_handler(CallbackQueryHandler(handle_txlist, pattern=r"^txlist_"))
    app.add_handler(CallbackQueryHandler(handle_chart_callback, pattern=r"^chart_"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern=r"^menu_"))

    # Stats keyboard + transaction input
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        _combined_text_handler,
    ))

    # Voice message transcription
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

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
        await app.bot.set_my_commands(BOT_COMMANDS)
        logger.info("[bot] bot commands registered")
    except Exception as e:
        logger.warning(f"set_my_commands failed: {e}")

    try:
        await init_sheets()
    except Exception as e:
        logger.warning(f"init_sheets failed (non-fatal): {e}")
        return

    try:
        cats = await load_categories_from_sheet()
        if cats:
            classifier.reload_categories(cats)
    except Exception as e:
        logger.warning(f"load_categories failed (non-fatal): {e}")

    # Load users from yaml or sheet only if ALLOWED_USERS env var is not set
    if not os.getenv("ALLOWED_USERS"):
        yaml_users = _load_allowed_users_from_yaml()
        if yaml_users:
            ALLOWED_USERS.clear()
            ALLOWED_USERS.update(yaml_users)
            logger.info(f"[bot] loaded {len(yaml_users)} allowed users from config/users.yaml")
    if not os.getenv("ALLOWED_USERS") and not ALLOWED_USERS:
        try:
            sheet_users = await load_users_from_sheet()
            if sheet_users:
                ALLOWED_USERS.clear()
                ALLOWED_USERS.update(sheet_users)
                logger.info(f"[bot] loaded {len(sheet_users)} allowed users from sheet")
        except Exception as e:
            logger.warning(f"load_users failed (non-fatal): {e}")

    try:
        sheet_names = await load_user_names_from_sheet()
        if sheet_names:
            user_store.update_names(sheet_names)
            logger.info(f"[bot] loaded {len(sheet_names)} user names from sheet")
    except Exception as e:
        logger.warning(f"load_user_names failed (non-fatal): {e}")


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

    STATS_BUTTONS = {"🗓 Tháng này", "🏆 Top tháng", "💰 Ngân sách"}

    if context.user_data.get("waiting_date_tx_id"):
        await handle_date_input(update, context)
        return

    if text in STATS_BUTTONS or context.user_data.get("waiting_custom_range"):
        await handle_stats_keyboard(update, context)
        return

    # Single transaction
    result = parse_message(text)
    if result:
        logger.info(f"Parse result (single): {result}")
        await handle_transaction(update, context)
        return

    # Batch transactions
    batch = parse_batch_message(text)
    if batch:
        logger.info(f"Parse result (batch): {len(batch)} items")
        msg = await update.message.reply_text("⏳ Đang ghi...")
        await _handle_batch_transactions(update, context, msg, batch)
        return

    await update.message.reply_text(
        "❓ Không hiểu. Thử:\n"
        "• `39 cơm trưa` — ghi 1 khoản\n"
        "• `40 cơm, 50 cháo, 12 gửi xe` — ghi nhiều khoản",
        parse_mode="Markdown",
    )


if __name__ == "__main__":
    main()
