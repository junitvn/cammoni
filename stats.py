"""
Compute statistics from transaction records.
Default: all family members combined.
"""
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sheets import get_transactions_range, get_budgets, now_vn, TZ
from classifier import CATEGORY_INFO, CATEGORY_KEYS
from parser import format_amount
import users as user_store

PERIODS = {
    "today": "Hôm nay",
    "week": "Tuần này",
    "month": "Tháng này",
}


def _date_range(period: str) -> tuple[datetime, datetime]:
    now = now_vn()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = now
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
    else:
        raise ValueError(f"Unknown period: {period}")
    return start, end


async def compute_stats(
    period: str,
    user_id: Optional[str] = None,
    custom_start: Optional[datetime] = None,
    custom_end: Optional[datetime] = None,
) -> dict:
    if custom_start and custom_end:
        start, end = custom_start, custom_end
    else:
        start, end = _date_range(period)

    rows = await get_transactions_range(start, end, user_id=user_id)

    total_chi = 0
    total_thu = 0
    total_excluded_chi = 0
    by_cat: dict[str, int] = {k: 0 for k in CATEGORY_KEYS}

    for row in rows:
        excluded = str(row.get("excluded", "")).strip().upper() == "Y"
        try:
            amt = int(row.get("amount", 0))
        except (ValueError, TypeError):
            amt = 0
        tx_type = str(row.get("type", "chi"))
        cat = str(row.get("category", "khac"))

        if excluded:
            if tx_type == "chi":
                total_excluded_chi += amt
            continue

        if tx_type == "thu":
            total_thu += amt
        else:
            total_chi += amt
            if cat in by_cat:
                by_cat[cat] += amt
            else:
                by_cat["khac"] += amt

    so_du = total_thu - total_chi

    # Budget check (month only, when not custom)
    budgets = {}
    if period == "month" and not custom_start:
        budget_rows = await get_budgets()
        month_start, month_end = _date_range("month")
        all_month_rows = await get_transactions_range(month_start, month_end)

        monthly_by_cat: dict[str, int] = {k: 0 for k in CATEGORY_KEYS}
        monthly_total = 0
        for row in all_month_rows:
            if str(row.get("excluded", "")).strip().upper() == "Y":
                continue
            if str(row.get("type", "chi")) == "chi":
                try:
                    amt = int(row.get("amount", 0))
                except (ValueError, TypeError):
                    amt = 0
                cat = str(row.get("category", "khac"))
                if cat in monthly_by_cat:
                    monthly_by_cat[cat] += amt
                else:
                    monthly_by_cat["khac"] += amt
                monthly_total += amt

        for b_row in budget_rows:
            scope = str(b_row.get("scope", ""))
            try:
                limit = int(b_row.get("limit_vnd", 0))
            except (ValueError, TypeError):
                limit = 0
            if limit <= 0:
                continue
            used = monthly_total if scope == "chung" else monthly_by_cat.get(scope, 0)
            pct = (used / limit * 100) if limit else 0
            budgets[scope] = {"limit": limit, "used": used, "pct": pct}

    return {
        "period": period,
        "start": start,
        "end": end,
        "total_chi": total_chi,
        "total_thu": total_thu,
        "total_excluded_chi": total_excluded_chi,
        "so_du": so_du,
        "by_category": by_cat,
        "transactions": rows,
        "budgets": budgets,
    }


def format_top_text(rows: list, period_label: str, limit: int = 10) -> str:
    def _extract(tx_type: str) -> list:
        bucket = []
        for row in rows:
            if str(row.get("type", "chi")) != tx_type:
                continue
            try:
                amt = int(row.get("amount", 0))
            except (ValueError, TypeError):
                amt = 0
            if amt > 0:
                bucket.append(row | {"_amt": amt})
        bucket.sort(key=lambda r: r["_amt"], reverse=True)
        return bucket[:limit]

    def _section(items: list, counter_start: int) -> tuple[list[str], int]:
        lines = []
        for i, row in enumerate(items, counter_start):
            amt_str = format_amount(row["_amt"])
            cat = str(row.get("category", "khac"))
            info = CATEGORY_INFO.get(cat, {"emoji": "📦"})
            desc = str(row.get("description", ""))
            name = user_store.get_name(row.get("user", ""))
            name_str = f"{name} " if name else ""
            lines.append(f"{i}. {amt_str:<15}{info['emoji']} {name_str}{desc}")
        return lines, counter_start + len(items)

    top_thu = _extract("thu")
    top_chi = _extract("chi")

    if not top_thu and not top_chi:
        return f"🏆 *Top {period_label}*\n\nChưa có giao dịch nào."

    lines = [f"🏆 *Top {period_label}*"]
    n = 1
    if top_thu:
        lines.append("\n💰 *Thu nhập*")
        section_lines, n = _section(top_thu, n)
        lines.extend(section_lines)
    if top_chi:
        lines.append("\n💸 *Chi tiêu*")
        section_lines, _ = _section(top_chi, n)
        lines.extend(section_lines)

    return "\n".join(lines)


