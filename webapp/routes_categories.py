"""CRUD for the shared Categories sheet."""
import re
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Query

import classifier
from sheets import (
    add_category, delete_category, get_recent_transactions,
    load_categories_from_sheet, update_category, update_transaction_field,
)

from webapp.auth import CurrentUser, get_current_user
from webapp.schemas import CategoryCreate, CategoryOut, CategoryUpdate

router = APIRouter(prefix="/api/categories", tags=["categories"])


def _slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFD", name)
    ascii_only = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_only.lower()).strip("_")
    return slug or "category"


def _to_out(key: str, v: dict) -> dict:
    return {"key": key, "name": v["name"], "emoji": v["emoji"], "income": v["income"], "keywords": v["keywords"]}


@router.get("", response_model=list[CategoryOut])
async def list_categories(user: CurrentUser = Depends(get_current_user)):
    cats = await load_categories_from_sheet()
    return [_to_out(k, v) for k, v in cats.items()]


@router.post("", response_model=CategoryOut, status_code=201)
async def create_category(body: CategoryCreate, user: CurrentUser = Depends(get_current_user)):
    existing = await load_categories_from_sheet()
    base = _slugify(body.name)
    key = base
    i = 2
    while key in existing:
        key = f"{base}_{i}"
        i += 1

    await add_category(key, body.name, body.emoji, body.income, body.keywords)
    classifier.reload_categories(await load_categories_from_sheet())
    return {"key": key, "name": body.name, "emoji": body.emoji, "income": body.income, "keywords": body.keywords}


@router.put("/{key}", response_model=CategoryOut)
async def edit_category(key: str, body: CategoryUpdate, user: CurrentUser = Depends(get_current_user)):
    ok = await update_category(key, name=body.name, emoji=body.emoji, keywords=body.keywords)
    if not ok:
        raise HTTPException(404, "Category not found")
    cats = await load_categories_from_sheet()
    classifier.reload_categories(cats)
    return _to_out(key, cats[key])


@router.delete("/{key}", status_code=204)
async def remove_category(
    key: str,
    reassign: bool = Query(False),
    user: CurrentUser = Depends(get_current_user),
):
    cats = await load_categories_from_sheet()
    info = cats.get(key)
    if not info:
        raise HTTPException(404, "Category not found")

    in_use = await get_recent_transactions(category=key, limit=10_000)
    if in_use and not reassign:
        raise HTTPException(409, detail={"count": len(in_use)})

    if in_use:
        fallback = "thu_khac" if info["income"] else "khac"
        if fallback == key or fallback not in cats:
            raise HTTPException(400, "No fallback category available to reassign to")
        for row in in_use:
            await update_transaction_field(row["id"], "category", fallback)

    await delete_category(key)
    classifier.reload_categories(await load_categories_from_sheet())
