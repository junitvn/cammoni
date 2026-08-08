"""
Aggregation helpers for the webapp API, built on top of moni-bot's existing
sheets.py primitives. Timestamps are exchanged with the frontend as ISO-8601;
the sheet itself stores them as 'dd/mm/yyyy HH:MM' (see sheets.format_ts/parse_ts).
"""
from datetime import datetime, timedelta
from typing import Optional

from sheets import TZ, get_transactions_range, now_vn, parse_ts


def parse_client_timestamp(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp from the frontend into a TZ-aware VN datetime."""
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def format_client_timestamp(row_ts: str) -> str:
    """Convert the sheet's 'dd/mm/yyyy HH:MM' string into ISO-8601 for the API response."""
    dt = parse_ts(row_ts)
    return dt.isoformat() if dt else row_ts


def row_to_out(row: dict) -> dict:
    try:
        amount = int(float(row.get("amount", 0)))
    except (ValueError, TypeError):
        amount = 0
    return {
        "id": row.get("id", ""),
        "timestamp": format_client_timestamp(row.get("timestamp", "")),
        "type": row.get("type", "chi"),
        "amount": amount,
        "category": row.get("category", "khac"),
        "description": row.get("description", ""),
        "user_name": row.get("user_name", ""),
        "excluded": str(row.get("excluded", "")).strip().upper() == "Y",
    }


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    """month = 'YYYY-MM'."""
    year, mo = (int(x) for x in month.split("-"))
    start = datetime(year, mo, 1, tzinfo=TZ)
    next_start = datetime(year + 1, 1, 1, tzinfo=TZ) if mo == 12 else datetime(year, mo + 1, 1, tzinfo=TZ)
    return start, next_start - timedelta(seconds=1)


def _day_label(dt: datetime, now: datetime) -> str:
    today = now.date()
    d = dt.date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return dt.strftime("%A, %d.%m.%Y")


def _range_bounds(start: str, end: str) -> tuple[datetime, datetime]:
    """start/end = 'YYYY-MM-DD'; end is clamped to now."""
    sy, sm, sd = (int(x) for x in start.split("-"))
    ey, em, ed = (int(x) for x in end.split("-"))
    start_dt = datetime(sy, sm, sd, tzinfo=TZ)
    end_dt = min(datetime(ey, em, ed, 23, 59, 59, tzinfo=TZ), now_vn())
    return start_dt, end_dt


def _month_keys(start: datetime, end: datetime) -> list[str]:
    """Every 'YYYY-MM' from start through end, inclusive, spanning year boundaries."""
    keys = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        keys.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return keys


async def get_analysis_summary(start: str, end: str) -> dict:
    """Expense-by-category breakdown and expense/income totals per month, over [start, end]."""
    start, end = _range_bounds(start, end)
    rows = await get_transactions_range(start, end)

    by_category: dict[str, int] = {}
    monthly: dict[str, dict[str, int]] = {k: {"expense": 0, "income": 0} for k in _month_keys(start, end)}

    for row in rows:
        if str(row.get("excluded", "")).strip().upper() == "Y":
            continue
        ts = parse_ts(row.get("timestamp", ""))
        if not ts:
            continue
        try:
            amt = int(float(row.get("amount", 0)))
        except (ValueError, TypeError):
            amt = 0
        tx_type = row.get("type", "chi")
        month_key = f"{ts.year}-{ts.month:02d}"
        bucket = monthly.setdefault(month_key, {"expense": 0, "income": 0})
        if tx_type == "thu":
            bucket["income"] += amt
        else:
            bucket["expense"] += amt
            category = row.get("category", "khac")
            by_category[category] = by_category.get(category, 0) + amt

    return {
        "by_category": [{"category": k, "amount": v} for k, v in sorted(by_category.items(), key=lambda p: -p[1])],
        "monthly": [{"month": k, **v} for k, v in sorted(monthly.items())],
    }


async def get_category_transactions(category: str, start: str, end: str) -> list[dict]:
    """Non-excluded, non-income rows for one category, over [start, end], most recent first."""
    start, end = _range_bounds(start, end)
    rows = await get_transactions_range(start, end)
    matching = [
        row for row in rows
        if row.get("category") == category
        and row.get("type", "chi") != "thu"
        and str(row.get("excluded", "")).strip().upper() != "Y"
    ]
    matching.sort(key=lambda r: parse_ts(r.get("timestamp", "")) or start, reverse=True)
    return matching


async def get_home_summary(month: str) -> dict:
    now = now_vn()
    start, end = _month_bounds(month)
    rows = await get_transactions_range(start, end)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_expense = today_income = 0
    month_expense = month_income = 0

    dated_rows: list[tuple[datetime, dict]] = []
    for row in rows:
        ts = parse_ts(row.get("timestamp", ""))
        if not ts:
            continue
        try:
            amt = int(float(row.get("amount", 0)))
        except (ValueError, TypeError):
            amt = 0
        excluded = str(row.get("excluded", "")).strip().upper() == "Y"
        tx_type = row.get("type", "chi")

        if not excluded:
            if tx_type == "thu":
                month_income += amt
            else:
                month_expense += amt
            if ts >= today_start:
                if tx_type == "thu":
                    today_income += amt
                else:
                    today_expense += amt

        dated_rows.append((ts, row))

    dated_rows.sort(key=lambda p: p[0], reverse=True)

    groups: list[dict] = []
    current_label = None
    current_bucket: list[dict] = []
    for ts, row in dated_rows:
        label = _day_label(ts, now)
        if label != current_label:
            if current_bucket:
                groups.append({"label": current_label, "transactions": current_bucket})
            current_label = label
            current_bucket = []
        current_bucket.append(row_to_out(row))
    if current_bucket:
        groups.append({"label": current_label, "transactions": current_bucket})

    return {
        "month": month,
        "today": {"expense": today_expense, "income": today_income},
        "month_totals": {"expense": month_expense, "income": month_income},
        "groups": groups,
    }
