"""
Main bot entrypoint.
"""
import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, time as dtime
from typing import Optional
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
    load_user_names_from_sheet, set_budget, delete_transaction,
    get_transaction_by_id, TZ,
)
import users as user_store
from stats import compute_stats, format_stats_text, check_budget_warning, format_top_text
from charts import generate_charts
from editor import get_editor_conversation_handler
from budget import get_budget_conversation_handler, budget_menu
from worldcup import fetch_worldcup_scores

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


def _load_reminder_users() -> set[int]:
    env_ids = os.getenv("REMINDER_USERS", "").strip()
    if env_ids:
        try:
            return {int(i.strip()) for i in env_ids.split(",") if i.strip()}
        except ValueError:
            pass
    return set()


REMINDER_USERS: set[int] = _load_reminder_users()


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True  # no whitelist configured → allow all
    return user_id in ALLOWED_USERS


# ── Bot commands (shown in Telegram's blue menu button) ──────────────────────

BOT_COMMANDS = [
    BotCommand("month", "Thống kê tháng này (hoặc /month 5 để xem tháng 5)"),
    BotCommand("week", "Thống kê tuần này"),
    BotCommand("today", "Thống kê hôm nay"),
    BotCommand("range", "Thống kê khoảng thời gian"),
    BotCommand("topmonth", "Top chi tiêu tháng này"),
    BotCommand("topweek", "Top chi tiêu tuần này"),
    BotCommand("budget", "Quản lý ngân sách"),
    BotCommand("worldcup", "Kết quả World Cup hôm qua (hoặc /worldcup YYYY-MM-DD)"),
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

def _action_sheet_kb(tx_id: str, excluded: bool, cancel_cb: str | None = None) -> InlineKeyboardMarkup:
    excl_label = "✅ Tính vào ngân sách" if excluded else "🚫 Không tính"
    rows = [
        [InlineKeyboardButton("✏️ Sửa", callback_data=f"edm_enter_{tx_id}")],
        [
            InlineKeyboardButton(excl_label, callback_data=f"qexcl_{tx_id}"),
            InlineKeyboardButton("🗑️ Xóa", callback_data=f"qdel_{tx_id}"),
        ],
    ]
    if cancel_cb:
        rows.append([InlineKeyboardButton("❌ Hủy", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(rows)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 Chào mừng đến với *Cam's Moni*!\n\n"
        "Nhắn tin để ghi chi tiêu:\n"
        "  `39 cơm trưa` → chi 39.000đ\n"
        "  `.500 lương` → thu 500.000đ\n"
        "  `+200 mẹ cho` → thu 200.000đ\n\n"
        "Có thể dùng voice chat để ghi lại giao dịch, tìm kiếm hoặc thiết lập ngân sách.\n\n"
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
    args = context.args or []
    if args:
        try:
            month = int(args[0])
            year = int(args[1]) if len(args) > 1 else now_vn().year
            if not (1 <= month <= 12):
                raise ValueError
            start = datetime(year, month, 1, tzinfo=TZ)
            if month == 12:
                end = datetime(year + 1, 1, 1, tzinfo=TZ) - timedelta(seconds=1)
            else:
                end = datetime(year, month + 1, 1, tzinfo=TZ) - timedelta(seconds=1)
            await _send_stats(update, context, "month", custom_start=start, custom_end=end)
        except (ValueError, IndexError):
            await update.message.reply_text("Dùng: `/month 5` hoặc `/month 5 2025`", parse_mode="Markdown")
    else:
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
        "Nhập khoảng thời gian:\n"
        "• `3 6` — ngày 3 đến ngày 6 tháng này\n"
        "• `5/5 6` — ngày 5/5 đến ngày 6 tháng này\n"
        "• `25/5 3/6` — 25/5 đến 3/6\n"
        "• `1/6/2025 30/6/2025` — khoảng cụ thể",
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

    display_name = user_store.get_name(
        update.effective_user.id, update.effective_user.first_name or ""
    )
    name_part = f" ({display_name})" if display_name else ""
    type_label = "Thu nhập" if result.tx_type == "thu" else "Chi tiêu"
    date_str = f"{timestamp.day}/{timestamp.month}"
    reply_text = (
        f"✅ {date_str} {type_label}{name_part}:\n"
        f"  {cat_disp} · {result.description}\n"
        f"  💰 {amt_str}"
    )

    keyboard = _action_sheet_kb(tx_id, excluded=False)
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

        name_part = f" ({uname})" if uname else ""
        type_label = "Thu nhập" if tx_type == "thu" else "Chi tiêu"
        date_str = f"{timestamp.day}/{timestamp.month}"
        text = (
            f"✅ {date_str} {type_label}{name_part}:\n"
            f"  {cat_disp} · {description}\n"
            f"  💰 {amt_str}"
        )

        keyboard = _action_sheet_kb(tx_id, excluded=False)
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
        from voice import transcribe_voice
        from sheets import get_recent_transactions
        from parser import parse_amount_search, format_amount_range

        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        audio_bytes = buf.getvalue()

        result = await transcribe_voice(audio_bytes)

        intent = result.get("intent", "record")
        if intent == "search":
            await _handle_voice_search(update, context, msg, result)
        elif intent == "stats":
            await _handle_voice_stats(update, context, msg, result)
        elif intent == "budget":
            await _handle_voice_budget(update, context, msg, result)
        elif intent == "category_filter":
            await _handle_voice_category_filter(update, context, msg, result)
        else:
            transactions = result.get("transactions", [])
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


async def _handle_voice_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    msg,
    search_result: dict,
) -> None:
    from sheets import get_recent_transactions
    from parser import parse_amount_search, format_amount_range

    keyword = search_result.get("keyword") or None
    amount_str = search_result.get("amount_search")
    amount_min = amount_max = None

    if amount_str:
        amt_range = parse_amount_search(str(amount_str))
        if amt_range:
            amount_min, amount_max, _ = amt_range

    rows = await get_recent_transactions(
        limit=100, keyword=keyword, amount_min=amount_min, amount_max=amount_max
    )

    if not rows:
        parts = []
        if keyword:
            parts.append(f'"{keyword}"')
        if amount_min is not None:
            parts.append(format_amount_range(amount_min, amount_max))
        await msg.edit_text(f"Không tìm thấy kết quả cho {' · '.join(parts) or 'tìm kiếm'}.")
        return

    uid = update.effective_user.id
    rows_desc = _sort_rows_grouped(rows)
    context.user_data[f"txlist_rows_{uid}"] = rows_desc

    label_parts = []
    if keyword:
        label_parts.append(keyword)
    if amount_min is not None:
        label_parts.append(format_amount_range(amount_min, amount_max))
    label = " · ".join(label_parts) or "Tìm kiếm"
    context.user_data[f"txlist_label_{uid}"] = label

    total = len(rows_desc)
    shown = min(_PAGE_SIZE, total)
    context.user_data[f"txlist_offset_{uid}"] = shown
    await msg.edit_text(
        _format_txlist_grouped(rows_desc, shown, f"🔍 {label}"),
        reply_markup=_txlist_keyboard(uid, shown, total),
        parse_mode="Markdown",
    )


async def _handle_voice_stats(update, context, msg, result: dict) -> None:
    from stats import compute_stats, format_stats_text, PERIODS
    period = str(result.get("period", "month"))
    custom_start = custom_end = None

    if period == "range":
        ref = now_vn()
        rs = result.get("range_start") or ""
        re_ = result.get("range_end") or ""
        custom_start = _parse_date_token(rs, ref) if rs else None
        custom_end = _parse_date_token(re_, ref) if re_ else None
        if not custom_start or not custom_end:
            await msg.edit_text("❓ Không nhận ra khoảng thời gian.")
            return
        if custom_end < custom_start:
            custom_start, custom_end = custom_end, custom_start
        custom_end = custom_end.replace(hour=23, minute=59, second=59)
    elif period == "month" and result.get("month"):
        try:
            month = int(result["month"])
            year = int(result["year"]) if result.get("year") else now_vn().year
            custom_start = datetime(year, month, 1, tzinfo=TZ)
            if month == 12:
                custom_end = datetime(year + 1, 1, 1, tzinfo=TZ) - timedelta(seconds=1)
            else:
                custom_end = datetime(year, month + 1, 1, tzinfo=TZ) - timedelta(seconds=1)
        except (ValueError, TypeError):
            pass

    uid = update.effective_user.id
    try:
        stats = await compute_stats(period, custom_start=custom_start, custom_end=custom_end)
    except Exception as e:
        await msg.edit_text(f"❌ Lỗi thống kê: {e}")
        return

    has_data = stats["total_chi"] > 0 or stats["total_thu"] > 0
    if has_data:
        _s = stats.get("start")
        if period == "month" and _s:
            _lbl = f"Tháng {_s.month}/{_s.year}"
        elif period in PERIODS:
            _lbl = PERIODS[period]
        elif _s:
            _lbl = f"{format_ts(_s)[:5]}-{format_ts(stats['end'])[:5]}"
        else:
            _lbl = "Thống kê"
        context.user_data[f"txlist_rows_{uid}"] = _sort_rows_grouped(stats["transactions"])
        context.user_data[f"txlist_label_{uid}"] = _lbl
        context.user_data[f"txlist_offset_{uid}"] = min(_PAGE_SIZE, len(stats["transactions"]))
        context.user_data[f"chart_params_{uid}"] = {
            "period": period, "custom_start": custom_start,
            "custom_end": custom_end, "filter_uid": None,
        }

    await msg.edit_text(
        format_stats_text(stats),
        reply_markup=_stats_keyboard(period, uid, has_data),
        parse_mode="Markdown",
    )


async def _handle_voice_budget(update, context, msg, result: dict) -> None:
    scope = str(result.get("scope", "chung"))
    amount_k = int(result.get("amount_k", 0))
    if amount_k <= 0:
        await msg.edit_text("❓ Không nhận ra số tiền ngân sách.")
        return
    await set_budget(scope, amount_k * 1000)
    label = "Tổng chi" if scope == "chung" else CATEGORY_INFO.get(scope, {"name": scope}).get("name", scope)
    await msg.edit_text(
        f"✅ Đã đặt ngân sách *{label}*: {format_amount(amount_k * 1000)}/tháng",
        parse_mode="Markdown",
    )


async def _handle_voice_category_filter(update, context, msg, result: dict) -> None:
    from sheets import get_recent_transactions
    category = str(result.get("category", ""))
    if not category:
        await msg.edit_text("❓ Không nhận ra danh mục.")
        return
    rows = await get_recent_transactions(limit=100, category=category)
    info = CATEGORY_INFO.get(category, {"emoji": "📦", "name": category})
    label = f"{info['emoji']} {info['name']}"
    if not rows:
        await msg.edit_text(f"Không tìm thấy giao dịch nào cho {label}.")
        return
    uid = update.effective_user.id
    rows_desc = _sort_rows_grouped(rows)
    context.user_data[f"txlist_rows_{uid}"] = rows_desc
    context.user_data[f"txlist_label_{uid}"] = label
    total = len(rows_desc)
    shown = min(_PAGE_SIZE, total)
    context.user_data[f"txlist_offset_{uid}"] = shown
    await msg.edit_text(
        _format_txlist_grouped(rows_desc, shown, f"🔍 {label}"),
        reply_markup=_txlist_keyboard(uid, shown, total),
        parse_mode="Markdown",
    )


# ── Edit mode ─────────────────────────────────────────────────────────────────

def _edm_detail_text(row: dict) -> str:
    tx_type = str(row.get("type", "chi"))
    type_icon = "💰" if tx_type == "thu" else "💸"
    try:
        amt = format_amount(int(float(str(row.get("amount", 0)))))
    except (ValueError, TypeError):
        amt = "?"
    cat = str(row.get("category", "khac"))
    cat_info = CATEGORY_INFO.get(cat, {"emoji": "📦", "name": cat})
    desc = str(row.get("description", ""))
    name = user_store.get_name(row.get("user", ""))
    ts_str = str(row.get("timestamp", ""))[:16]
    excl = str(row.get("excluded", "")).strip().upper() == "Y"
    lines = [
        f"{type_icon} *{amt}* — {cat_info['emoji']} {cat_info['name']}",
        f'📝 "{desc}"',
        f"📅 {ts_str}",
    ]
    if name:
        lines.append(f"👤 _{name}_")
    if excl:
        lines.append("🚫 _Không tính vào ngân sách_")
    return "\n".join(lines)


def _edm_field_kb(tx_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Số tiền", callback_data=f"edm_fld_{tx_id}_amount"),
            InlineKeyboardButton("🏷 Phân loại", callback_data=f"edm_fld_{tx_id}_category"),
        ],
        [
            InlineKeyboardButton("📅 Ngày", callback_data=f"edm_fld_{tx_id}_date"),
            InlineKeyboardButton("📝 Mô tả", callback_data=f"edm_fld_{tx_id}_desc"),
        ],
        [InlineKeyboardButton("👤 Người", callback_data=f"edm_fld_{tx_id}_user")],
        [InlineKeyboardButton("❌ Hủy", callback_data=f"edm_cancel_{tx_id}")],
    ])


async def _render_edit_screen(query, context, tx_id: str) -> None:
    row = context.user_data.get(f"edm_row_{tx_id}", {})
    text = _edm_detail_text(row) + "\n\nChọn trường cần sửa:"
    try:
        await query.edit_message_text(text, reply_markup=_edm_field_kb(tx_id), parse_mode="Markdown")
    except Exception:
        pass


async def handle_edm_enter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tx_id = query.data.replace("edm_enter_", "")
    result = await get_transaction_by_id(tx_id)
    if not result:
        await query.answer("Không tìm thấy khoản.", show_alert=True)
        return
    _, row, _sht = result
    context.user_data[f"edm_row_{tx_id}"] = row
    context.user_data[f"edm_msg_{tx_id}"] = (query.message.chat_id, query.message.message_id)
    context.user_data[f"edm_snap_{tx_id}"] = (
        query.message.text or "",
        query.message.reply_markup,
    )
    await _render_edit_screen(query, context, tx_id)


def _edm_date_kb(tx_id: str, center: datetime) -> InlineKeyboardMarkup:
    row1, row2 = [], []
    for delta in range(-3, 4):
        d = center + timedelta(days=delta)
        label = f"✓{d.day}/{d.month}" if delta == 0 else f"{d.day}/{d.month}"
        cb = f"edm_setdate_{tx_id}_{d.day}_{d.month}_{d.year}"
        (row1 if delta < 1 else row2).append(InlineKeyboardButton(label, callback_data=cb))
    return InlineKeyboardMarkup([
        row1, row2,
        [InlineKeyboardButton("✏️ Nhập ngày", callback_data=f"edm_inputdate_{tx_id}")],
        [InlineKeyboardButton("⬅️ Quay lại", callback_data=f"edm_back_{tx_id}")],
    ])


async def handle_edm_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    rest = query.data[len("edm_fld_"):]
    tx_id, field = rest[:8], rest[9:]
    row = context.user_data.get(f"edm_row_{tx_id}", {})

    if field == "category":
        tx_type = str(row.get("type", "chi"))
        keys = INCOME_CATEGORY_KEYS if tx_type == "thu" else EXPENSE_CATEGORY_KEYS
        cur = str(row.get("category", "khac"))
        per_row = 2 if tx_type == "thu" else 3
        kb_rows, btn_row = [], []
        for key in keys:
            info = CATEGORY_INFO[key]
            label = f"✓ {info['emoji']} {info['name']}" if key == cur else f"{info['emoji']} {info['name']}"
            btn_row.append(InlineKeyboardButton(label, callback_data=f"edm_setcat_{tx_id}_{key}"))
            if len(btn_row) == per_row:
                kb_rows.append(btn_row)
                btn_row = []
        if btn_row:
            kb_rows.append(btn_row)
        kb_rows.append([InlineKeyboardButton("⬅️ Quay lại", callback_data=f"edm_back_{tx_id}")])
        await query.edit_message_text("Chọn phân loại:", reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    if field == "date":
        from sheets import parse_ts as _pts
        center = _pts(str(row.get("timestamp", ""))) or now_vn()
        await query.edit_message_text("Chọn ngày:", reply_markup=_edm_date_kb(tx_id, center))
        return

    if field == "user":
        names = user_store.get_all()
        cur_user = str(row.get("user", ""))
        kb_rows = []
        for uid, nm in names.items():
            prefix = "✓ " if str(uid) == cur_user else ""
            kb_rows.append([InlineKeyboardButton(
                f"{prefix}{nm}", callback_data=f"edm_setuser_{tx_id}_{uid}"
            )])
        if not kb_rows:
            kb_rows.append([InlineKeyboardButton("(không có user)", callback_data="noop")])
        kb_rows.append([InlineKeyboardButton("⬅️ Quay lại", callback_data=f"edm_back_{tx_id}")])
        await query.edit_message_text("Chọn người:", reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    # amount or desc → text input
    context.user_data["edm_waiting"] = (tx_id, field)
    prompt = "Nhập số tiền mới (VD: `150` = 150.000đ):" if field == "amount" else "Nhập mô tả mới:"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data=f"edm_back_{tx_id}")]])
    await query.edit_message_text(prompt, reply_markup=kb, parse_mode="Markdown")


