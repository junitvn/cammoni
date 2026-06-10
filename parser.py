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
    date_day: Optional[int] = None    # day parsed from message prefix (1-31)
    date_month: Optional[int] = None  # month parsed from message prefix (1-12)


# Matches: optional prefix (. or +), then number (with optional dot separators), then description
_PATTERN = re.compile(
    r'^([.+])?'                  # optional prefix
    r'([\d]+(?:[.,][\d]+)*)'     # number, possibly with . or , separators
    r'\s+'                       # whitespace separator
    r'(.+)$',                    # description
    re.UNICODE
)

# Date prefix: "15. " → day only; "15/6 " or "15-6 " → day and month
_DATE_FULL = re.compile(r'^(\d{1,2})[/\-](\d{1,2})\s+(.+)$', re.DOTALL)
_DATE_ONLY = re.compile(r'^(\d{1,2})\.\s+(.+)$', re.DOTALL)

# Matches a number token (with optional income prefix)
_NUM_TOKEN = re.compile(r'^([.+])?(\d+(?:[.,]\d+)*)$')


def _extract_date_prefix(text: str) -> tuple[Optional[int], Optional[int], str]:
    """Extract leading date prefix from message. Returns (day, month, remaining_text)."""
    m = _DATE_FULL.match(text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return d, mo, m.group(3).strip()
    m = _DATE_ONLY.match(text)
    if m:
        d = int(m.group(1))
        if 1 <= d <= 31:
            return d, None, m.group(2).strip()
    return None, None, text


def parse_message(text: str) -> Optional[ParseResult]:
    """Parse a single transaction. Returns ParseResult or None."""
    text = text.strip()

    day, month, body = _extract_date_prefix(text)

    m = _PATTERN.match(body)
    if m:
        prefix, num_str, desc = m.group(1), m.group(2), m.group(3).strip()
        num_clean = re.sub(r'[.,]', '', num_str)
        try:
            number = int(num_clean)
        except ValueError:
            return None
        if number <= 0:
            return None
        return ParseResult(
            amount=number * 1000,
            description=desc,
            tx_type="thu" if prefix in (".", "+") else "chi",
            raw=text,
            date_day=day,
            date_month=month,
        )

    # With date prefix, also try desc-first format ("15. cơm 50")
    if day is not None:
        result = _parse_item(body)
        if result:
            result.raw = text
            result.date_day = day
            result.date_month = month
            return result

    return None


def _parse_item(text: str) -> Optional[ParseResult]:
    """Parse one item in either 'num desc' or 'desc num' format."""
    text = text.strip()
    if not text:
        return None
    r = parse_message(text)
    if r:
        return r
    # Try desc-first: "cơm 40" or "gửi xe 12"
    m = re.match(r'^([.+])?(.+?)\s+([.+])?(\d+(?:[.,]\d+)*)$', text)
    if m:
        prefix = m.group(1) or m.group(3)
        desc = m.group(2).strip()
        num_clean = re.sub(r'[.,]', '', m.group(4))
        try:
            number = int(num_clean)
            if number > 0 and desc:
                return ParseResult(
                    amount=number * 1000,
                    description=desc,
                    tx_type="thu" if prefix in (".", "+") else "chi",
                    raw=text,
                )
        except ValueError:
            pass
    return None


def _make_result(num_token: str, desc: str, raw: str) -> Optional[ParseResult]:
    m = _NUM_TOKEN.match(num_token)
    if not m:
        return None
    prefix, num_str = m.group(1), m.group(2)
    num_clean = re.sub(r'[.,]', '', num_str)
    try:
        number = int(num_clean)
    except ValueError:
        return None
    if number <= 0 or not desc.strip():
        return None
    return ParseResult(
        amount=number * 1000,
        description=desc.strip(),
        tx_type="thu" if prefix in (".", "+") else "chi",
        raw=raw,
    )


def _parse_space_separated(text: str) -> list[ParseResult]:
    """Parse 'cơm 40 cháo 50 gửi xe 12' or '40 cơm 50 cháo 12 gửi xe'."""
    tokens = text.split()
    if len(tokens) < 4:
        return []

    num_pos = [i for i, t in enumerate(tokens) if _NUM_TOKEN.match(t)]
    if len(num_pos) < 2:
        return []

    results = []

    if num_pos[0] == 0:
        # Number-first: 40 cơm 50 cháo 12 gửi xe
        for j, ni in enumerate(num_pos):
            next_ni = num_pos[j + 1] if j + 1 < len(num_pos) else len(tokens)
            desc = " ".join(tokens[ni + 1:next_ni])
            r = _make_result(tokens[ni], desc, text)
            if r:
                results.append(r)
    else:
        # Desc-first: cơm 40 cháo 50 gửi xe 12
        prev_ni = -1
        for ni in num_pos:
            desc = " ".join(tokens[prev_ni + 1:ni])
            r = _make_result(tokens[ni], desc, text)
            if r:
                results.append(r)
            prev_ni = ni

    return results if len(results) >= 2 else []


def parse_batch_message(text: str) -> list[ParseResult]:
    """
    Parse multiple transactions from one message.
    Returns list of ParseResult (empty if fewer than 2 items found).
    Supports: '40 cơm, 50 cháo', 'cơm 40, cháo 50', 'cơm 40 cháo 50 gửi xe 12'
    """
    text = text.strip()
    if not text:
        return []

    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if len(parts) >= 2:
            results = [r for p in parts if (r := _parse_item(p))]
            if len(results) >= 2:
                return results

    return _parse_space_separated(text)


def format_amount(amount: int) -> str:
    """Format VND amount with thousands separator."""
    return f"{amount:,.0f}đ".replace(",", ".")
