"""
Google Sheets CRUD via direct REST API calls using httpx (async).
Using httpx instead of gspread/requests because httpx is ~40s faster on this machine
for the initial TCP connection to sheets.googleapis.com.
"""
import os
import uuid
import logging
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import time
import json

import httpx
from parser import normalize_vn
import google.auth.crypt
import google.auth.jwt

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

# Column indices (0-based) for Transactions sheet
COL_ID = 0
COL_TIMESTAMP = 1
COL_USER = 2
COL_TYPE = 3
COL_AMOUNT = 4
COL_CATEGORY = 5
COL_DESCRIPTION = 6
COL_AUTO = 7
COL_USER_NAME = 8
COL_EXCLUDED = 9

TRANSACTIONS_HEADER = [
    "id", "timestamp", "user", "type", "amount",
    "category", "description", "auto_classified", "user_name", "excluded"
]
BUDGET_HEADER = ["scope", "limit_vnd", "period"]
CONFIG_HEADER = ["description", "category"]
CATEGORIES_HEADER = ["key", "name", "emoji", "income", "keywords"]
USERS_HEADER = ["user_id", "name"]

# Default categories seeded on first run
_CATEGORIES_SEED = [
    ["an_ngoai", "Ăn ngoài", "🍜", "",
     "cơm, phở, bún, trà sữa, cà phê, cafe, ăn, quán, nhậu, ship đồ ăn, giao đồ ăn, shopeefood, grabfood, baemin, pizza, burger, sushi, lẩu, bánh mì, bánh, nước, trà, bia, nhà hàng, fast food, kfc, mcdonalds, highlands, starbucks, the coffee house, gà, vịt, hải sản, bò né, hủ tiếu, mì, dimsum, hotpot"],
    ["di_cho", "Đi chợ", "🛒", "",
     "chợ, rau, thịt, cá, siêu thị, bách hóa, đồ ăn, gạo, vinmart, winmart, coopmart, bigc, lotte mart, aeon, go!, tops market, trứng, sữa, hoa quả, trái cây, mắm, muối, dầu ăn, gia vị, mì tôm, đồ khô, tạp hóa, quầy, mua đồ, thực phẩm"],
    ["bat_buoc", "Chi tiêu bắt buộc", "📌", "",
     "điện, nước, internet, wifi, tiền nhà, thuê nhà, học phí, bảo hiểm, thuế, điện thoại, phone, sim, phí, hóa đơn, bill, trả góp, vay, nợ, đóng tiền, học, trường, gas"],
    ["y_te", "Y tế", "🏥", "",
     "viện phí, bệnh viện, khám bệnh, thuốc, dược, phòng khám, cấp cứu, nha khoa, mắt, da liễu, xét nghiệm, siêu âm, chụp x-quang, vaccine, tiêm phòng, spa, thẩm mỹ, khám, thuốc"],
    ["phuong_tien", "Phương tiện đi lại", "🚗", "",
     "grab, xe, xăng, gửi xe, taxi, vé, sửa xe, bus, xe bus, tàu, máy bay, vé tàu, vé máy bay, uber, be, gojek, xe ôm, đỗ xe, parking, rửa xe, bảo dưỡng xe, thay nhớt, lốp xe, ắc quy, đăng kiểm, bằng lái, phí cầu đường, eto, vinbus"],
    ["dau_tu", "Đầu tư", "📈", "",
     "chứng khoán, vàng, cổ phiếu, gửi tiết kiệm, tiết kiệm, crypto, quỹ, bitcoin, eth, usdt, vnindex, fpt, vcb, hpg, stock, invest, đầu tư, mở tài khoản, nạp tiền đầu tư, mua vàng, tích lũy"],
    ["khac", "Chi tiêu khác", "📦", "", ""],
    ["luong", "Lương", "💼", "yes",
     "lương, salary, thưởng, bonus, lương tháng, lương tuần, phụ cấp, hoa hồng"],
    ["thu_khac", "Thu nhập khác", "💵", "yes", ""],
]

