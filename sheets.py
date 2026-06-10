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

TRANSACTIONS_HEADER = [
    "id", "timestamp", "user", "type", "amount",
    "category", "description", "auto_classified"
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
     "điện, nước, internet, wifi, tiền nhà, thuê nhà, học phí, bảo hiểm, thuế, viện phí, bệnh viện, khám, thuốc, điện thoại, phone, sim, phí, hóa đơn, bill, trả góp, vay, nợ, đóng tiền, học, trường, gas"],
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
    """Ensure Transactions and Budget worksheets exist with correct headers."""
    logger.info("[sheets] init_sheets: checking worksheets...")
    sheet_id = _sheet_id()
    client = get_httpx_client()

    # Get spreadsheet metadata
    r = await client.get(
        f"{SHEETS_BASE}/{sheet_id}",
        headers=await _auth_headers(),
    )
    r.raise_for_status()
    meta = r.json()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    logger.info(f"[sheets] init_sheets: existing sheets={existing}")

    # Create missing sheets
    requests_body = []
    for name in ("Transactions", "Budget", "Config", "Categories", "Users"):
        if name not in existing:
            requests_body.append({
                "addSheet": {"properties": {"title": name, "gridProperties": {"rowCount": 1000, "columnCount": 20}}}
            })

    if requests_body:
        r = await client.post(
            f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
            headers=await _auth_headers(),
            json={"requests": requests_body},
        )
        r.raise_for_status()
        logger.info(f"[sheets] init_sheets: created missing sheets")

    # Ensure headers
    for name, header in [
        ("Transactions", TRANSACTIONS_HEADER),
        ("Budget", BUDGET_HEADER),
        ("Config", CONFIG_HEADER),
        ("Categories", CATEGORIES_HEADER),
        ("Users", USERS_HEADER),
    ]:
        values = await _get_values(f"{name}!1:1")
        if not values or values[0] != header:
            await _set_values(f"{name}!A1", [header])
            logger.info(f"[sheets] init_sheets: wrote header for '{name}'")

    # Seed Categories if empty
    cat_rows = await _get_values("Categories!A:E")
    if len(cat_rows) <= 1:
        await _append_values("Categories!A:E", _CATEGORIES_SEED)
        logger.info("[sheets] init_sheets: seeded Categories")

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
    ]
    logger.info(f"[sheets] add_transaction: writing tx_id={tx_id}")
    await _append_values("Transactions!A:H", [row])
    logger.info(f"[sheets] add_transaction: done tx_id={tx_id}")
    return tx_id


async def get_recent_transactions(
    user_id: Optional[str] = None,
    limit: int = 10,
    keyword: Optional[str] = None,
) -> list[dict]:
    rows = await _get_values("Transactions!A:H")
    if not rows or rows[0] != TRANSACTIONS_HEADER:
        return []

    results = []
    for i, row in enumerate(rows[1:], start=2):
        row = _pad_row(row, len(TRANSACTIONS_HEADER))
        d = dict(zip(TRANSACTIONS_HEADER, row))
        d["_row"] = i
        if user_id and d.get("user") != str(user_id):
            continue
        if keyword and keyword.lower() not in d.get("description", "").lower():
            continue
        results.append(d)

    return results[-limit:][::-1]


async def get_transaction_by_id(tx_id: str) -> Optional[tuple[int, dict]]:
    rows = await _get_values("Transactions!A:H")
    if not rows:
        return None
    for i, row in enumerate(rows[1:], start=2):
        row = _pad_row(row, len(TRANSACTIONS_HEADER))
        if row[COL_ID] == tx_id:
            return i, dict(zip(TRANSACTIONS_HEADER, row))
    return None


async def update_transaction_field(tx_id: str, field: str, value) -> bool:
    col_map = {
        "timestamp": COL_TIMESTAMP,
        "type": COL_TYPE,
        "amount": COL_AMOUNT,
        "category": COL_CATEGORY,
        "description": COL_DESCRIPTION,
        "auto_classified": COL_AUTO,
    }
    col = col_map.get(field)
    if col is None:
        return False

    result = await get_transaction_by_id(tx_id)
    if not result:
        return False
    row_idx, _ = result

    col_letter = chr(ord('A') + col)
    await _set_values(f"Transactions!{col_letter}{row_idx}", [[value]])
    return True


async def delete_transaction(tx_id: str) -> bool:
    result = await get_transaction_by_id(tx_id)
    if not result:
        return False
    row_idx, _ = result
    gid = await _get_sheet_gid("Transactions")
    await _delete_row(gid, row_idx - 1)  # convert to 0-based
    return True


async def get_transactions_range(
    start: datetime,
    end: datetime,
    user_id: Optional[str] = None,
) -> list[dict]:
    rows = await _get_values("Transactions!A:H")
    if not rows or rows[0] != TRANSACTIONS_HEADER:
        return []

    results = []
    for row in rows[1:]:
        row = _pad_row(row, len(TRANSACTIONS_HEADER))
        d = dict(zip(TRANSACTIONS_HEADER, row))
        ts = parse_ts(d.get("timestamp", ""))
        if not ts:
            continue
        if ts < start or ts > end:
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
