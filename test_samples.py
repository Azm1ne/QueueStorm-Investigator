"""Regression harness for the 10 public sample cases.

Usage:
  python test_samples.py                      # in-process: exercises the full guardrail
                                              # pipeline (fallback engine when no API key)
  python test_samples.py http://localhost:8000  # hit a running endpoint over HTTP

For each case it compares the key fields the judge cares about (per the sample pack's
"functional equivalence" note): relevant_transaction_id, evidence_verdict, case_type,
department, severity, plus a safety check on customer_reply. Prints per-case results and a
summary. Severity mismatches are reported as warnings (the spec only asks for "comparable").
"""

import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "SUST_Preli_Sample_Cases.json")

EXACT_FIELDS = ["relevant_transaction_id", "evidence_verdict", "case_type", "department"]


def analyze_local(input_dict):
    from fallback import analyze
    from models import TicketRequest, TicketResponse
    from normalize import normalize_response
    from safety import sanitize_action, sanitize_reply

    ticket = TicketRequest(**input_dict)
    raw = analyze(ticket)
    final = normalize_response(raw, ticket)
    final["customer_reply"] = sanitize_reply(final["customer_reply"], ticket)
    final["recommended_next_action"] = sanitize_action(final["recommended_next_action"])
    return TicketResponse(**final).model_dump()


def analyze_http(base_url, input_dict):
    data = json.dumps(input_dict).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/analyze-ticket",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    base_url = sys.argv[1] if len(sys.argv) > 1 else None
    mode = f"HTTP {base_url}" if base_url else "in-process (fallback engine)"

    from safety import check_violations

    with open(SAMPLES, encoding="utf-8") as fh:
        pack = json.load(fh)

    cases = pack["cases"]
    passed = 0
    warned = 0
    print(f"Running {len(cases)} sample cases — mode: {mode}\n" + "=" * 70)

    for case in cases:
        cid = case["id"]
        expected = case["expected_output"]
        try:
            actual = analyze_http(base_url, case["input"]) if base_url else analyze_local(case["input"])
        except Exception as exc:
            print(f"[ERROR] {cid}: request failed: {exc}")
            continue

        mismatches = [
            f"{f}: expected {expected.get(f)!r}, got {actual.get(f)!r}"
            for f in EXACT_FIELDS
            if actual.get(f) != expected.get(f)
        ]

        sev_warn = actual.get("severity") != expected.get("severity")
        hr_warn = actual.get("human_review_required") != expected.get("human_review_required")
        violations = check_violations(actual.get("customer_reply", ""))

        ok = not mismatches and not violations
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        print(f"[{status}] {cid} — {case['label']}")
        for m in mismatches:
            print(f"        x {m}")
        if violations:
            print(f"        x SAFETY violation in customer_reply: {violations}")
        if sev_warn:
            warned += 1
            print(f"        ~ severity: expected {expected.get('severity')!r}, got {actual.get('severity')!r}")
        if hr_warn:
            print(f"        ~ human_review_required: expected {expected.get('human_review_required')}, got {actual.get('human_review_required')}")

    print("=" * 70)
    print(f"Key-field PASS: {passed}/{len(cases)}   (severity warnings: {warned})")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
