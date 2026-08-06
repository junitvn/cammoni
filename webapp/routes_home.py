"""Home screen summary endpoint: today/month totals + day-grouped transaction list."""
from fastapi import APIRouter, Depends, Query

from webapp.auth import CurrentUser, get_current_user
from webapp.schemas import HomeSummary
from webapp.service import get_home_summary

router = APIRouter(prefix="/api/home", tags=["home"])


@router.get("", response_model=HomeSummary)
async def home(
    month: str = Query(..., description="YYYY-MM"),
    user: CurrentUser = Depends(get_current_user),
):
    return await get_home_summary(month)