async def handle_edm_setcat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    rest = query.data[len("edm_setcat_"):]
    tx_id, cat_key = rest[:8], rest[9:]
    row = context.user_data.get(f"edm_row_{tx_id}", {})
    ok = await update_transaction_field(tx_id, "category", cat_key)
    if ok:
        row["category"] = cat_key
        desc = str(row.get("description", ""))
        if desc:
            asyncio.create_task(upsert_config_mapping(desc, cat_key))
    await _render_edit_screen(query, context, tx_id)


async def handle_edm_setdate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    tx_id, day, month, year = parts[2], int(parts[3]), int(parts[4]), int(parts[5])
    row = context.user_data.get(f"edm_row_{tx_id}", {})
    from sheets import parse_ts as _pts
    base = _pts(str(row.get("timestamp", ""))) or now_vn()
    try:
        new_ts = base.replace(day=day, month=month, year=year)
    except ValueError:
        await query.answer("Ngày không hợp lệ.", show_alert=True)
        return
    ok = await update_transaction_field(tx_id, "timestamp", format_ts(new_ts))
    if ok:
        row["timestamp"] = format_ts(new_ts)
    await _render_edit_screen(query, context, tx_id)


async def handle_edm_inputdate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tx_id = query.data.replace("edm_inputdate_", "")
    context.user_data["edm_waiting"] = (tx_id, "date")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Quay lại", callback_data=f"edm_back_{tx_id}"),
    ]])
    await query.edit_message_text("Nhập ngày (`15` hoặc `15/6`):", reply_markup=kb, parse_mode="Markdown")


