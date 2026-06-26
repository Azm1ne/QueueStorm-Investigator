"""Deterministic rule-based investigator.

This is the fallback that runs when the LLM is unavailable, errors, times out, or returns
unusable output (and when GEMINI_API_KEY is unset). It implements the decision logic
distilled from the 10 sample cases: keyword case-type detection, amount/type transaction
matching, the consistent / inconsistent / insufficient_data verdict, severity, routing,
escalation, and a safe reply. Its output is always run through normalize() + sanitize, so
it can never produce an out-of-spec or unsafe response.

It is intentionally conservative: when the evidence is ambiguous it returns
insufficient_data with relevant_transaction_id = null rather than guessing.
"""

import re

from models import TicketRequest
from normalize import DEFAULT_HUMAN_REVIEW, DEFAULT_SEVERITY

_BANGLA_RE = re.compile(r"[ঀ-৿]")
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_NUM_RE = re.compile(r"\d[\d,]*")

# Which transaction type(s) plausibly back each case_type, used to narrow matches.
CASE_TO_TXN_TYPE = {
    "wrong_transfer": {"transfer"},
    "payment_failed": {"payment"},
    "refund_request": {"payment", "refund"},
    "duplicate_payment": {"payment"},
    "merchant_settlement_delay": {"settlement"},
    "agent_cash_in_issue": {"cash_in"},
}


def detect_language(request: TicketRequest) -> str:
    lang = (request.language or "").lower()
    if lang in ("bn", "en"):
        return lang
    if _BANGLA_RE.search(request.complaint or ""):
        return "bn"
    return "en"


def extract_amounts(text: str) -> list:
    """Pull plausible BDT amounts out of free text. Skips phone-like and tiny numbers."""
    if not text:
        return []
    t = text.translate(_BN_DIGITS)
    out = []
    for m in _NUM_RE.findall(t):
        digits = m.replace(",", "")
        if not digits.isdigit():
            continue
        if len(digits) >= 8:  # phone number / id, not an amount
            continue
        if digits.startswith("0") and len(digits) > 4:
            continue
        val = float(digits)
        if val < 10:  # filters time noise like "2pm" -> 2
            continue
        out.append(val)
    return out


def detect_case_type(text: str) -> str:
    t = (text or "").lower()

    # 1. Phishing / social engineering (safety-critical, checked first).
    if re.search(r"phishing|scam|fraud|prank", t):
        return "phishing_or_social_engineering"
    if re.search(r"otp|\bpin\b|password|ওটিপি|পিন|পাসওয়ার্ড", t) and re.search(
        r"call|called|someone|stranger|asked|share|block|claim|suspicious|unknown|"
        r"sms|message|text|from bkash|কল|ফোন|ব্লক",
        t,
    ):
        return "phishing_or_social_engineering"

    # 2. Duplicate payment.
    if re.search(r"twice|two times|double|duplicate|deducted twice|charged twice|"
                 r"দুইবার|দুবার|দুই বার", t):
        return "duplicate_payment"

    # 3. Agent cash-in.
    if re.search(r"এজেন্ট|agent", t) and re.search(
        r"cash[ -]?in|ক্যাশ ?ইন|deposit|জমা", t
    ):
        return "agent_cash_in_issue"

    # 4. Merchant settlement delay.
    if re.search(r"settle|settlement|সেটেলমেন্ট", t) or (
        re.search(r"merchant|মার্চেন্ট", t) and re.search(r"sales|বিক্রি", t)
    ):
        return "merchant_settlement_delay"

    # 5. Payment failed (with balance deducted).
    if re.search(r"fail|ব্যর্থ", t) and re.search(
        r"deduct|balance|ব্যালেন্স|কাটা|\bbut\b", t
    ):
        return "payment_failed"

    # 6. Wrong transfer.
    if re.search(r"wrong (number|person|recipient|account)|ভুল (নম্বর|মানুষ|নাম্বার)", t) or (
        re.search(r"sent|send|transfer|পাঠ", t)
        and re.search(r"wrong|didn'?t get|did not get|not receive|পাইনি|পায়নি|mistake|ভুল", t)
    ):
        return "wrong_transfer"

    # 7. Refund request.
    if re.search(r"refund|ফেরত|রিফান্ড|money back", t):
        return "refund_request"

    return "other"


