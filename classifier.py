"""
2-tier classifier:
  Tier 0: Config sheet learned mappings (highest priority)
  Tier 1: rule-based keyword matching (free, instant)
  Tier 2: Gemini 2.0 Flash fallback for ambiguous descriptions

Categories are loaded from Google Sheets at startup via reload_categories().
"""
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level mutable globals — populated by reload_categories() at startup.
# All modules that import these names hold references to the same objects,
# so in-place mutations here are visible everywhere.
_CATEGORIES: dict = {}
CATEGORY_INFO: dict = {}
CATEGORY_KEYS: list = []
CATEGORY_NAMES: list = []
INCOME_CATEGORY_KEYS: list = []
EXPENSE_CATEGORY_KEYS: list = []


def reload_categories(data: dict) -> None:
    """Populate module globals from a categories dict (loaded from Sheets)."""
    _CATEGORIES.clear()
    _CATEGORIES.update(data)
    CATEGORY_INFO.clear()
    CATEGORY_INFO.update({k: {"name": v["name"], "emoji": v["emoji"]} for k, v in data.items()})
    CATEGORY_KEYS.clear()
    CATEGORY_KEYS.extend(data.keys())
    CATEGORY_NAMES.clear()
    CATEGORY_NAMES.extend(v["name"] for v in data.values())
    INCOME_CATEGORY_KEYS.clear()
    INCOME_CATEGORY_KEYS.extend(k for k, v in data.items() if v.get("income"))
    EXPENSE_CATEGORY_KEYS.clear()
    EXPENSE_CATEGORY_KEYS.extend(k for k, v in data.items() if not v.get("income"))
    logger.info(f"[classifier] reloaded {len(data)} categories")


def classify_rule(description: str) -> Optional[str]:
    """Tier-1: keyword match against expense categories. Returns key or None."""
    desc_lower = description.lower()
    for key in EXPENSE_CATEGORY_KEYS:
        if key == "khac":
            continue
        for kw in _CATEGORIES.get(key, {}).get("keywords", []):
            if kw.lower() in desc_lower:
                return key
    return None


def classify_income_rule(description: str) -> str:
    """Return income category key by keyword. Defaults to last income category."""
    desc_lower = description.lower()
    for key in INCOME_CATEGORY_KEYS:
        for kw in _CATEGORIES.get(key, {}).get("keywords", []):
            if kw.lower() in desc_lower:
                return key
    return INCOME_CATEGORY_KEYS[-1] if INCOME_CATEGORY_KEYS else "thu_khac"


# Simple in-memory cache for Gemini results keyed by normalized description
_gemini_cache: dict[str, str] = {}


async def classify_gemini(description: str, amount: int) -> str:
    """Tier-2: Gemini fallback with model chain. Returns 'khac' on any error."""
    if not os.getenv("GEMINI_API_KEY"):
        return "khac"
    if os.getenv("USE_AI_FALLBACK", "true").lower() != "true":
        return "khac"

    cache_key = description.strip().lower()
    if cache_key in _gemini_cache:
        return _gemini_cache[cache_key]

    try:
        from gemini_utils import generate_with_fallback

        category_list = ", ".join(f'"{v["name"]}"' for v in _CATEGORIES.values())
        prompt = (
            f"Phân loại khoản chi/thu sau vào đúng 1 trong các mục: {category_list}.\n"
            f"Số tiền: {amount:,} VNĐ. Mô tả: '{description}'.\n"
            f"Chỉ trả về tên mục, không giải thích thêm."
        )

        response = await generate_with_fallback(prompt)
        name_returned = response.text.strip().strip('"').strip("'")

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
    Order: Config sheet → income/expense rule → Gemini (expense only).
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
    for key, data in _CATEGORIES.items():
        if data["name"].lower() == name.lower():
            return key
    return "khac"