async def handle_edm_setuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    rest = query.data[len("edm_setuser_"):]
    tx_id, uid_str = rest[:8], rest[9:]
    try:
        uid = int(uid_str)
    except ValueError:
        await query.answer("User không hợp lệ.", show_alert=True)
        return
    row = context.user_data.get(f"edm_row_{tx_id}", {})
    name = user_store.get_name(uid) or ""
    ok1 = await update_transaction_field(tx_id, "user", str(uid))
    ok2 = await update_transaction_field(tx_id, "user_name", name)
    if ok1:
        row["user"] = uid
    if ok2:
        row["user_name"] = name
    await _render_edit_screen(query, context, tx_id)


async def handle_edm_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tx_id = query.data.replace("edm_back_", "")
    context.user_data.pop("edm_waiting", None)
    await _render_edit_screen(query, context, tx_id)


async def handle_edm_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Legacy handler — no longer triggered (no Lưu button), kept for safety
    query = update.callback_query
    await query.answer()


async def handle_edm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tx_id = query.data.replace("edm_cancel_", "")
    snap = context.user_data.pop(f"edm_snap_{tx_id}", None)
    for k in (f"edm_row_{tx_id}", f"edm_msg_{tx_id}"):
        context.user_data.pop(k, None)
    context.user_data.pop("edm_waiting", None)
    if snap and snap[0]:
        try:
            await query.edit_message_text(snap[0], reply_markup=snap[1])
            return
        except Exception:
            pass
    await query.edit_message_text("❌ Đã huỷ.")


