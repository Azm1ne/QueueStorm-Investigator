"""Single source of truth for every enum in the QueueStorm Investigator contract.

Values are copied verbatim from SUST_Preli_Sample_Cases.json -> allowed_enums and the
Problem Statement Sections 6-7. The Problem Statement is explicit: "All enum values must
match exactly. Variants (case differences, plural forms, alternate spellings) will be
scored as schema violations." So these tuples are the only place values are defined; the
Pydantic models, prompt, and fallback all import from here.
"""

from typing import Literal, get_args

# --- Input enums (request) ---
Language = Literal["en", "bn", "mixed"]
Channel = Literal["in_app_chat", "call_center", "email", "merchant_portal", "field_agent"]
UserType = Literal["customer", "merchant", "agent", "unknown"]
TransactionType = Literal[
    "transfer", "payment", "cash_in", "cash_out", "settlement", "refund"
]
TransactionStatus = Literal["completed", "failed", "pending", "reversed"]

# --- Output enums (response) ---
EvidenceVerdict = Literal["consistent", "inconsistent", "insufficient_data"]
CaseType = Literal[
    "wrong_transfer",
    "payment_failed",
    "refund_request",
    "duplicate_payment",
    "merchant_settlement_delay",
    "agent_cash_in_issue",
    "phishing_or_social_engineering",
    "other",
]
Severity = Literal["low", "medium", "high", "critical"]
Department = Literal[
    "customer_support",
    "dispute_resolution",
    "payments_ops",
    "merchant_operations",
    "agent_operations",
    "fraud_risk",
]

# Runtime sets for validation/coercion in the LLM and fallback layers.
EVIDENCE_VERDICTS = set(get_args(EvidenceVerdict))
CASE_TYPES = set(get_args(CaseType))
SEVERITIES = set(get_args(Severity))
DEPARTMENTS = set(get_args(Department))

# Severity ordering for "floor" enforcement (e.g. phishing must be at least critical).
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Canonical department for each case_type, used by the fallback engine and as a
# deterministic post-LLM invariant. Derived from Problem Statement Section 7.2.
CASE_TYPE_TO_DEPARTMENT = {
    "wrong_transfer": "dispute_resolution",
    "payment_failed": "payments_ops",
    "refund_request": "customer_support",
    "duplicate_payment": "payments_ops",
    "merchant_settlement_delay": "merchant_operations",
    "agent_cash_in_issue": "agent_operations",
    "phishing_or_social_engineering": "fraud_risk",
    "other": "customer_support",
}
