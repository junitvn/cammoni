"""
Gemini API helper with automatic model fallback on quota (429) errors.
Priority: gemini-3.1-flash-lite → gemini-3.5-flash → gemini-2.5-flash-lite
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

MODEL_CHAIN = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-2.5-flash-lite",
]

_configured = False


def _ensure_configured() -> None:
    global _configured
    if not _configured:
        import google.generativeai as genai
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set")
        genai.configure(api_key=api_key)
        _configured = True


def _is_quota_error(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "quota" in msg.lower() or "rate" in msg.lower()


async def generate_with_fallback(contents) -> object:
    """
    Call generate_content(contents) trying each model in MODEL_CHAIN.
    Falls back to the next model on quota/rate-limit errors (429).
    Raises the last error if all models are exhausted.
    """
    _ensure_configured()
    import google.generativeai as genai

    last_err: Exception | None = None
    for model_name in MODEL_CHAIN:
        try:
            model = genai.GenerativeModel(model_name)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda m=model: m.generate_content(contents),
            )
            if model_name != MODEL_CHAIN[0]:
                logger.info(f"[gemini] used fallback model: {model_name}")
            return response
        except Exception as e:
            if _is_quota_error(e):
                last_err = e
                logger.warning(f"[gemini] {model_name} quota exceeded, trying next model")
                continue
            raise

    raise last_err
