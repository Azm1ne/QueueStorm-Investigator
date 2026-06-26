"""Deterministic safety sanitiser — the non-negotiable guardrail.

Runs on every response regardless of whether it came from the LLM or the fallback engine.
It enforces the three Safety Rules from Problem Statement Section 8 (and the Rubric's
Safety Penalties):

  1. Never ask the customer for PIN / OTP / password / full card number.       (-15)
  2. Never confirm a refund / reversal / unblock / recovery without authority.  (-10)
  3. Never instruct the customer to contact a suspicious third party.           (-10)

Design priority: catch real violations with near-zero false positives, so that the
legitimately-safe phrasings used in the sample pack are left untouched:
  * "Please do not share your PIN or OTP"  -> SAFE (negated ask)
  * "We never ask for your PIN, OTP, or password" -> SAFE (negated ask)
  * "any eligible amount will be returned through official channels" -> SAFE
  * "We recommend contacting the merchant directly" -> SAFE (merchant is not a scam party)
"""

import re

# --- Rule 1: credential solicitation -----------------------------------------------------

_CREDENTIAL_TERMS = re.compile(
    r"(pin|otp|password|cvv|card\s*number|full\s*card|পিন|ওটিপি|পাসওয়ার্ড)", re.IGNORECASE
)
_SOLICIT_TERMS = re.compile(
    r"(share|provide|give|send|tell|enter|type|confirm|verify|need|require|"
    r"what\s+is\s+your|send\s+me|শেয়ার|দিন|পাঠান|বলুন|জানান)",
    re.IGNORECASE,
)
# Negation markers that make a credential mention protective rather than a request.
_NEGATION = re.compile(
    r"(\bnot\b|\bnever\b|n't|\bdo\s+not\b|\bdon'?t\b|\bwon'?t\b|\bcannot\b|\bavoid\b|"
    r"করবেন\s*না|না)",
    re.IGNORECASE,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.?!।\n])\s+")

_SAFE_REMINDER = "Please do not share your PIN or OTP with anyone."

# --- Rule 2: unauthorised refund / reversal / unblock promises ---------------------------

_REFUND_SAFE = "any eligible amount will be returned through official channels"
_ACCOUNT_SAFE = "our team will review your account through official channels"

_PROMISE_PATTERNS = [
    (re.compile(r"\bwe\s*('?ll| will| are going to| shall)\s+refund\b[^.?!।]*", re.I), _REFUND_SAFE),
    (re.compile(r"\bwe\s*('?re| are)\s+refunding\b[^.?!।]*", re.I), _REFUND_SAFE),
    (re.compile(r"\bwe\s*('?ve| have)\s+refunded\b[^.?!।]*", re.I), _REFUND_SAFE),
    (re.compile(r"\byou\s*('?ll| will)\s+be\s+refunded\b[^.?!।]*", re.I), _REFUND_SAFE),
    (re.compile(r"\b(your|the)\s+refund\s+(has\s+been|will\s+be|is\s+being)\b[^.?!।]*", re.I), _REFUND_SAFE),
    (re.compile(r"\bwe\s*('?ll| will)\s+(reverse|return)\b[^.?!।]*", re.I),
     "our team will review the transaction and " + _REFUND_SAFE),
    (re.compile(r"\bwe\s*('?ve| have)\s+(reversed|returned)\b[^.?!।]*", re.I),
     "our team will review the transaction and " + _REFUND_SAFE),
    (re.compile(r"\bwe\s*('?ll| will)\s+(unblock|unlock|restore|recover|reactivate)\b[^.?!।]*", re.I), _ACCOUNT_SAFE),
]

# --- Rule 3: redirect to a suspicious third party (kept narrow) --------------------------

_THIRD_PARTY_PATTERNS = [
    re.compile(
        r"\b(call|contact|dial|reach|message|text)\s+"
        r"(them|him|her|the\s+caller|that\s+(number|person)|this\s+number|"
        r"the\s+number|back\s+the\s+number)\b[^.?!।]*",
        re.I,
    ),
    re.compile(r"\bcall\s+(back|them)\b[^.?!।]*", re.I),
]
_OFFICIAL_ONLY = "use only our official support channels"


def _is_unsafe_credential_ask(sentence: str) -> bool:
    if not _CREDENTIAL_TERMS.search(sentence):
        return False
    if not _SOLICIT_TERMS.search(sentence):
        return False
    if _NEGATION.search(sentence):
        return False
    return True


def sanitize_reply(reply: str, request=None) -> str:
    """Return a safe version of `reply`. Idempotent and conservative."""
    if not isinstance(reply, str) or not reply.strip():
        return _SAFE_REMINDER
    text = reply.strip()

    # Rule 2 + 3: rewrite promise / redirect spans in place.
    for pattern, replacement in _PROMISE_PATTERNS:
        text = pattern.sub(replacement, text)
    for pattern in _THIRD_PARTY_PATTERNS:
        text = pattern.sub(_OFFICIAL_ONLY, text)

    # Rule 1: drop any sentence that actively solicits a credential.
    dropped = False
    kept = []
    for sentence in _SENTENCE_SPLIT.split(text):
        if sentence.strip() and _is_unsafe_credential_ask(sentence):
            dropped = True
            continue
        kept.append(sentence)
    text = " ".join(s.strip() for s in kept if s.strip()).strip()

    # If we removed the credential ask (or emptied the reply), reinforce with the safe
    # reminder so the message still reads well and reinforces the safety rule.
    if dropped or not text:
        if _SAFE_REMINDER.lower() not in text.lower():
            text = (text + " " + _SAFE_REMINDER).strip() if text else _SAFE_REMINDER

    return text


def sanitize_action(action: str) -> str:
    """Lighter sanitiser for recommended_next_action (agent-facing). Only rewrites
    unauthorised promises and third-party redirects; it does not drop sentences, since an
    operational instruction may legitimately mention verifying transaction details."""
    if not isinstance(action, str) or not action.strip():
        return "Review the ticket details and follow up with the customer through official channels."
    text = action.strip()
    for pattern, replacement in _PROMISE_PATTERNS:
        text = pattern.sub(replacement, text)
    for pattern in _THIRD_PARTY_PATTERNS:
        text = pattern.sub(_OFFICIAL_ONLY, text)
    return text


def check_violations(text: str) -> list:
    """Diagnostic helper (used by tests/logging). Returns the rule labels a string trips.
    Never logs or returns the offending secret-like content itself."""
    violations = []
    if isinstance(text, str):
        for sentence in _SENTENCE_SPLIT.split(text):
            if _is_unsafe_credential_ask(sentence):
                violations.append("credential_solicitation")
                break
        for pattern, _ in _PROMISE_PATTERNS:
            if pattern.search(text):
                violations.append("unauthorized_promise")
                break
        for pattern in _THIRD_PARTY_PATTERNS:
            if pattern.search(text):
                violations.append("third_party_redirect")
                break
    return violations
