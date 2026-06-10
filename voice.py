"""
Voice message transcription using Gemini.
Extracts transactions directly from audio.
"""
import asyncio
import base64
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        import google.generativeai as genai
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set")
        genai.configure(api_key=api_key)
        _model = genai.GenerativeModel("gemini-2.5-flash-lite")
    return _model


async def transcribe_to_transactions(audio_bytes: bytes) -> list[dict]:
    """
    Send OGG voice bytes to Gemini, return extracted transactions.
    Each item: {"amount_k": int, "description": str, "type": "chi"|"thu"}
    """
    model = _get_model()

    prompt = (
        "Đây là tin nhắn thoại tiếng Việt dùng để ghi chép thu chi hàng ngày. "
        "Hãy nhận dạng giọng nói và trích xuất tất cả các khoản thu/chi được đề cập. "
        "Trả về JSON array, mỗi phần tử gồm:\n"
        '  {"amount_k": <số tiền đơn vị nghìn đồng, kiểu số>, '
        '"description": "<mô tả ngắn gọn>", '
        '"type": "chi" hoặc "thu", '
        '"date_day": <ngày trong tháng 1-31 nếu được đề cập, không thì null>, '
        '"date_month": <tháng 1-12 nếu được đề cập cùng ngày, không thì null>, '
        '"date_offset": <số ngày so với hôm nay: 0=hôm nay, -1=hôm qua, -2=hôm kia; '
        'mặc định 0, chỉ dùng khi không có date_day>}\n'
        "Ví dụ ngày: 'hôm qua' → date_offset=-1; 'ngày 15' → date_day=15; "
        "'15 tháng 6' → date_day=15, date_month=6; 'hôm kia' → date_offset=-2.\n"
        "Lưu ý: nếu không có prefix + hoặc . thì mặc định là chi (expense). "
        "Chỉ trả về JSON array, không giải thích thêm. "
        "Nếu không nghe rõ hoặc không có khoản nào, trả về []."
    )

    part = {
        "inline_data": {
            "mime_type": "audio/ogg",
            "data": base64.b64encode(audio_bytes).decode(),
        }
    }

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: model.generate_content([part, prompt]),
    )

    raw = response.text.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    data = json.loads(raw)
    if not isinstance(data, list):
        return []

    results = []
    for item in data:
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
            results.append(entry)
        except (KeyError, ValueError, TypeError):
            continue

    return results
