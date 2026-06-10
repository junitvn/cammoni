"""
Parse incoming Telegram messages into transaction records.
Rules:
  - Plain number + description → expense (chi tiêu)
  - Leading '.' or '+' → income (thu nhập)
  - All numbers are multiplied by 1,000 (unit = thousands VND)
  - Accept dot separators: 1.500 = 1500 → 1,500,000đ
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


def normalize_vn(text: str) -> str:
    """Normalize Vietnamese text: lowercase, remove diacritics (handles ă/â/ê/ô/ơ/ư/đ)."""
    text = text.lower().replace("đ", "d")
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _parse_num(s: str) -> Optional[int]:
    """Parse a number string (may have . or , as thousands sep) → int, or None."""
    clean = re.sub(r"[,.\s]", "", s)
    return int(clean) if clean.isdigit() and int(clean) > 0 else None


def _fmt_k(n: int) -> str:
    return f"{n:,}".replace(",", ".") + "k"


# (lo_vnd | None, hi_vnd | None, display_label)
AmountRange = tuple  # (Optional[int], Optional[int], str)


def parse_amount_search(s: str) -> Optional[AmountRange]:
    """
    Parse amount search into (lo_vnd, hi_vnd, label).
    lo/hi are None for open-ended ranges.

    Supported formats (amounts in thousands, e.g. 200 = 200k = 200.000đ):
      '<200'   → (None, 199_000, '< 200k')
      '>50'    → (51_000, None,  '> 50k')
      '<=200'  → (None, 200_000, '≤ 200k')
      '>=50'   → (50_000, None,  '≥ 50k')
      '50-200' → (50_000, 200_000, '50k-200k')
      '200'    → (200_000, 299_999, '200k-299k')  ← first-digit scale
    """
    s = s.strip()

    # <= or >=
    m = re.match(r'^([<>]=)\s*([\d,.\s]+)$', s)
    if m:
        op, n = m.group(1), _parse_num(m.group(2))
        if n:
            if op == '<=':
                return None, n * 1000, f"≤ {_fmt_k(n)}"
            else:
                return n * 1000, None, f"≥ {_fmt_k(n)}"

    # < or >
    m = re.match(r'^([<>])\s*([\d,.\s]+)$', s)
    if m:
        op, n = m.group(1), _parse_num(m.group(2))
        if n:
            if op == '<':
                return None, n * 1000 - 1, f"< {_fmt_k(n)}"
            else:
                return n * 1000 + 1, None, f"> {_fmt_k(n)}"

    # x-y range  (avoid matching negative numbers)
    m = re.match(r'^([\d,.\s]+)\s*[-–]\s*([\d,.\s]+)$', s)
    if m:
        lo_n, hi_n = _parse_num(m.group(1)), _parse_num(m.group(2))
        if lo_n and hi_n:
            if lo_n > hi_n:
                lo_n, hi_n = hi_n, lo_n
            return lo_n * 1000, hi_n * 1000, f"{_fmt_k(lo_n)}-{_fmt_k(hi_n)}"

    # First-digit scale pattern: '200' → 200k-299k, '244' → 200k-299k
    clean = re.sub(r"[,.\s]", "", s)
    if clean.isdigit() and int(clean) > 0:
        n = len(clean)
        first = int(clean[0])
        scale = 10 ** (n - 1)
        lo = first * scale * 1000
        hi = (first + 1) * scale * 1000 - 1
        lo_k, hi_k = lo // 1000, hi // 1000
        label = _fmt_k(lo_k) if lo_k == hi_k else f"{_fmt_k(lo_k)}-{_fmt_k(hi_k)}"
        return lo, hi, label

    return None


def format_amount_range(lo: Optional[int], hi: Optional[int]) -> str:
    """Format (lo_vnd, hi_vnd) → compact label. Handles None for open bounds."""
    if lo is None and hi is not None:
        return f"< {_fmt_k((hi + 1) // 1000)}"
    if lo is not None and hi is None:
        return f"> {_fmt_k((lo - 1) // 1000)}"
    if lo is not None and hi is not None:
        lo_k, hi_k = lo // 1000, hi // 1000
        return f"{_fmt_k(lo_k)}-{_fmt_k(hi_k)}" if lo_k != hi_k else _fmt_k(lo_k)
    return ""


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
