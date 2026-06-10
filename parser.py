"""
Parse incoming Telegram messages into transaction records.
Rules:
  - Plain number + description → expense (chi tiêu)
  - Leading '.' or '+' → income (thu nhập)
  - All numbers are multiplied by 1,000 (unit = thousands VND)
  - Accept dot separators: 1.500 = 1500 → 1,500,000đ
"""
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ParseResult:
    amount: int          # amount in VND (already ×1000)
    description: str
    tx_type: str         # "chi" or "thu"
    raw: str


# Matches: optional prefix (. or +), then number (with optional dot separators), then description
_PATTERN = re.compile(
    r'^([.+])?'                  # optional prefix
    r'([\d]+(?:[.,][\d]+)*)'     # number, possibly with . or , separators
    r'\s+'                       # whitespace separator
    r'(.+)$',                    # description
    re.UNICODE
)


def parse_message(text: str) -> Optional[ParseResult]:
    """
    Parse a user message. Returns ParseResult or None if not a transaction.
    """
    text = text.strip()
    m = _PATTERN.match(text)
    if not m:
        return None

    prefix, num_str, desc = m.group(1), m.group(2), m.group(3).strip()

    # Normalize number: remove dots/commas used as thousands separators
    # e.g. "1.500" → "1500", "1,500" → "1500"
    num_clean = re.sub(r'[.,]', '', num_str)
    try:
        number = int(num_clean)
    except ValueError:
        return None

    if number <= 0:
        return None

    amount = number * 1000  # always ×1000
    tx_type = "thu" if prefix in (".", "+") else "chi"

    return ParseResult(
        amount=amount,
        description=desc,
        tx_type=tx_type,
        raw=text,
    )


def format_amount(amount: int) -> str:
    """Format VND amount with thousands separator."""
    return f"{amount:,.0f}đ".replace(",", ".")