def format_stats_text(stats: dict) -> str:
    lines = []
    if stats["period"] == "month" and stats.get("start"):
        s = stats["start"]
        period_label = f"Tháng {s.month}/{s.year}"
    else:
        period_label = PERIODS.get(stats["period"], "Khoảng thời gian")
    lines.append(f"📊 *{period_label}*")
    lines.append(f"💸 Chi: {format_amount(stats['total_chi'])}")
    excluded_chi = stats.get("total_excluded_chi", 0)
    if excluded_chi:
        lines.append(f"🚫 Chi ngoài ngân sách: {format_amount(excluded_chi)}")
    lines.append(f"💰 Thu: {format_amount(stats['total_thu'])}")
    so_du = stats["so_du"]
    if so_du >= 0:
        so_du_str = f"+{format_amount(so_du)}"
    else:
        so_du_str = f"-{format_amount(abs(so_du))}"
    lines.append(f"🏦 Số dư: {so_du_str}")
    lines.append("")
    lines.append("*Theo mục chi:*")

    total_chi = stats["total_chi"] or 1
    for key in CATEGORY_KEYS:
        amt = stats["by_category"].get(key, 0)
        if amt == 0:
            continue
        info = CATEGORY_INFO.get(key, {"emoji": "📦", "name": key})
        pct = amt / total_chi * 100
        lines.append(f"  {info['emoji']} {info['name']}: {format_amount(amt)} ({pct:.0f}%)")

    for scope, b in stats.get("budgets", {}).items():
        pct = b["pct"]
        if pct >= 100:
            scope_label = "Tổng" if scope == "chung" else scope
            lines.append(
                f"\n🔴 *{scope_label}* vượt ngân sách! "
                f"{format_amount(b['used'])}/{format_amount(b['limit'])} ({pct:.0f}%)"
            )
        elif pct >= 80:
            scope_label = "Tổng" if scope == "chung" else scope
            lines.append(
                f"\n⚠️ *{scope_label}* đã dùng {pct:.0f}% ngân sách "
                f"({format_amount(b['used'])}/{format_amount(b['limit'])})"
            )

    return "\n".join(lines)


async def check_budget_warning(category: str, added_amount: int) -> Optional[str]:
    budget_rows = await get_budgets()
    if not budget_rows:
        return None

    month_start, month_end = _date_range("month")
    all_rows = await get_transactions_range(month_start, month_end)

    monthly_total = 0
    monthly_by_cat: dict[str, int] = {k: 0 for k in CATEGORY_KEYS}
    for row in all_rows:
        if str(row.get("excluded", "")).strip().upper() == "Y":
            continue
        if str(row.get("type", "chi")) == "chi":
            try:
                amt = int(row.get("amount", 0))
            except (ValueError, TypeError):
                amt = 0
            cat = str(row.get("category", "khac"))
            if cat in monthly_by_cat:
                monthly_by_cat[cat] += amt
            else:
                monthly_by_cat["khac"] += amt
            monthly_total += amt

    warnings = []
    for b in budget_rows:
        scope = str(b.get("scope", ""))
        try:
            limit = int(b.get("limit_vnd", 0))
        except (ValueError, TypeError):
            limit = 0
        if limit <= 0:
            continue

        if scope == "chung":
            used = monthly_total
            label = "Tổng chi tháng này"
        elif scope == category:
            used = monthly_by_cat.get(category, 0)
            info = CATEGORY_INFO.get(category, {"name": category})
            label = info["name"]
        else:
            continue

        pct = used / limit * 100 if limit else 0
        if pct >= 100:
            warnings.append(
                f"🔴 {label} vượt ngân sách! {format_amount(used)}/{format_amount(limit)} ({pct:.0f}%)"
            )
        elif pct >= 80:
            warnings.append(
                f"⚠️ {label} đã dùng {format_amount(used)}/{format_amount(limit)} ({pct:.0f}%)"
            )

    return "\n".join(warnings) if warnings else None
