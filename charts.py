"""
Generate matplotlib charts and return PNG bytes.
"""
import io
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib import rcParams

from classifier import CATEGORY_INFO, CATEGORY_KEYS
from parser import format_amount

# Use a font that supports Vietnamese
rcParams["font.family"] = ["DejaVu Sans", "Noto Sans", "sans-serif"]

# Category colors
_COLORS = ["#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7", "#DDA0DD"]
_CAT_COLORS = {key: _COLORS[i % len(_COLORS)] for i, key in enumerate(CATEGORY_KEYS)}


def _make_pie(by_category: dict[str, int], title: str) -> bytes:
    labels = []
    sizes = []
    colors = []

    for key in CATEGORY_KEYS:
        amt = by_category.get(key, 0)
        if amt > 0:
            info = CATEGORY_INFO[key]
            labels.append(f"{info['emoji']} {info['name']}\n{format_amount(amt)}")
            sizes.append(amt)
            colors.append(_CAT_COLORS[key])

    if not sizes:
        return _empty_chart("Chưa có dữ liệu")

    fig, ax = plt.subplots(figsize=(7, 5))
    wedges, texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        colors=colors,
        autopct="%1.0f%%",
        startangle=140,
        pctdistance=0.8,
        textprops={"fontsize": 8},
    )
    for at in autotexts:
        at.set_fontsize(7)
    ax.set_title(title, fontsize=12, fontweight="bold", pad=15)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _make_bar(transactions: list[dict], title: str) -> bytes:
    """Daily bar chart grouped by category."""
    from collections import defaultdict
    from sheets import parse_ts

    daily: dict[str, dict[str, int]] = defaultdict(lambda: {k: 0 for k in CATEGORY_KEYS})

    for row in transactions:
        if str(row.get("type", "chi")) != "chi":
            continue
        ts = parse_ts(str(row.get("timestamp", "")))
        if not ts:
            continue
        day = ts.strftime("%d/%m")
        cat = str(row.get("category", "khac"))
        try:
            amt = int(row.get("amount", 0))
        except (ValueError, TypeError):
            amt = 0
        if cat in daily[day]:
            daily[day][cat] += amt
        else:
            daily[day]["khac"] += amt

    if not daily:
        return _empty_chart("Chưa có dữ liệu")

    days = sorted(daily.keys(), key=lambda d: datetime.strptime(d, "%d/%m"))

    fig, ax = plt.subplots(figsize=(10, 5))
    bar_width = 0.6
    bottoms = [0] * len(days)

    for key in CATEGORY_KEYS:
        values = [daily[d].get(key, 0) / 1_000_000 for d in days]  # show as millions
        if sum(values) == 0:
            continue
        info = CATEGORY_INFO[key]
        ax.bar(days, values, bar_width, bottom=bottoms, color=_CAT_COLORS[key],
               label=f"{info['emoji']} {info['name']}")
        bottoms = [b + v for b, v in zip(bottoms, values)]

    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel("Ngày")
    ax.set_ylabel("Triệu VNĐ")
    ax.legend(loc="upper right", fontsize=7)
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _empty_chart(message: str) -> bytes:
    fig, ax = plt.subplots(figsize=(5, 3))
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=14)
    ax.axis("off")
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generate_charts(stats: dict) -> tuple[bytes, bytes]:
    """
    Returns (pie_png_bytes, bar_png_bytes).
    """
    period_label = {
        "today": "Hôm nay",
        "week": "Tuần này",
        "month": "Tháng này",
    }.get(stats.get("period", ""), "Khoảng thời gian")

    pie = _make_pie(stats["by_category"], f"Chi tiêu {period_label}")
    bar = _make_bar(stats["transactions"], f"Chi tiêu theo ngày — {period_label}")
    return pie, bar