# ── Auth ──────────────────────────────────────────────────────────────────────

_service_account_info: Optional[dict] = None
_access_token: Optional[str] = None
_token_expiry: float = 0.0
_httpx_client: Optional[httpx.AsyncClient] = None
_config_cache: Optional[dict] = None


def _load_service_account() -> dict:
    global _service_account_info
    if _service_account_info is None:
        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        with open(creds_file) as f:
            _service_account_info = json.load(f)
    return _service_account_info


def get_httpx_client() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None or _httpx_client.is_closed:
        _httpx_client = httpx.AsyncClient(timeout=60.0)
    return _httpx_client


async def _get_access_token() -> str:
    """Get/refresh OAuth2 access token via httpx (no requests library)."""
    global _access_token, _token_expiry
    now = time.time()
    if _access_token and now < _token_expiry - 60:
        return _access_token

    info = _load_service_account()
    signer = google.auth.crypt.RSASigner.from_service_account_info(info)
    now_int = int(now)
    payload = {
        "iss": info["client_email"],
        "sub": info["client_email"],
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now_int,
        "exp": now_int + 3600,
        "scope": " ".join(SCOPES),
    }
    assertion = google.auth.jwt.encode(signer, payload).decode("utf-8")

    client = get_httpx_client()
    r = await client.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
    )
    r.raise_for_status()
    data = r.json()
    _access_token = data["access_token"]
    _token_expiry = now + data.get("expires_in", 3600)
    logger.info("[sheets] access token refreshed")
    return _access_token


async def _auth_headers() -> dict:
    token = await _get_access_token()
    return {"Authorization": f"Bearer {token}"}


def _sheet_id() -> str:
    return os.getenv("GOOGLE_SHEET_ID", "")


# ── Sheet init ────────────────────────────────────────────────────────────────

async def init_sheets() -> None:
    """Ensure monthly transaction sheets and shared sheets exist with correct headers."""
    logger.info("[sheets] init_sheets: checking worksheets...")
    sheet_id = _sheet_id()
    client = get_httpx_client()

    r = await client.get(f"{SHEETS_BASE}/{sheet_id}", headers=await _auth_headers())
    r.raise_for_status()
    meta = r.json()
    sheet_list = meta.get("sheets", [])
    existing_titles = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheet_list}
    logger.info(f"[sheets] init_sheets: existing={list(existing_titles.keys())}")

    current_month = _month_sheet_name()

    # One-time migration: rename legacy "Transactions" → current month sheet
    if "Transactions" in existing_titles and current_month not in existing_titles:
        r = await client.post(
            f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
            headers=await _auth_headers(),
            json={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": existing_titles["Transactions"], "title": current_month},
                "fields": "title",
            }}]},
        )
        r.raise_for_status()
        existing_titles[current_month] = existing_titles.pop("Transactions")
        logger.info(f"[sheets] migrated 'Transactions' → '{current_month}'")

    # Create shared sheets if missing
    requests_body = []
    for name in ("Budget", "Config", "Categories", "Users"):
        if name not in existing_titles:
            requests_body.append({"addSheet": {"properties": {
                "title": name, "gridProperties": {"rowCount": 1000, "columnCount": 20},
            }}})
    if requests_body:
        r = await client.post(
            f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
            headers=await _auth_headers(),
            json={"requests": requests_body},
        )
        r.raise_for_status()

    # Ensure headers for shared sheets
    for name, header in [
        ("Budget", BUDGET_HEADER),
        ("Config", CONFIG_HEADER),
        ("Categories", CATEGORIES_HEADER),
        ("Users", USERS_HEADER),
    ]:
        values = await _get_values(f"{name}!1:1")
        cur = values[0] if values else []
        if cur == header:
            continue
        if cur and header[:len(cur)] == cur:
            extra = header[len(cur):]
            sc = chr(ord('A') + len(cur))
            ec = chr(ord('A') + len(header) - 1)
            await _set_values(f"{name}!{sc}1:{ec}1", [extra])
        else:
            await _set_values(f"{name}!A1", [header])
        logger.info(f"[sheets] wrote header for '{name}'")

    # Seed Categories if empty
    cat_rows = await _get_values("Categories!A:E")
    if len(cat_rows) <= 1:
        await _append_values("Categories!A:E", _CATEGORIES_SEED)
        logger.info("[sheets] init_sheets: seeded Categories")

    # Ensure current month sheet exists
    await _ensure_month_sheet(current_month)

    logger.info("[sheets] init_sheets: done")


