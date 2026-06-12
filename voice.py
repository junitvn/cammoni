"""
Voice message transcription using Gemini.
transcribe_voice() returns either a "record" or "search" intent.
"""
import asyncio
import base64
import json
import logging
import re

from gemini_utils import generate_with_fallback

logger = logging.getLogger(__name__)


_PROMPT = """Đây là tin nhắn thoại tiếng Việt liên quan đến quản lý chi tiêu.

Hãy xác định ý định (intent):
- "record": người dùng muốn GHI CHÉP một hoặc nhiều khoản thu/chi.
  Ví dụ: "ăn cơm 50", "đổ xăng 200k", "mua sữa 30 hôm qua", "thu lương 5 triệu".
- "search": người dùng muốn TÌM KIẾM / XEM LẠI giao dịch đã lưu.
  Ví dụ: "tìm cơm", "kiếm xăng", "50k có gì", "tìm khoảng 200", "grab tuần này".
- "budget": người dùng muốn ĐẶT NGÂN SÁCH.
  Ví dụ: "đặt ngân sách ăn ngoài 3 triệu", "ngân sách tổng 15 triệu", "set budget xăng xe 500k".
- "category_filter": người dùng muốn XEM DANH SÁCH THEO DANH MỤC.
  Ví dụ: "xem ăn ngoài", "liệt kê y tế", "danh sách xăng xe tháng này".
- "stats": người dùng muốn XEM THỐNG KÊ tổng quan.
  Ví dụ: "hôm nay chi bao nhiêu", "thống kê tuần này", "tháng này tiêu gì", "tháng 5 chi tiêu", "từ ngày 1 đến 10".

Trả về JSON (KHÔNG có markdown):

Nếu intent = "record":
{
  "intent": "record",
  "transactions": [
    {
      "amount_k": <số tiền nghìn đồng>,
      "description": "<mô tả>",
      "type": "chi" | "thu",
      "date_day": <ngày 1-31 nếu đề cập, else null>,
      "date_month": <tháng 1-12 nếu đề cập cùng ngày, else null>,
      "date_offset": <0=hôm nay, -1=hôm qua, -2=hôm kia; mặc định 0>
    }
  ]
}

Nếu intent = "search":
{
  "intent": "search",
  "keyword": "<từ khoá tìm theo mô tả, hoặc null nếu không có>",
  "amount_search": "<chuỗi tìm theo tiền: '50'=50k-59k, '<200'=dưới 200k, '>50'=trên 50k, '<=200'=tối đa 200k, '>=50'=tối thiểu 50k, '50-200'=từ 50k đến 200k; hoặc null>"
}

Nếu intent = "budget":
{"intent": "budget", "scope": "<'chung' hoặc category key: an_ngoai|di_cho|bat_buoc|y_te|phuong_tien|dau_tu|khac>", "amount_k": <số tiền nghìn đồng>}

Nếu intent = "category_filter":
{"intent": "category_filter", "category": "<category key: an_ngoai|di_cho|bat_buoc|y_te|phuong_tien|dau_tu|khac|luong|thu_khac>"}

Nếu intent = "stats":
{"intent": "stats", "period": "today"|"week"|"month"|"range",
 "month": <số tháng nếu không phải tháng hiện tại, else null>,
 "year": <năm nếu khác năm hiện tại, else null>,
 "range_start": "<dd hoặc dd/mm hoặc dd/mm/yyyy nếu period=range, else null>",
 "range_end": "<dd hoặc dd/mm hoặc dd/mm/yyyy nếu period=range, else null>"}

Chỉ trả về JSON, không giải thích."""


async def transcribe_voice(audio_bytes: bytes) -> dict:
    """
    Transcribe voice and return intent dict:
      {"intent": "record", "transactions": [...]}
      {"intent": "search", "keyword": str|None, "amount_search": str|None}
    """
    part = {
        "inline_data": {
            "mime_type": "audio/ogg",
            "data": base64.b64encode(audio_bytes).decode(),
        }
    }

    response = await generate_with_fallback([part, _PROMPT])

    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    data = json.loads(raw)
    if not isinstance(data, dict) or "intent" not in data:
        return {"intent": "record", "transactions": []}

    intent = data.get("intent", "record")

    if intent == "search":
        return {
            "intent": "search",
            "keyword": data.get("keyword") or None,
            "amount_search": str(data["amount_search"]) if data.get("amount_search") else None,
        }

    if intent == "budget":
        try:
            amount_k = int(data.get("amount_k", 0))
        except (ValueError, TypeError):
            amount_k = 0
        return {"intent": "budget", "scope": str(data.get("scope", "chung")), "amount_k": amount_k}

    if intent == "category_filter":
        return {"intent": "category_filter", "category": str(data.get("category", ""))}

    if intent == "stats":
        return {
            "intent": "stats",
            "period": str(data.get("period", "month")),
            "month": data.get("month"),
            "year": data.get("year"),
            "range_start": data.get("range_start"),
            "range_end": data.get("range_end"),
        }

    # Parse record intent
    transactions = []
    for item in data.get("transactions", []):
        try:
            amount_k = int(item["amount_k"])
            description = str(item["description"]).strip()
            tx_type = "thu" if str(item.get("type", "chi")) == "thu" else "chi"
            if amount_k <= 0 or not description:
                continue
            entry: dict = {"amount_k": amount_k, "description": description, "type": tx_type}
            date_day = item.get("date_day")
            date_month = item.get("date_month")
            date_offset = item.get("date_offset", 0)
            if date_day is not None:
                entry["date_day"] = int(date_day)
                if date_month is not None:
                    entry["date_month"] = int(date_month)
            elif date_offset:
                entry["date_offset"] = int(date_offset)
            transactions.append(entry)
        except (KeyError, ValueError, TypeError):
            continue

    return {"intent": "record", "transactions": transactions}