def _by_type(txns, case_type):
    types = CASE_TO_TXN_TYPE.get(case_type)
    if not types:
        return list(txns)
    matched = [t for t in txns if (t.type or "").lower() in types]
    return matched if matched else list(txns)


def _match_single(case_type, history, amounts):
    """Return (relevant_transaction_id, verdict) for non-duplicate cases."""
    if not history:
        return None, "insufficient_data"

    if amounts:
        amt = set(amounts)
        cands = [t for t in history if t.amount is not None and t.amount in amt]
        if not cands:  # amount stated but nothing matches it
            return None, "insufficient_data"
    else:
        cands = _by_type(history, case_type)

    if len(cands) > 1:
        typed = _by_type(cands, case_type)
        if len(typed) == 1:
            cands = typed

    if len(cands) == 1:
        match = cands[0]
        verdict = "consistent"
        # Inconsistency signal: a "wrong transfer" to an established/repeat recipient.
        if case_type == "wrong_transfer" and match.counterparty:
            repeats = sum(1 for t in history if t.counterparty == match.counterparty)
            if repeats > 1:
                verdict = "inconsistent"
        return match.transaction_id, verdict

    # Zero matches or multiple equally-plausible matches -> do not guess.
    return None, "insufficient_data"


def _match_duplicate(history, amounts):
    groups = {}
    for t in history:
        groups.setdefault((t.amount, t.counterparty), []).append(t)
    dup_groups = [g for g in groups.values() if len(g) > 1]
    if amounts:
        amt = set(amounts)
        filtered = [g for g in dup_groups if g[0].amount in amt]
        dup_groups = filtered or dup_groups
    if dup_groups:
        group = max(dup_groups, key=len)
        latest = sorted(group, key=lambda t: t.timestamp or "")[-1]
        return latest.transaction_id, "consistent"  # point at the suspected duplicate
    return _match_single("duplicate_payment", history, amounts)


def _compose_reply(case_type, verdict, rid, lang):
    ref_en = f" regarding transaction {rid}" if rid else ""

    if lang == "bn":
        head = f"আপনার লেনদেন {rid} এর বিষয়ে আমরা অবগত হয়েছি।" if rid else "আপনার অভিযোগটি আমরা পেয়েছি।"
        return (
            f"{head} আমাদের সংশ্লিষ্ট দল এটি যাচাই করবে এবং অফিসিয়াল চ্যানেলে আপনাকে জানাবে। "
            "অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।"
        )

    if case_type == "phishing_or_social_engineering":
        return (
            "Thank you for reaching out before sharing any information. We never ask for your "
            "PIN, OTP, or password under any circumstances. Please do not share these with "
            "anyone, even if they claim to be from us. Our fraud team has been notified."
        )
    if case_type == "refund_request":
        return (
            "Thank you for reaching out. Refunds for completed merchant payments depend on the "
            "merchant's own policy. We recommend contacting the merchant directly. Please do "
            "not share your PIN or OTP with anyone."
        )
    if case_type == "merchant_settlement_delay":
        return (
            f"We have noted your concern about settlement {rid or 'in question'}. Our merchant "
            "operations team will check the batch status and update you on the expected "
            "settlement time through official channels."
        )
    if case_type in ("payment_failed", "duplicate_payment"):
        return (
            f"We have noted your concern{ref_en}. Our payments team will review it and any "
            "eligible amount will be returned through official channels. Please do not share "
            "your PIN or OTP with anyone."
        )
    if case_type == "wrong_transfer" and verdict == "insufficient_data":
        return (
            "Thank you for reaching out. We see more than one transaction that could match. "
            "Could you share the recipient's number so we can identify the right transaction? "
            "Please do not share your PIN or OTP with anyone."
        )
    if case_type == "other":
        return (
            "Thank you for reaching out. To help you faster, please share the transaction ID, "
            "the amount involved, and a short description of what went wrong. Please do not "
            "share your PIN or OTP with anyone."
        )
    # wrong_transfer matched, or any other matched case
    return (
        f"We have noted your concern{ref_en}. Our team will review the case and contact you "
        "through official support channels. Please do not share your PIN or OTP with anyone."
    )


