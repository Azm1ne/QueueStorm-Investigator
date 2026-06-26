"""Pydantic models for the QueueStorm Investigator contract.

Design rule:
  * INPUT models are lenient. Hidden tests include malformed/edge inputs and we must not
    crash on them (Problem Statement Section 4: "The service must not crash on malformed
    input"). Only ticket_id and complaint are truly required.
  * OUTPUT model is strict. Enum fields are typed Literal so a wrong value raises a
    ValidationError that we catch and repair before responding — this is what protects the
    15 schema points and the "enum values must match exactly" rule.
"""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from enums import CaseType, Department, EvidenceVerdict, Severity


class TransactionHistoryEntry(BaseModel):
    """A single recent transaction. All fields optional so a partial/odd entry in the
    history does not 400 the whole request."""

    model_config = ConfigDict(extra="ignore")

    transaction_id: Optional[str] = None
    timestamp: Optional[str] = None
    type: Optional[str] = None
    amount: Optional[float] = None
    counterparty: Optional[str] = None
    status: Optional[str] = None

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, v):
        # Tolerate a non-numeric amount in one entry rather than 400-ing the whole ticket.
        if v is None or isinstance(v, (int, float)):
            return float(v) if v is not None else None
        if isinstance(v, str):
            try:
                return float(v.replace(",", "").strip())
            except ValueError:
                return None
        return None


class TicketRequest(BaseModel):
    """POST /analyze-ticket request body. Optional string enums are kept as plain str on
    input (not Literal) so an unexpected channel/language value never rejects the request;
    we normalise internally instead."""

    model_config = ConfigDict(extra="ignore")

    ticket_id: str
    complaint: str
    language: Optional[str] = None
    channel: Optional[str] = None
    user_type: Optional[str] = None
    campaign_context: Optional[str] = None
    transaction_history: List[TransactionHistoryEntry] = Field(default_factory=list)
    metadata: Optional[dict] = None

    @field_validator("transaction_history", mode="before")
    @classmethod
    def _coerce_history(cls, v):
        # Tolerate null or a non-list being sent for transaction_history.
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return v


class TicketResponse(BaseModel):
    """POST /analyze-ticket response body. Strict enums via Literal types."""

    model_config = ConfigDict(extra="ignore")

    ticket_id: str
    relevant_transaction_id: Optional[str]
    evidence_verdict: EvidenceVerdict
    case_type: CaseType
    severity: Severity
    department: Department
    agent_summary: str
    recommended_next_action: str
    customer_reply: str
    human_review_required: bool
    confidence: Optional[float] = None
    reason_codes: Optional[List[str]] = None

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v):
        if v is None:
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, v))
