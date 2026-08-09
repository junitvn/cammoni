"""CRUD for transactions — a thin wrapper over sheets.py, reusing the bot's classify() logic."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from classifier import EXPENSE_CATEGORY_KEYS, INCOME_CATEGORY_KEYS, classify
from parser import normalize_vn, parse_amount_search
from sheets import (
    add_transaction, delete_transaction, format_ts, get_transaction_by_id,
    get_recent_transactions, load_categories_from_sheet, now_vn, update_transaction_field, upsert_config_mapping,
)

from webapp.auth import CurrentUser, get_current_user
from webapp.schemas import TransactionCreate, TransactionOut, TransactionUpdate
from webapp.service import get_category_transactions, parse_client_timestamp, row_to_out

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def _matches_search(row: dict, categories: dict, query: str) -> bool:
    normalized_query = normalize_vn(query)
    compact_query = _digits(query)

    if normalized_query in normalize_vn(row.get("description", "")):
        return True

    amount_digits = _digits(row.get("amount", ""))
    if compact_query and compact_query in amount_digits:
        return True

    amount_range = parse_amount_search(query)
    if amount_range:
        amount_min, amount_max, _ = amount_range
        try:
            amount = int(float(row.get("amount", 0)))
        except (ValueError, TypeError):
            amount = 0
        if (amount_min is None or amount >= amount_min) and (amount_max is None or amount <= amount_max):
            return True

    category_key = row.get("category", "")
    category = categories.get(category_key, {})
    category_text = " ".join([str(category_key), str(category.get("name", ""))])
    return normalized_query in normalize_vn(category_text)


@router.get("", response_model=list[TransactionOut])
async def list_transactions(
    category: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: CurrentUser = Depends(get_current_user),
):
    if not category or not start or not end:
        raise HTTPException(400, "category, start and end are required")
    rows = await get_category_transactions(category, start, end)
    return [row_to_out(row) for row in rows]


@router.get("/search", response_model=list[TransactionOut])
async def search_transactions(
    q: str = "",
    limit: int = 100,
    user: CurrentUser = Depends(get_current_user),
):
    query = q.strip()
    if not query:
        return []

    categories = await load_categories_from_sheet()
    result_limit = min(max(limit, 1), 200)
    rows = await get_recent_transactions(limit=10_000)
    return [row_to_out(row) for row in rows if _matches_search(row, categories, query)][:result_limit]


@router.get("/{tx_id}", response_model=TransactionOut)
async def get_transaction(tx_id: str, user: CurrentUser = Depends(get_current_user)):
    result = await get_transaction_by_id(tx_id)
    if not result:
        raise HTTPException(404, "Transaction not found")
    return row_to_out(result[1])


@router.post("", response_model=TransactionOut, status_code=201)
async def create_transaction(body: TransactionCreate, user: CurrentUser = Depends(get_current_user)):
    if body.type not in ("chi", "thu"):
        raise HTTPException(400, "type must be 'chi' or 'thu'")

    valid_keys = INCOME_CATEGORY_KEYS if body.type == "thu" else EXPENSE_CATEGORY_KEYS
    category = body.category
    auto_classified = False
    if category:
        if category not in valid_keys:
            raise HTTPException(400, "Unknown category for this transaction type")
        await upsert_config_mapping(body.description, category)
    else:
        category, auto_classified = await classify(body.description, body.amount, body.type)

    timestamp = now_vn()
    if body.timestamp:
        parsed = parse_client_timestamp(body.timestamp)
        if not parsed:
            raise HTTPException(400, "Invalid timestamp")
        timestamp = parsed

    tx_id = await add_transaction(
        user_id=str(user.id),
        tx_type=body.type,
        amount=body.amount,
        category=category,
        description=body.description,
        auto_classified=auto_classified,
        timestamp=timestamp,
        user_name=body.user_name or user.name,
    )
    result = await get_transaction_by_id(tx_id)
    return row_to_out(result[1])


@router.put("/{tx_id}", response_model=TransactionOut)
async def edit_transaction(tx_id: str, body: TransactionUpdate, user: CurrentUser = Depends(get_current_user)):
    result = await get_transaction_by_id(tx_id)
    if not result:
        raise HTTPException(404, "Transaction not found")
    _, row, _ = result

    if body.amount is not None:
        await update_transaction_field(tx_id, "amount", body.amount)
    if body.category is not None:
        valid_keys = INCOME_CATEGORY_KEYS if row.get("type") == "thu" else EXPENSE_CATEGORY_KEYS
        if body.category not in valid_keys:
            raise HTTPException(400, "Unknown category for this transaction type")
        await update_transaction_field(tx_id, "category", body.category)
        await upsert_config_mapping(row.get("description", ""), body.category)
    if body.description is not None:
        await update_transaction_field(tx_id, "description", body.description)
    if body.timestamp is not None:
        parsed = parse_client_timestamp(body.timestamp)
        if not parsed:
            raise HTTPException(400, "Invalid timestamp")
        await update_transaction_field(tx_id, "timestamp", format_ts(parsed))
    if body.excluded is not None:
        await update_transaction_field(tx_id, "excluded", "Y" if body.excluded else "")
    if body.user_name is not None:
        await update_transaction_field(tx_id, "user_name", body.user_name)

    result = await get_transaction_by_id(tx_id)
    return row_to_out(result[1])


@router.delete("/{tx_id}", status_code=204)
async def remove_transaction(tx_id: str, user: CurrentUser = Depends(get_current_user)):
    ok = await delete_transaction(tx_id)
    if not ok:
        raise HTTPException(404, "Transaction not found")
