"""
FastAPI backend for the moni-app Telegram Mini App.
Reuses moni-bot's existing sheets/classifier modules directly — the same Google
Sheet is the single source of truth for both the bot and the mini app.

Run from the moni-bot directory (so this package and its sibling modules resolve):
    cd moni-bot && python -m uvicorn webapp.main:app --reload --port 8000
"""
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import classifier
from sheets import init_sheets, load_categories_from_sheet

from webapp.auth import CurrentUser, get_current_user
from webapp.routes_analysis import router as analysis_router
from webapp.routes_budgets import router as budgets_router
from webapp.routes_categories import router as categories_router
from webapp.routes_home import router as home_router
from webapp.routes_transactions import router as transactions_router

app = FastAPI(title="moni-app API")

_default_origins = "http://localhost:5173,http://127.0.0.1:5173"
allowed_origins = [
    o.strip() for o in os.getenv("WEBAPP_CORS_ORIGINS", _default_origins).split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    await init_sheets()
    cats = await load_categories_from_sheet()
    if cats:
        classifier.reload_categories(cats)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/me")
async def me(user: CurrentUser = Depends(get_current_user)):
    return {"id": user.id, "name": user.name}


app.include_router(categories_router)
app.include_router(transactions_router)
app.include_router(home_router)
app.include_router(analysis_router)
app.include_router(budgets_router)
