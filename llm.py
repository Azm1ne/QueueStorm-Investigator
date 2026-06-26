"""Gemini reasoning layer.

Thin async wrapper around the Google Generative Language REST API using structured
function-calling output. A missing GEMINI_API_KEY never breaks startup or import — the app
simply runs on the deterministic fallback engine instead.
"""

import os

import httpx

_DEFAULT_MODEL = "gemini-flash-latest"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def is_enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


async def analyze_with_llm(request, timeout_s: float = 12.0) -> dict:
    """Call Gemini and return the raw analysis dict from its function call.

    Raises on any error (no key, network, timeout, unexpected response). The caller falls
    back to the deterministic engine on any exception."""
    from prompt import ANALYSIS_TOOL, SYSTEM_PROMPT, build_user_message

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("MODEL_NAME") or _DEFAULT_MODEL

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [
            {"role": "user", "parts": [{"text": build_user_message(request)}]}
        ],
        "tools": [{"functionDeclarations": [ANALYSIS_TOOL]}],
        "tool_config": {
            "function_calling_config": {
                "mode": "ANY",
                "allowed_function_names": ["submit_analysis"],
            }
        },
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 1024,
        },
    }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(
            _GEMINI_URL.format(model=model),
            headers={"X-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )

    resp.raise_for_status()
    data = resp.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {list(data.keys())}")

    parts = candidates[0].get("content", {}).get("parts") or []
    for part in parts:
        fc = part.get("functionCall", {})
        if fc.get("name") == "submit_analysis":
            return dict(fc.get("args", {}))

    raise RuntimeError("Gemini response contained no submit_analysis function call")
