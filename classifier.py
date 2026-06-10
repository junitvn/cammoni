"""
2-tier classifier:
  Tier 1: rule-based keyword matching (free, instant)
  Tier 2: Gemini 2.0 Flash fallback for ambiguous descriptions
"""
import os
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

CATEGORIES_FILE = Path(__file__).parent / "config" / "categories.yaml"


def _load_categories() -> dict:
    with open(CATEGORIES_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["categories"]


_CATEGORIES = _load_categories()

# Category key → display info
CATEGORY_INFO = {
    key: {"name": v["name"], "emoji": v["emoji"]}
    for key, v in _CATEGORIES.items()
}
CATEGORY_KEYS = list(_CATEGORIES.keys())
CATEGORY_NAMES = [v["name"] for v in _CATEGORIES.values()]

INCOME_CATEGORY_KEYS = [k for k, v in _CATEGORIES.items() if v.get("income")]
EXPENSE_CATEGORY_KEYS = [k for k, v in _CATEGORIES.items() if not v.get("income")]


def classify_rule(description: str) -> Optional[str]:
    """
    Tier-1: return category key if a keyword matches, else None.
    Only checks expense categories.
    """
    desc_lower = description.lower()
    for key in EXPENSE_CATEGORY_KEYS:
        if key == "khac":
            continue
        for kw in _CATEGORIES[key].get("keywords", []):
            if kw.lower() in desc_lower:
                return key
    return None


def classify_income_rule(description: str) -> str:
    """Return income category key based on keywords. Defaults to thu_khac."""
    desc_lower = description.lower()
    for key in INCOME_CATEGORY_KEYS:
        for kw in _CATEGORIES[key].get("keywords", []):
            if kw.lower() in desc_lower:
                return key
    return INCOME_CATEGORY_KEYS[-1] if INCOME_CATEGORY_KEYS else "thu_khac"


# Simple in-memory cache for Gemini results keyed by normalized description
_gemini_cache: dict[str, str] = {}


async def classify_gemini(description: str, amount: int) -> str:
    """
    Tier-2: call Gemini 2.0 Flash to classify.
    Falls back to 'khac' on any error.
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return "khac"

    use_ai = os.getenv("USE_AI_FALLBACK", "true").lower()
    if use_ai != "true":
        return "khac"

    # Normalize for cache
    cache_key = description.strip().lower()
    if cache_key in _gemini_cache:
        return _gemini_cache[cache_key]

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")

        category_list = ", ".join(
            f'"{v["name"]}"' for v in _CATEGORIES.values()
        )
        prompt = (
            f"Phân loại khoản chi/thu sau vào đúng 1 trong các mục: {category_list}.\n"
            f"Số tiền: {amount:,} VNĐ. Mô tả: '{description}'.\n"
            f"Chỉ trả về tên mục, không giải thích thêm."
        )

        response = model.generate_content(prompt)
        name_returned = response.text.strip().strip('"').strip("'")

        # Map returned name back to key
        for key, data in _CATEGORIES.items():
            if data["name"].lower() == name_returned.lower():
                _gemini_cache[cache_key] = key
                return key

        _gemini_cache[cache_key] = "khac"
        return "khac"

    except Exception as e:
        logger.warning(f"Gemini classification failed: {e}")
        return "khac"


async def classify(description: str, amount: int, tx_type: str = "chi") -> tuple[str, bool]:
    """
    Returns (category_key, ai_used).
    For income: Config → income rule.
    For expense: Config → rule-based → Gemini.
    """
    from sheets import get_config_mappings
    config = await get_config_mappings()
    config_key = config.get(description.lower().strip())

    if tx_type == "thu":
        if config_key and config_key in INCOME_CATEGORY_KEYS:
            return config_key, False
        return classify_income_rule(description), False

    if config_key and config_key in EXPENSE_CATEGORY_KEYS:
        return config_key, False

    key = classify_rule(description)
    if key:
        return key, False

    key = await classify_gemini(description, amount)
    return key, True


def category_display(key: str) -> str:
    info = CATEGORY_INFO.get(key, {"emoji": "📦", "name": "Chi tiêu khác"})
    return f"{info['emoji']} {info['name']}"


def key_from_name(name: str) -> str:
    """Return category key from Vietnamese name."""
    for key, data in _CATEGORIES.items():
        if data["name"].lower() == name.lower():
            return key
    return "khac"
