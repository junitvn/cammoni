"""Analysis screen endpoint: expense-by-category breakdown + monthly expense/income over a date range."""
from fastapi import APIRouter, Depends, Query

from webapp.auth import CurrentUser, get_current_user
from webapp.schemas import AnalysisSummary
from webapp.service import get_analysis_summary
from sheets import now_vn

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


@router.get("", response_model=AnalysisSummary)
async def analysis(
    start: str = Query(default=None, description="YYYY-MM-DD, defaults to start of current month"),
    end: str = Query(default=None, description="YYYY-MM-DD, defaults to today"),
    user: CurrentUser = Depends(get_current_user),
):
    now = now_vn()
    return await get_analysis_summary(start or now.strftime("%Y-%m-01"), end or now.strftime("%Y-%m-%d"))