async def handle_edm_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    waiting = context.user_data.get("edm_waiting")
    if not waiting:
        return False
    tx_id, field = waiting
    text = update.message.text.strip()
    row = context.user_data.get(f"edm_row_{tx_id}", {})

    if field == "amount":
        try:
            value = int(text.replace(".", "").replace(",", "").replace(" ", "")) * 1000
        except ValueError:
            await update.message.reply_text("Số không hợp lệ, nhập lại:")
            return True
        ok = await update_transaction_field(tx_id, "amount", value)
        if ok:
            row["amount"] = value
    elif field == "desc":
        ok = await update_transaction_field(tx_id, "description", text)
        if ok:
            row["description"] = text
    elif field == "date":
        from sheets import parse_ts as _pts
        base = _pts(str(row.get("timestamp", ""))) or now_vn()
        m = re.match(r'^(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?$', text)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else base.year
            if year < 100:
                year += 2000
        else:
            m = re.match(r'^(\d{1,2})$', text)
            if not m:
                await update.message.reply_text("Không nhận ra. Nhập `15` hoặc `15/6`.", parse_mode="Markdown")
                return True
            day, month, year = int(m.group(1)), base.month, base.year
        try:
            new_ts = base.replace(day=day, month=month, year=year)
        except ValueError:
            await update.message.reply_text("Ngày không hợp lệ, nhập lại:")
            return True
        ok = await update_transaction_field(tx_id, "timestamp", format_ts(new_ts))
        if ok:
            row["timestamp"] = format_ts(new_ts)

    context.user_data.pop("edm_waiting", None)
    msg_loc = context.user_data.get(f"edm_msg_{tx_id}")
    if msg_loc:
        chat_id, message_id = msg_loc
        try:
            await context.bot.edit_message_text(
                _edm_detail_text(row) + "\n\nChọn trường cần sửa:",
                chat_id=chat_id, message_id=message_id,
                reply_markup=_edm_field_kb(tx_id),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"render edit screen failed: {e}")
    return True


