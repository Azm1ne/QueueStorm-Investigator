"""Deterministic schema guardrail.

`normalize_response` takes a possibly-messy dict (typically raw LLM output) plus the
original request, and returns a dict that is guaranteed to satisfy the response contract:
valid enums, a real-or-null relevant_transaction_id, sane defaults for any missing field,
and a few hard invariants (phishing => critical + fraud_risk; consistent verdict requires a
matched transaction). This is the layer that protects the 15 schema points and prevents the
LLM from ever emitting an out-of-spec value.
"""

from typing import Optional

from enums import (
    CASE_TYPE_TO_DEPARTMENT,
    CASE_TYPES,
    DEPARTMENTS,
    EVIDENCE_VERDICTS,
    SEVERITIES,
)
from models import TicketRequest

# Sensible defaults derived from the 10 sample cases. Used only to fill a field the LLM
# omitted, or by the fallback engine — never to override a valid value the LLM supplied
# (except the safety invariants at the bottom of normalize_response).
DEFAULT_SEVERITY = {
    "wrong_transfer": "high",
    "payment_failed": "high",
    "refund_request": "low",
    "duplicate_payment": "high",
    "merchant_settlement_delay": "medium",
    "agent_cash_in_issue": "high",
    "phishing_or_social_engineering": "critical",
    "other": "low",
}

DEFAULT_HUMAN_REVIEW = {
    "wrong_transfer": True,
    "payment_failed": False,
    "refund_request": False,
    "duplicate_payment": True,
    "merchant_settlement_delay": False,
    "agent_cash_in_issue": True,
    "phishing_or_social_engineering": True,
    "other": False,
}

_SAFE_REPLY_FALLBACK = (
    "Thank you for reaching out. We have noted your concern and our team will review it and "
    "contact you through official support channels. Please do not share your PIN or OTP with "
    "anyone."
)


def _coerce_enum(value, allowed: set, default: str) -> str:
    """Lower/strip/normalise separators and snap to an allowed value, else default."""
    if not isinstance(value, str):
        return default
    v = value.strip().lower().replace(" ", "_").replace("-", "_")
    if v in allowed:
        return v
    return default


def _coerce_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return default


def _valid_txn_ids(request: TicketRequest) -> set:
    return {e.transaction_id for e in request.transaction_history if e.transaction_id}


def _coerce_text(value, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def normalize_response(raw, request: TicketRequest) -> dict:
    if not isinstance(raw, dict):
        raw = {}

    case_type = _coerce_enum(raw.get("case_type"), CASE_TYPES, "other")

    severity = _coerce_enum(
        raw.get("severity"), SEVERITIES, DEFAULT_SEVERITY.get(case_type, "low")
    )

    verdict = _coerce_enum(
        raw.get("evidence_verdict"), EVIDENCE_VERDICTS, "insufficient_data"
    )

    # relevant_transaction_id must be a real id from the provided history, or null.
    rid: Optional[str] = raw.get("relevant_transaction_id")
    if rid is not None:
        rid = str(rid)
        if rid not in _valid_txn_ids(request):
            rid = None

    # Department: trust a valid LLM value (it may correctly route a contested refund to
    # dispute_resolution), but snap an invalid one to the canonical department.
    department = _coerce_enum(
        raw.get("department"),
        DEPARTMENTS,
        CASE_TYPE_TO_DEPARTMENT.get(case_type, "customer_support"),
    )

    human_review = _coerce_bool(
        raw.get("human_review_required"), DEFAULT_HUMAN_REVIEW.get(case_type, False)
    )

    response = {
        "ticket_id": request.ticket_id,  # always echo from the request, never the LLM
        "relevant_transaction_id": rid,
        "evidence_verdict": verdict,
        "case_type": case_type,
        "severity": severity,
        "department": department,
        "agent_summary": _coerce_text(
            raw.get("agent_summary"), "Customer support case requires review."
        ),
        "recommended_next_action": _coerce_text(
            raw.get("recommended_next_action"),
            "Review the ticket details and follow up with the customer through official channels.",
        ),
        "customer_reply": _coerce_text(raw.get("customer_reply"), _SAFE_REPLY_FALLBACK),
        "human_review_required": human_review,
        "confidence": raw.get("confidence"),
        "reason_codes": raw.get("reason_codes"),
    }

    # reason_codes must be a list of strings or None.
    rc = response["reason_codes"]
    if isinstance(rc, list):
        response["reason_codes"] = [str(x) for x in rc if isinstance(x, (str, int))]
    else:
        response["reason_codes"] = None

    # --- Hard invariants (override the LLM) ---

    # A "consistent" verdict means a transaction was matched. If none was, the only honest
    # verdicts are insufficient_data (or inconsistent). Every sample with a null id uses
    # insufficient_data, so snap to that.
    if response["relevant_transaction_id"] is None and verdict == "consistent":
        response["evidence_verdict"] = "insufficient_data"

    # Phishing / social engineering: always critical, always fraud_risk, always escalate.
    if case_type == "phishing_or_social_engineering":
        response["severity"] = "critical"
        response["department"] = "fraud_risk"
        response["human_review_required"] = True

    return response
