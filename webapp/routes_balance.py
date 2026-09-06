"""Running cash balance endpoint: starting-of-month balance, editable for the current month, with history."""
from fastapi import APIRouter, Depends, Query

from webapp.auth import CurrentUser, get_current_user
from webapp.schemas import BalanceOut, BalanceUpdate
from webapp.service import get_balance_summary, set_current_month_balance

router = APIRouter(prefix="/api/balance", tags=["balance"])


@router.get("", response_model=BalanceOut)
async def balance(
    history: int = Query(default=12, ge=0, le=60, description="Number of past months to include in history"),
    user: CurrentUser = Depends(get_current_user),
):
    return await get_balance_summary(history)


@router.put("", response_model=BalanceOut)
async def update_balance(body: BalanceUpdate, user: CurrentUser = Depends(get_current_user)):
    return await set_current_month_balance(body.starting_balance)
