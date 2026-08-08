"""Analysis screen endpoint: expense-by-category breakdown + monthly expense/income, year-to-date."""
from fastapi import APIRouter, Depends, Query

from webapp.auth import CurrentUser, get_current_user
from webapp.schemas import AnalysisSummary
from webapp.service import get_analysis_summary
from sheets import now_vn

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


@router.get("", response_model=AnalysisSummary)
async def analysis(
    year: str = Query(default=None, description="YYYY, defaults to current year"),
    user: CurrentUser = Depends(get_current_user),
):
    return await get_analysis_summary(year or str(now_vn().year))
