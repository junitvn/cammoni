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