# ── Quick delete / exclude handlers ──────────────────────────────────────────

async def handle_qdel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tx_id = query.data.replace("qdel_", "")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Xác nhận xóa", callback_data=f"qdelok_{tx_id}"),
        InlineKeyboardButton("❌ Hủy", callback_data=f"qdelno_{tx_id}"),
    ]])
    await query.edit_message_reply_markup(reply_markup=keyboard)


async def handle_qdelok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data.startswith("qdelno_"):
        tx_id = query.data.replace("qdelno_", "")
        in_edit_mode = f"edm_row_{tx_id}" in context.user_data
        if in_edit_mode:
            await _render_edit_screen(query, context, tx_id)
            return
        result = await get_transaction_by_id(tx_id)
        excl = False
        if result:
            _, row, _sht = result
            excl = str(row.get("excluded", "")).strip().upper() == "Y"
        await query.edit_message_reply_markup(reply_markup=_action_sheet_kb(tx_id, excl))
        return
    tx_id = query.data.replace("qdelok_", "")
    ok = await delete_transaction(tx_id)
    for k in (f"edm_row_{tx_id}", f"edm_msg_{tx_id}", f"edm_snap_{tx_id}"):
        context.user_data.pop(k, None)
    context.user_data.pop("edm_waiting", None)
    first_line = (query.message.text or "").split("\n")[0]
    if ok:
        await query.edit_message_text(f"🗑️ Đã xóa: {first_line}")
    else:
        await query.edit_message_text("❌ Không tìm thấy khoản.")


