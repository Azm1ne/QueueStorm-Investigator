"""QueueStorm Investigator — FastAPI service.

Endpoints:
  GET  /health         -> {"status":"ok"}
  POST /analyze-ticket -> structured investigator analysis (see models.TicketResponse)

Request flow (LLM-first with deterministic guardrails):
  parse+validate input -> try Gemini (hard timeout) -> on any failure, rule-based fallback
  -> normalize (schema/enum/invariants) -> safety sanitise -> validate -> 200.
The service never crashes on bad input and never returns an unsafe or out-of-spec body.
"""

import asyncio
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

import fallback
import llm
from models import TicketRequest, TicketResponse
from normalize import normalize_response
from safety import sanitize_action, sanitize_reply

logger = logging.getLogger("queuestorm")
logging.basicConfig(level=logging.INFO)


def _load_dotenv():
    """Minimal .env loader so local dev works without an extra dependency. Never overrides
    variables already set in the real environment (the hosting platform wins)."""
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT_SECONDS", "12"))

app = FastAPI(title="QueueStorm Investigator", version="1.0")


_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(content=fh.read())


@app.get("/health")
async def health():
    return {"status": "ok"}


async def _run_analysis(ticket: TicketRequest) -> dict:
    """Return raw analysis from the LLM if available/healthy, else the rule-based engine."""
    if llm.is_enabled():
        try:
            return await asyncio.wait_for(
                llm.analyze_with_llm(ticket, LLM_TIMEOUT),
                timeout=LLM_TIMEOUT + 2.0,
            )
        except Exception as exc:  # timeout, network, bad output -> fall back
            logger.warning("LLM path failed (%s); using deterministic fallback", type(exc).__name__)
    return fallback.analyze(ticket)


def _finalize(raw: dict, ticket: TicketRequest) -> dict:
    """normalize -> safety sanitise -> validate. Guaranteed to return a valid response dict."""
    final = normalize_response(raw, ticket)
    final["customer_reply"] = sanitize_reply(final["customer_reply"], ticket)
    final["recommended_next_action"] = sanitize_action(final["recommended_next_action"])
    return TicketResponse(**final).model_dump()


@app.post("/analyze-ticket")
async def analyze_ticket(request: Request):
    # 1. Parse body (malformed JSON -> 400).
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Malformed JSON body."})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "Request body must be a JSON object."})

    # 2. Validate required fields (missing -> 400; empty complaint -> 422).
    try:
        ticket = TicketRequest(**body)
    except ValidationError:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid request: 'ticket_id' and 'complaint' are required string fields."},
        )
    if not ticket.complaint or not ticket.complaint.strip():
        return JSONResponse(status_code=422, content={"error": "The 'complaint' field must not be empty."})

    # 3. Analyse (LLM-first) and finalise through the deterministic guardrails.
    try:
        raw = await _run_analysis(ticket)
        return JSONResponse(status_code=200, content=_finalize(raw, ticket))
    except Exception:
        logger.exception("Primary analysis failed; attempting deterministic fallback")
        try:
            raw = fallback.analyze(ticket)
            return JSONResponse(status_code=200, content=_finalize(raw, ticket))
        except Exception:
            logger.exception("Fallback analysis failed")
            return JSONResponse(
                status_code=500,
                content={"error": "Internal error while analysing the ticket."},
            )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # Never leak stack traces, tokens, or secrets in the response body.
    logger.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"error": "Internal server error."})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