# ── Low-level helpers ─────────────────────────────────────────────────────────

async def _get_values(range_: str) -> list[list]:
    sheet_id = _sheet_id()
    client = get_httpx_client()
    r = await client.get(
        f"{SHEETS_BASE}/{sheet_id}/values/{range_}",
        headers=await _auth_headers(),
    )
    r.raise_for_status()
    return r.json().get("values", [])


async def _append_values(range_: str, values: list[list]) -> None:
    sheet_id = _sheet_id()
    client = get_httpx_client()
    r = await client.post(
        f"{SHEETS_BASE}/{sheet_id}/values/{range_}:append",
        headers=await _auth_headers(),
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": values},
    )
    r.raise_for_status()


async def _set_values(range_: str, values: list[list]) -> None:
    sheet_id = _sheet_id()
    client = get_httpx_client()
    r = await client.put(
        f"{SHEETS_BASE}/{sheet_id}/values/{range_}",
        headers=await _auth_headers(),
        params={"valueInputOption": "RAW"},
        json={"values": values},
    )
    r.raise_for_status()


async def _delete_row(sheet_gid: int, row_index: int) -> None:
    """Delete row by 0-based index."""
    sheet_id = _sheet_id()
    client = get_httpx_client()
    r = await client.post(
        f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
        headers=await _auth_headers(),
        json={"requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_gid,
                    "dimension": "ROWS",
                    "startIndex": row_index,
                    "endIndex": row_index + 1,
                }
            }
        }]},
    )
    r.raise_for_status()


async def _get_sheet_gid(sheet_name: str) -> int:
    sheet_id = _sheet_id()
    client = get_httpx_client()
    r = await client.get(f"{SHEETS_BASE}/{sheet_id}", headers=await _auth_headers())
    r.raise_for_status()
    for s in r.json().get("sheets", []):
        if s["properties"]["title"] == sheet_name:
            return s["properties"]["sheetId"]
    raise ValueError(f"Sheet '{sheet_name}' not found")


# ── Monthly sheet helpers ─────────────────────────────────────────────────────

# Cache of sheets already confirmed to exist (avoids repeated API calls)
_known_month_sheets: set[str] = set()


def _month_sheet_name(dt: Optional[datetime] = None) -> str:
    """Sheet name for a given month, e.g. 'T6/2026'."""
    dt = dt or datetime.now(TZ)
    return f"T{dt.month}/{dt.year}"


def _prev_month(dt: datetime) -> datetime:
    """Return a datetime in the previous month."""
    if dt.month == 1:
        return dt.replace(year=dt.year - 1, month=12, day=1)
    return dt.replace(month=dt.month - 1, day=1)