def _compose_summary_action(case_type, verdict, rid):
    txn = rid or "the reported transaction"
    table = {
        "wrong_transfer": (
            f"Customer reports a possible wrong transfer involving {txn}.",
            f"Verify {txn} with the customer and follow the wrong-transfer dispute workflow.",
        ),
        "payment_failed": (
            f"Customer reports a failed payment ({txn}) with a possible balance deduction.",
            f"Investigate the ledger status of {txn}; reverse within SLA if the balance was deducted on a failed payment.",
        ),
        "refund_request": (
            f"Customer requests a refund for {txn} (merchant payment).",
            "Explain that refund eligibility depends on the merchant's policy and guide the customer to the merchant.",
        ),
        "duplicate_payment": (
            f"Customer reports a duplicate payment; {txn} is the suspected duplicate.",
            f"Verify the duplicate with payments operations and reverse {txn} only if the biller confirms a single charge.",
        ),
        "merchant_settlement_delay": (
            f"Merchant reports settlement {txn} delayed beyond the expected window.",
            f"Route to merchant operations to verify the settlement batch status for {txn}.",
        ),
        "agent_cash_in_issue": (
            f"Customer reports an agent cash-in ({txn}) not reflected in their balance.",
            f"Investigate the status of {txn} with agent operations and resolve within the cash-in SLA.",
        ),
        "phishing_or_social_engineering": (
            "Customer reports a suspected phishing or social-engineering attempt.",
            "Escalate to the fraud team, reassure the customer that we never ask for OTP, and log the incident.",
        ),
        "other": (
            "Customer raised a vague concern with insufficient detail to identify a transaction.",
            "Ask the customer for the transaction ID, amount, time, and a description of the issue.",
        ),
    }
    summary, action = table.get(case_type, table["other"])
    if verdict == "inconsistent":
        summary += " The transaction history appears inconsistent with this claim."
        action = "Flag for human review; " + action[0].lower() + action[1:]
    elif verdict == "insufficient_data" and rid is None and case_type != "other":
        summary += " The evidence is insufficient to confirm a single matching transaction."
    return summary, action


def analyze(request: TicketRequest) -> dict:
    lang = detect_language(request)
    complaint = request.complaint or ""
    history = request.transaction_history
    amounts = extract_amounts(complaint)
    case_type = detect_case_type(complaint)

    if case_type in ("phishing_or_social_engineering", "other"):
        rid, verdict = None, "insufficient_data"
    elif case_type == "duplicate_payment":
        rid, verdict = _match_duplicate(history, amounts)
    else:
        rid, verdict = _match_single(case_type, history, amounts)

    # Severity (overrides on top of the per-case default).
    if case_type == "phishing_or_social_engineering":
        severity = "critical"
    elif verdict == "inconsistent":
        severity = "medium"
    elif case_type == "wrong_transfer" and verdict == "insufficient_data":
        severity = "medium"
    else:
        severity = DEFAULT_SEVERITY.get(case_type, "low")

    # Escalation: wrong_transfer escalates only once a transaction is actually matched.
    if case_type == "wrong_transfer":
        human_review = verdict != "insufficient_data"
    else:
        human_review = DEFAULT_HUMAN_REVIEW.get(case_type, False)

    summary, action = _compose_summary_action(case_type, verdict, rid)
    reply = _compose_reply(case_type, verdict, rid, lang)

    return {
        "ticket_id": request.ticket_id,
        "relevant_transaction_id": rid,
        "evidence_verdict": verdict,
        "case_type": case_type,
        "severity": severity,
        "department": None,  # normalize() fills the canonical department from case_type
        "agent_summary": summary,
        "recommended_next_action": action,
        "customer_reply": reply,
        "human_review_required": human_review,
        "confidence": 0.55,
        "reason_codes": [case_type, verdict, "rule_based_fallback"],
    }