async def handle_qexcl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tx_id = query.data.replace("qexcl_", "")
    result = await get_transaction_by_id(tx_id)
    if not result:
        await query.answer("Không tìm thấy khoản.", show_alert=True)
        return
    _, row, _sheet = result
    currently_excluded = str(row.get("excluded", "")).strip().upper() == "Y"
    new_val = "" if currently_excluded else "Y"
    await update_transaction_field(tx_id, "excluded", new_val)

    if f"edm_row_{tx_id}" in context.user_data:
        context.user_data[f"edm_row_{tx_id}"]["excluded"] = new_val
        await _render_edit_screen(query, context, tx_id)
        return

    old_text = query.message.text or ""
    skip_prefixes = ("🚫", "✅ Đã tính", "🔴", "⚠️")
    lines = [l for l in old_text.split("\n") if not any(l.startswith(p) for p in skip_prefixes)]
    base = "\n".join(lines).rstrip()
    if new_val == "Y":
        await query.edit_message_text(f"{base}\n🚫 _Không tính vào ngân sách_", parse_mode="Markdown")
    else:
        await query.edit_message_text(f"{base}\n✅ _Đã tính vào ngân sách_", parse_mode="Markdown")


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


def _parse_date_token(token: str, ref: datetime) -> Optional[datetime]:
    """Parse 'dd', 'dd/mm', or 'dd/mm/yyyy' relative to ref month/year."""
    token = token.strip()
    parts = re.split(r"[/\-]", token)
    try:
        day = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else ref.month
        year = int(parts[2]) if len(parts) > 2 else ref.year
        return datetime(year, month, day, tzinfo=TZ)
    except (ValueError, IndexError):
        return None