def _months_in_range(start: datetime, end: datetime) -> list[str]:
    """Return ordered list of month sheet names covering [start, end]."""
    names = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        names.append(f"T{month}/{year}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return names


def _sr(sheet_name: str, range_: str) -> str:
    """Format 'SheetName!A:J', quoting sheet name if it contains special chars."""
    if any(c in sheet_name for c in " /\\!#"):
        return f"'{sheet_name}'!{range_}"
    return f"{sheet_name}!{range_}"


async def _safe_get_values(range_: str) -> list[list]:
    """Like _get_values but returns [] instead of raising on missing/bad sheet."""
    try:
        return await _get_values(range_)
    except Exception:
        return []


async def _ensure_month_sheet(name: str) -> None:
    """Create the month sheet with header if it doesn't exist yet."""
    global _known_month_sheets
    if name in _known_month_sheets:
        return
    values = await _safe_get_values(_sr(name, "1:1"))
    if values and values[0] == TRANSACTIONS_HEADER:
        _known_month_sheets.add(name)
        return
    if values and values[0] and TRANSACTIONS_HEADER[:len(values[0])] == values[0]:
        # Header exists but missing new columns — patch
        extra = TRANSACTIONS_HEADER[len(values[0]):]
        start_col = chr(ord('A') + len(values[0]))
        end_col = chr(ord('A') + len(TRANSACTIONS_HEADER) - 1)
        await _set_values(_sr(name, f"{start_col}1:{end_col}1"), [extra])
        _known_month_sheets.add(name)
        return
    if not values:
        # Sheet doesn't exist — create it
        sheet_id = _sheet_id()
        client = get_httpx_client()
        r = await client.post(
            f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
            headers=await _auth_headers(),
            json={"requests": [{"addSheet": {"properties": {
                "title": name,
                "gridProperties": {"rowCount": 2000, "columnCount": 12},
            }}}]},
        )
        r.raise_for_status()
        logger.info(f"[sheets] created month sheet '{name}'")
    await _set_values(_sr(name, "A1"), [TRANSACTIONS_HEADER])
    _known_month_sheets.add(name)
    logger.info(f"[sheets] wrote header for '{name}'")


# ── Time helpers ──────────────────────────────────────────────────────────────

def now_vn() -> datetime:
    return datetime.now(TZ)


def format_ts(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y %H:%M")


def parse_ts(s) -> Optional[datetime]:
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    for fmt in (
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d/%m %H:%M:%S",
        "%d/%m %H:%M",
        "%d/%m",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=now_vn().year)
            return dt.replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


# ── Transactions ──────────────────────────────────────────────────────────────

async def add_transaction(
    user_id: str,
    tx_type: str,
    amount: int,
    category: str,
    description: str,
    auto_classified: bool,
    timestamp: Optional[datetime] = None,
    tx_id: Optional[str] = None,
    user_name: str = "",
    excluded: bool = False,
) -> str:
    if timestamp is None:
        timestamp = now_vn()
    if tx_id is None:
        tx_id = str(uuid.uuid4())[:8]

    row = [
        tx_id,
        format_ts(timestamp),
        str(user_id),
        tx_type,
        amount,
        category,
        description,
        "Y" if auto_classified else "N",
        user_name,
        "Y" if excluded else "",
    ]
    sheet_name = _month_sheet_name(timestamp)
    await _ensure_month_sheet(sheet_name)
    logger.info(f"[sheets] add_transaction: writing tx_id={tx_id} to '{sheet_name}'")
    await _append_values(_sr(sheet_name, "A:J"), [row])
    logger.info(f"[sheets] add_transaction: done tx_id={tx_id}")
    return tx_id


def _filter_row(d: dict, user_id, keyword, category, amount_min, amount_max) -> bool:
    """Return True if row passes all filters."""
    if user_id and d.get("user") != str(user_id):
        return False
    if keyword and normalize_vn(keyword) not in normalize_vn(d.get("description", "")):
        return False
    if category and d.get("category") != category:
        return False
    if amount_min is not None or amount_max is not None:
        try:
            amt = int(float(str(d.get("amount", 0))))
        except (ValueError, TypeError):
            amt = 0
        if amount_min is not None and amt < amount_min:
            return False
        if amount_max is not None and amt > amount_max:
            return False
    return True


async def get_recent_transactions(
    user_id: Optional[str] = None,
    limit: int = 10,
    keyword: Optional[str] = None,
    amount_min: Optional[int] = None,
    amount_max: Optional[int] = None,
    category: Optional[str] = None,
) -> list[dict]:
    """Fetch recent transactions across monthly sheets (newest first)."""
    now = now_vn()
    all_results: list[dict] = []

    for i in range(6):  # search up to 6 months back
        if i == 0:
            month_dt = now
        else:
            month_dt = now
            for _ in range(i):
                month_dt = _prev_month(month_dt)
        sheet_name = _month_sheet_name(month_dt)
        rows = await _safe_get_values(_sr(sheet_name, "A:J"))
        if not rows or rows[0] != TRANSACTIONS_HEADER:
            if i > 0:
                break  # no more historical months
            continue
        month_results = []
        for j, row in enumerate(rows[1:], start=2):
            row = _pad_row(row, len(TRANSACTIONS_HEADER))
            d = dict(zip(TRANSACTIONS_HEADER, row))
            d["_row"] = j
            d["_sheet"] = sheet_name
            if _filter_row(d, user_id, keyword, category, amount_min, amount_max):
                month_results.append(d)
        # Prepend older month's rows so all_results stays chronological
        all_results = month_results + all_results
        if len(all_results) >= limit * 2:
            break

    return all_results[-limit:][::-1]


async def get_transaction_by_id(tx_id: str) -> Optional[tuple[int, dict, str]]:
    """Returns (row_index, row_dict, sheet_name) or None. Searches recent months."""
    now = now_vn()
    for i in range(6):
        if i == 0:
            month_dt = now
        else:
            month_dt = now
            for _ in range(i):
                month_dt = _prev_month(month_dt)
        sheet_name = _month_sheet_name(month_dt)
        rows = await _safe_get_values(_sr(sheet_name, "A:J"))
        if not rows:
            if i > 0:
                break
            continue
        for j, row in enumerate(rows[1:], start=2):
            row = _pad_row(row, len(TRANSACTIONS_HEADER))
            if row[COL_ID] == tx_id:
                return j, dict(zip(TRANSACTIONS_HEADER, row)), sheet_name
    return None


async def update_transaction_field(tx_id: str, field: str, value) -> bool:
    col_map = {
        "timestamp": COL_TIMESTAMP,
        "user": COL_USER,
        "type": COL_TYPE,
        "amount": COL_AMOUNT,
        "category": COL_CATEGORY,
        "description": COL_DESCRIPTION,
        "auto_classified": COL_AUTO,
        "user_name": COL_USER_NAME,
        "excluded": COL_EXCLUDED,
    }
    col = col_map.get(field)
    if col is None:
        return False

    result = await get_transaction_by_id(tx_id)
    if not result:
        return False
    row_idx, _, sheet_name = result
    col_letter = chr(ord('A') + col)
    await _set_values(_sr(sheet_name, f"{col_letter}{row_idx}"), [[value]])
    return True


async def delete_transaction(tx_id: str) -> bool:
    result = await get_transaction_by_id(tx_id)
    if not result:
        return False
    row_idx, _, sheet_name = result
    gid = await _get_sheet_gid(sheet_name)
    await _delete_row(gid, row_idx - 1)
    return True


async def get_transactions_range(
    start: datetime,
    end: datetime,
    user_id: Optional[str] = None,
) -> list[dict]:
    """Read transactions across all monthly sheets in the date range."""
    sheet_names = _months_in_range(start, end)
    results = []
    for sheet_name in sheet_names:
        rows = await _safe_get_values(_sr(sheet_name, "A:J"))
        if not rows or rows[0] != TRANSACTIONS_HEADER:
            continue
        for row in rows[1:]:
            row = _pad_row(row, len(TRANSACTIONS_HEADER))
            d = dict(zip(TRANSACTIONS_HEADER, row))
            ts = parse_ts(d.get("timestamp", ""))
            if not ts or ts < start or ts > end:
                continue
            if user_id and d.get("user") != str(user_id):
                continue
            results.append(d)
    return results


# ── Budget ────────────────────────────────────────────────────────────────────

async def get_budgets() -> list[dict]:
    rows = await _get_values("Budget!A:C")
    if not rows or rows[0] != BUDGET_HEADER:
        return []
    return [dict(zip(BUDGET_HEADER, _pad_row(r, 3))) for r in rows[1:]]


async def load_categories_from_sheet() -> dict:
    """Load categories from the Categories sheet. Returns dict keyed by category key."""
    rows = await _get_values("Categories!A:E")
    if not rows or rows[0] != CATEGORIES_HEADER:
        return {}
    result = {}
    for row in rows[1:]:
        row = _pad_row(row, 5)
        key, name, emoji, income_flag, keywords_raw = row
        if not key:
            continue
        keywords = [kw.strip() for kw in keywords_raw.split(",") if kw.strip()] if keywords_raw else []
        result[key] = {
            "name": name,
            "emoji": emoji,
            "keywords": keywords,
            "income": income_flag.lower() in ("yes", "true", "1"),
        }
    logger.info(f"[sheets] loaded {len(result)} categories from sheet")
    return result


async def load_users_from_sheet() -> set[int]:
    """Load allowed user IDs from the Users sheet."""
    rows = await _get_values("Users!A:B")
    if not rows or rows[0] != USERS_HEADER:
        return set()
    result = set()
    for row in rows[1:]:
        row = _pad_row(row, 2)
        try:
            result.add(int(row[0]))
        except (ValueError, TypeError):
            pass
    logger.info(f"[sheets] loaded {len(result)} users from sheet")
    return result


async def load_user_names_from_sheet() -> dict[int, str]:
    """Load {user_id: name} mapping from the Users sheet."""
    rows = await _get_values("Users!A:B")
    if not rows or rows[0] != USERS_HEADER:
        return {}
    result = {}
    for row in rows[1:]:
        row = _pad_row(row, 2)
        try:
            uid = int(row[0])
            name = str(row[1]).strip()
            if name:
                result[uid] = name
        except (ValueError, TypeError):
            pass
    return result


async def get_config_mappings() -> dict[str, str]:
    """Returns {description_lower: category_key} from Config sheet, with in-memory cache."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    rows = await _get_values("Config!A:B")
    if not rows or rows[0] != CONFIG_HEADER:
        _config_cache = {}
        return _config_cache
    result = {}
    for row in rows[1:]:
        row = _pad_row(row, 2)
        if row[0]:
            result[row[0].lower().strip()] = row[1]
    _config_cache = result
    return _config_cache


async def upsert_config_mapping(description: str, category: str) -> None:
    """Save or update a description→category mapping in the Config sheet."""
    global _config_cache
    key = description.lower().strip()
    rows = await _get_values("Config!A:B")
    for i, row in enumerate(rows[1:], start=2):
        row = _pad_row(row, 2)
        if row[0].lower().strip() == key:
            await _set_values(f"Config!A{i}:B{i}", [[description, category]])
            if _config_cache is not None:
                _config_cache[key] = category
            return
    await _append_values("Config!A:B", [[description, category]])
    if _config_cache is not None:
        _config_cache[key] = category


async def set_budget(scope: str, limit_vnd: int, period: str = "month") -> None:
    rows = await _get_values("Budget!A:C")
    header_offset = 1  # row 1 is header
    for i, row in enumerate(rows[1:], start=2):
        row = _pad_row(row, 3)
        if row[0] == scope:
            await _set_values(f"Budget!A{i}:C{i}", [[scope, limit_vnd, period]])
            return
    await _append_values("Budget!A:C", [[scope, limit_vnd, period]])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pad_row(row: list, length: int) -> list:
    return row + [""] * (length - len(row))
