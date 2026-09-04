"""
Standalone FastAPI service exposing monthly budget (spending limit) data —
total and per category — for the cammoni-app Mini App frontend.

Reference implementation only: the Mini App's other endpoints
(/api/categories, /api/transactions, /api/home, /api/analysis) are served by
a separate "moni-app API" process that isn't part of this repo. This app
serves /api/budgets only — merge its routes into that service (or point the
frontend's VITE_API_BASE_URL at this one) when ready to go live.

Run: uvicorn api:app --reload --port 8001
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import classifier
from classifier import CATEGORY_INFO, EXPENSE_CATEGORY_KEYS
from sheets import get_budgets, set_budget, get_transactions_range, load_categories_from_sheet, now_vn

logger = logging.getLogger(__name__)

TOTAL_SCOPE = "chung"
TOTAL_LABEL = "Tổng chi"
TOTAL_EMOJI = "💰"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        cats = await load_categories_from_sheet()
        if cats:
            classifier.reload_categories(cats)
    except Exception as e:
        logger.warning(f"load_categories failed (non-fatal): {e}")
    yield


app = FastAPI(title="moni-app budgets API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class BudgetOut(BaseModel):
    scope: str
    label: str
    emoji: str
    limit_vnd: int
    period: str
    used: int
    pct: float


class BudgetUpdate(BaseModel):
    limit_vnd: int = Field(ge=0)


async def _month_usage() -> tuple[int, dict[str, int]]:
    """Total + per-category expense sums for the current calendar month (excludes rows marked excluded)."""
    now = now_vn()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = await get_transactions_range(start, now)

    total = 0
    by_cat: dict[str, int] = {k: 0 for k in EXPENSE_CATEGORY_KEYS}
    for row in rows:
        if str(row.get("excluded", "")).strip().upper() == "Y":
            continue
        if str(row.get("type", "chi")) != "chi":
            continue
        try:
            amt = int(row.get("amount", 0))
        except (ValueError, TypeError):
            amt = 0
        cat = str(row.get("category", "khac"))
        if cat not in by_cat:
            cat = "khac"
            by_cat.setdefault(cat, 0)
        by_cat[cat] += amt
        total += amt
    return total, by_cat


def _pct(used: int, limit: int) -> float:
    return (used / limit * 100) if limit > 0 else 0.0


@app.get("/api/budgets", response_model=list[BudgetOut])
async def list_budgets() -> list[BudgetOut]:
    budget_rows = await get_budgets()
    limits: dict[str, int] = {}
    period = "month"
    for b in budget_rows:
        scope = str(b.get("scope", ""))
        try:
            limits[scope] = int(b.get("limit_vnd", 0))
        except (ValueError, TypeError):
            limits[scope] = 0
        period = b.get("period") or period

    total_used, by_cat = await _month_usage()

    out = [
        BudgetOut(
            scope=TOTAL_SCOPE,
            label=TOTAL_LABEL,
            emoji=TOTAL_EMOJI,
            limit_vnd=limits.get(TOTAL_SCOPE, 0),
            period=period,
            used=total_used,
            pct=_pct(total_used, limits.get(TOTAL_SCOPE, 0)),
        )
    ]
    for key in EXPENSE_CATEGORY_KEYS:
        info = CATEGORY_INFO.get(key, {"name": key, "emoji": "📦"})
        limit = limits.get(key, 0)
        used = by_cat.get(key, 0)
        out.append(
            BudgetOut(
                scope=key,
                label=info["name"],
                emoji=info["emoji"],
                limit_vnd=limit,
                period=period,
                used=used,
                pct=_pct(used, limit),
            )
        )
    return out


@app.put("/api/budgets/{scope}", response_model=BudgetOut)
async def update_budget(scope: str, body: BudgetUpdate) -> BudgetOut:
    if scope != TOTAL_SCOPE and scope not in EXPENSE_CATEGORY_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown budget scope: {scope}")

    await set_budget(scope, body.limit_vnd)

    total_used, by_cat = await _month_usage()
    if scope == TOTAL_SCOPE:
        used, label, emoji = total_used, TOTAL_LABEL, TOTAL_EMOJI
    else:
        info = CATEGORY_INFO.get(scope, {"name": scope, "emoji": "📦"})
        used, label, emoji = by_cat.get(scope, 0), info["name"], info["emoji"]

    return BudgetOut(
        scope=scope,
        label=label,
        emoji=emoji,
        limit_vnd=body.limit_vnd,
        period="month",
        used=used,
        pct=_pct(used, body.limit_vnd),
    )