async def _handle_custom_range(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    ref = now_vn()
    # Remove separators (dash, en-dash, 'đến', 'to') then split on whitespace
    cleaned = re.sub(r"\s*[-–]\s*", " ", text.strip())
    tokens = [t for t in cleaned.split() if t not in ("đến", "tới", "to")]

    if len(tokens) < 2:
        await update.message.reply_text(
            "Nhập 2 mốc thời gian. VD: `3 6` hoặc `5/5 6` hoặc `25/5 3/6`",
            parse_mode="Markdown",
        )
        return

    start = _parse_date_token(tokens[0], ref)
    end = _parse_date_token(tokens[1], ref)
    if not start or not end:
        await update.message.reply_text("Không nhận ra ngày. VD: `3 6` hoặc `5/5 6`", parse_mode="Markdown")
        return

    if end < start:
        start, end = end, start
    end = end.replace(hour=23, minute=59, second=59)
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
    try:
        amt = format_amount(int(float(str(row.get("amount", 0)))))
    except (ValueError, TypeError):
        amt = "?"
    cat = str(row.get("category", "khac"))
    info = CATEGORY_INFO.get(cat, {"emoji": "📦"})
    desc = str(row.get("description", ""))
    name = user_store.get_name(row.get("user", ""))
    inner = f"{info['emoji']} • {name} • {desc}" if name else f"{info['emoji']} • {desc}"
    return f"  {amt:<15}{inner}"


def _sort_rows_grouped(rows: list) -> list:
    from sheets import parse_ts as _parse_ts
    def _key(r):
        type_order = 0 if str(r.get("type", "chi")) == "thu" else 1
        dt = _parse_ts(str(r.get("timestamp", "")))
        return (type_order, -(dt.timestamp() if dt else 0))
    return sorted(rows, key=_key)


def _format_txlist_grouped(rows: list, shown: int, label: str) -> str:
    lines = [f"📋 *{label}* ({shown}/{len(rows)})\n"]
    current_type: str | None = None
    current_date: str | None = None
    for row in rows[:shown]:
        tx_type = str(row.get("type", "chi"))
        date_str = str(row.get("timestamp", ""))[:5]  # dd/mm
        if tx_type != current_type:
            current_type = tx_type
            current_date = None
            lines.append("\n💰 *Thu nhập*" if tx_type == "thu" else "\n💸 *Chi tiêu*")
        if date_str != current_date:
            current_date = date_str
            lines.append(f"📅 _{date_str}_")
        lines.append(_tx_list_line(row))
    return "\n".join(lines)


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
        context.user_data[f"txlist_rows_{uid}"] = _sort_rows_grouped(stats["transactions"])
        # Derive a good label: show month/year for monthly stats
        _s = stats.get("start")
        if period == "month" and _s:
            _txlabel = f"Tháng {_s.month}/{_s.year}"
        elif period in PERIODS:
            _txlabel = PERIODS[period]
        elif _s:
            _txlabel = f"{format_ts(_s)[:5]}-{format_ts(stats['end'])[:5]}"
        else:
            _txlabel = "Khoảng"
        context.user_data[f"txlist_label_{uid}"] = _txlabel

    await msg.edit_text(
        format_stats_text(stats),
        reply_markup=_stats_keyboard(period, uid, has_data),
        parse_mode="Markdown",
    )


def _txlist_keyboard(uid: int, shown: int, total: int) -> InlineKeyboardMarkup:
    """Build the standard txlist keyboard: Load thêm (if any) + Sửa button."""
    row = []
    if shown < total:
        row.append(InlineKeyboardButton(
            f"⬇️ Xem thêm ({total - shown})",
            callback_data=f"txlist_{uid}_{shown}",
        ))
    row.append(InlineKeyboardButton("✏️ Sửa", callback_data=f"txlist_sel_{uid}"))
    return InlineKeyboardMarkup([row])


async def handle_txlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")  # txlist_{sub}_{...}
    sub = parts[1]

    # ── Load page: txlist_{uid}_{offset} ──────────────────────────────────────
    if sub.lstrip("-").isdigit():
        uid, offset = int(sub), int(parts[2])
        rows = context.user_data.get(f"txlist_rows_{uid}", [])
        label = context.user_data.get(f"txlist_label_{uid}", "Giao dịch")
        total = len(rows)
        if not rows:
            await query.answer("Không có giao dịch nào.", show_alert=True)
            return
        shown = min(offset + _PAGE_SIZE, total)
        context.user_data[f"txlist_offset_{uid}"] = shown
        text = _format_txlist_grouped(rows, shown, f"Danh sách — {label}")
        markup = _txlist_keyboard(uid, shown, total)
        if offset == 0:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
        else:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    # ── Show number selector: txlist_sel_{uid} ────────────────────────────────
    if sub == "sel":
        uid = int(parts[2])
        rows = context.user_data.get(f"txlist_rows_{uid}", [])
        shown = context.user_data.get(f"txlist_offset_{uid}", min(_PAGE_SIZE, len(rows)))
        visible = rows[:shown]
        if not visible:
            await query.answer("Không có giao dịch nào.", show_alert=True)
            return
        keyboard = []
        row_btns = []
        for i, row in enumerate(visible):
            ts = str(row.get("timestamp", ""))[:5]
            try:
                amt_k = int(float(str(row.get("amount", 0)))) // 1000
            except (ValueError, TypeError):
                amt_k = 0
            desc = str(row.get("description", ""))[:12]
            label = f"{i + 1}. {ts} {amt_k}k {desc}"
            row_btns.append(InlineKeyboardButton(label, callback_data=f"txlist_pick_{uid}_{i}"))
            if len(row_btns) == 2:
                keyboard.append(row_btns)
                row_btns = []
        if row_btns:
            keyboard.append(row_btns)
        keyboard.append([InlineKeyboardButton("❌ Hủy", callback_data=f"txlist_selcancel_{uid}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # ── Pick item: txlist_pick_{uid}_{index} ──────────────────────────────────
    if sub == "pick":
        uid, idx = int(parts[2]), int(parts[3])
        rows = context.user_data.get(f"txlist_rows_{uid}", [])
        shown = context.user_data.get(f"txlist_offset_{uid}", min(_PAGE_SIZE, len(rows)))
        total = len(rows)
        if idx >= len(rows):
            await query.answer("Không tìm thấy.", show_alert=True)
            return
        row = rows[idx]
        tx_id = str(row.get("id", ""))

        # Populate user_data for fix_cat/fix_date/qdel/qexcl handlers
        context.user_data[f"tx_type_{tx_id}"] = row.get("type", "chi")
        context.user_data[f"tx_desc_{tx_id}"] = row.get("description", "")
        from sheets import parse_ts as _pts
        ts_obj = _pts(str(row.get("timestamp", "")))
        if ts_obj:
            context.user_data[f"tx_ts_{tx_id}"] = ts_obj

        # Restore original list keyboard
        await query.edit_message_reply_markup(reply_markup=_txlist_keyboard(uid, shown, total))

        # Send item detail as new message
        ts = str(row.get("timestamp", ""))[:16]
        tx_type = str(row.get("type", "chi"))
        type_icon = "💰" if tx_type == "thu" else "💸"
        try:
            amt = format_amount(int(float(str(row.get("amount", 0)))))
        except (ValueError, TypeError):
            amt = "?"
        cat = str(row.get("category", "khac"))
        cat_info = CATEGORY_INFO.get(cat, {"emoji": "📦", "name": cat})
        desc = str(row.get("description", ""))
        name = user_store.get_name(row.get("user", ""))
        name_line = f"\n👤 _{name}_" if name else ""
        excl = str(row.get("excluded", "")).strip().upper() == "Y"
        excl_line = "\n🚫 _Không tính vào ngân sách_" if excl else ""

        detail = (
            f"{type_icon} *{amt}* — {cat_info['emoji']} {cat_info['name']}\n"
            f'📝 "{desc}"\n'
            f"📅 {ts}{name_line}{excl_line}"
        )
        keyboard = _action_sheet_kb(tx_id, excl, cancel_cb="txlist_itemcancel")
        await query.message.reply_text(detail, reply_markup=keyboard, parse_mode="Markdown")
        return

    # ── Cancel number selector: txlist_selcancel_{uid} ────────────────────────
    if sub == "selcancel":
        uid = int(parts[2])
        rows = context.user_data.get(f"txlist_rows_{uid}", [])
        shown = context.user_data.get(f"txlist_offset_{uid}", min(_PAGE_SIZE, len(rows)))
        total = len(rows)
        await query.edit_message_reply_markup(reply_markup=_txlist_keyboard(uid, shown, total))
        return

    # ── Cancel item detail message: txlist_itemcancel ─────────────────────────
    if sub == "itemcancel":
        await query.edit_message_text("❌ Đã hủy.")
        return


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


# ── World Cup scores ───────────────────────────────────────────────────────────

async def cmd_worldcup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    date_arg = None
    if context.args:
        try:
            from datetime import date
            date_arg = date.fromisoformat(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Dùng: /worldcup hoặc /worldcup YYYY-MM-DD")
            return
    text = await fetch_worldcup_scores(date_arg)
    await update.effective_message.reply_text(text, parse_mode="Markdown")


async def worldcup_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_USERS:
        return
    text = await fetch_worldcup_scores()
    for user_id in ALLOWED_USERS:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Could not send worldcup scores to {user_id}: {e}")


# ── Daily reminder ─────────────────────────────────────────────────────────────

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    targets = REMINDER_USERS or ALLOWED_USERS
    if not targets:
        return

    yesterday_text = ""
    try:
        yesterday_stats = compute_stats("today")
        if yesterday_stats["total_chi"] > 0:
            yesterday_text = f"\nHôm qua chi: {format_amount(yesterday_stats['total_chi'])}"
    except Exception:
        pass

    for user_id in targets:
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
    app.add_handler(CommandHandler("worldcup", cmd_worldcup))

    # Conversation handlers (must be added before generic handlers)
    app.add_handler(get_editor_conversation_handler())
    app.add_handler(get_budget_conversation_handler())

    # Inline callbacks
    app.add_handler(CallbackQueryHandler(handle_edm_enter, pattern=r"^edm_enter_"))
    app.add_handler(CallbackQueryHandler(handle_edm_field, pattern=r"^edm_fld_"))
    app.add_handler(CallbackQueryHandler(handle_edm_setcat, pattern=r"^edm_setcat_"))
    app.add_handler(CallbackQueryHandler(handle_edm_setdate, pattern=r"^edm_setdate_"))
    app.add_handler(CallbackQueryHandler(handle_edm_inputdate, pattern=r"^edm_inputdate_"))
    app.add_handler(CallbackQueryHandler(handle_edm_setuser, pattern=r"^edm_setuser_"))
    app.add_handler(CallbackQueryHandler(handle_edm_back, pattern=r"^edm_back_"))
    app.add_handler(CallbackQueryHandler(handle_edm_save, pattern=r"^edm_save_"))
    app.add_handler(CallbackQueryHandler(handle_edm_cancel, pattern=r"^edm_cancel_"))
    app.add_handler(CallbackQueryHandler(handle_qdel, pattern=r"^qdel_"))
    app.add_handler(CallbackQueryHandler(handle_qdelok, pattern=r"^(qdelok_|qdelno_)"))
    app.add_handler(CallbackQueryHandler(handle_qexcl, pattern=r"^qexcl_"))
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

    job_queue = app.job_queue
    if job_queue:
        # Daily reminder at 10:00 Asia/Ho_Chi_Minh
        job_queue.run_daily(
            daily_reminder,
            time=dtime(hour=22, minute=0, tzinfo=TZ),
        )
        # World Cup scores at 07:00 Asia/Ho_Chi_Minh
        job_queue.run_daily(
            worldcup_morning,
            time=dtime(hour=7, minute=0, tzinfo=TZ),
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
    if "Message is not modified" in str(context.error):
        return
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

    if context.user_data.get("edm_waiting"):
        if await handle_edm_text_input(update, context):
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
