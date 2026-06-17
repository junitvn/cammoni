"""
Fetch World Cup match scores from football-data.org (free tier).
Requires FOOTBALL_API_KEY env var — register free at https://www.football-data.org/client/register
"""
import os
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
logger = logging.getLogger(__name__)

_API_BASE = "https://api.football-data.org/v4"
_COMPETITION = "WC"  # FIFA World Cup


def _yesterday_vn() -> date:
    return (datetime.now(tz=TZ) - timedelta(days=1)).date()


def _fmt_time_vn(utc_date_str: str) -> str:
    """Convert ISO UTC datetime string to VN HH:MM."""
    try:
        dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
        dt_vn = dt.astimezone(TZ)
        return dt_vn.strftime("%H:%M")
    except Exception:
        return ""


async def fetch_worldcup_scores(target_date: date | None = None) -> str:
    """Return a formatted string of World Cup results for target_date (default: yesterday VN)."""
    api_key = os.getenv("FOOTBALL_API_KEY", "").strip()
    if not api_key:
        return "⚽ Chưa cấu hình `FOOTBALL_API_KEY`. Đăng ký miễn phí tại football-data.org."

    if target_date is None:
        target_date = _yesterday_vn()

    date_str = target_date.strftime("%Y-%m-%d")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_API_BASE}/competitions/{_COMPETITION}/matches",
                headers={"X-Auth-Token": api_key},
                params={"dateFrom": date_str, "dateTo": date_str},
            )
            if resp.status_code == 404:
                return f"⚽ Không tìm thấy dữ liệu World Cup ngày {date_str}."
            if resp.status_code == 403:
                return "⚽ API key không hợp lệ hoặc chưa được kích hoạt."
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        return "⚽ Timeout khi lấy kết quả World Cup."
    except Exception as e:
        logger.error(f"worldcup fetch error: {e}")
        return f"⚽ Lỗi lấy kết quả World Cup: {e}"

    matches = data.get("matches", [])
    if not matches:
        return f"⚽ Không có trận đấu World Cup nào ngày {date_str}."

    day_label = target_date.strftime("%d/%m/%Y")
    lines = [f"⚽ *Kết quả World Cup {day_label}*\n"]

    for m in matches:
        home = m["homeTeam"].get("shortName") or m["homeTeam"].get("name", "?")
        away = m["awayTeam"].get("shortName") or m["awayTeam"].get("name", "?")
        score = m.get("score", {})
        ft = score.get("fullTime", {})
        home_goals = ft.get("home")
        away_goals = ft.get("away")
        status = m.get("status", "")
        utc_date = m.get("utcDate", "")

        if status == "FINISHED" and home_goals is not None:
            lines.append(f"• {home} *{home_goals}–{away_goals}* {away} ✅")
        elif status in ("IN_PLAY", "PAUSED", "HALFTIME"):
            ht = score.get("halfTime", {})
            ht_h = ht.get("home", "?")
            ht_a = ht.get("away", "?")
            lines.append(f"• {home} *{home_goals}–{away_goals}* {away} 🔴 (đang đá)")
        elif status in ("SCHEDULED", "TIMED"):
            kick_off = _fmt_time_vn(utc_date)
            lines.append(f"• {home} vs {away} ⏰ {kick_off}")
        elif status == "POSTPONED":
            lines.append(f"• {home} vs {away} ⏸ (hoãn)")
        elif status == "CANCELLED":
            lines.append(f"• {home} vs {away} ❌ (hủy)")
        else:
            lines.append(f"• {home} vs {away} ({status})")

    return "\n".join(lines)
