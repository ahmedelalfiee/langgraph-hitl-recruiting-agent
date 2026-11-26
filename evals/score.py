"""Scoring for the agent's three LLM jobs. Pure functions, no network, unit-tested.

Triage and confirmation are scored against labels. Drafts are scored with checks that
encode the business rules (business.yaml) and the one failure that matters most for a
recruiting agent: offering an interview time that isn't really free.
"""
from __future__ import annotations

import re
import unicodedata

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# Drafts may answer in the candidate's language; the agent's clients are in Portugal.
PT = {"monday": "segunda", "tuesday": "terca", "wednesday": "quarta", "thursday": "quinta",
      "friday": "sexta", "saturday": "sabado", "sunday": "domingo"}


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return s.strip().lower()


def score_triage(case: dict, out: dict) -> dict:
    kinds = case.get("kind_any") or [case["kind"]]
    roles = case["role_any"] if "role_any" in case else [case["role"]]
    checks = {"kind": out.get("kind") in kinds}
    if set(kinds) & {"application", "candidate_question"}:  # the role only matters for candidates
        checks["role"] = _norm(out.get("role")) in {_norm(r) for r in roles}
    if case.get("name"):
        checks["name"] = _norm(out.get("candidate_first_name")) == _norm(case["name"])
    return checks


def score_confirm(case: dict, out: dict) -> dict:
    decisions = case.get("decision_any") or [case["decision"]]
    checks = {"decision": out.get("decision") in decisions}
    if out.get("decision") == "confirm" and case["decision"] == "confirm":
        checks["slot"] = out.get("slot_index") is not None and int(out["slot_index"]) == case["slot_index"]
    return checks


def _times(text: str) -> set[int]:
    """Clock times mentioned, as minutes after midnight: 15:00, 3pm, 3:30 pm, 11h."""
    found = set()
    for h, m in re.findall(r"\b([01]?\d|2[0-3])[:.h]([0-5]\d)\b", text):
        found.add(int(h) * 60 + int(m))
    for h, m, ap in re.findall(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\b", text, re.I):
        hour = int(h) % 12 + (12 if ap.lower() == "p" else 0)
        found.add(hour * 60 + int(m or 0))
    return found


def _slot_time(label: str) -> int:
    h, m = re.search(r"(\d{1,2}):(\d{2})", label).groups()
    return int(h) * 60 + int(m)


def _slot_day(label: str) -> str:
    return label.split()[0].lower()


def _days_in(text: str) -> set[str]:
    """Weekdays mentioned, in English or Portuguese, returned in English."""
    t = _norm(text)
    return {d for d in WEEKDAYS if re.search(rf"\b({d}|{PT[d]})\b", t)}


MONEY = re.compile(r"[€$£]\s?\d|\d[\d.,]*\s?(?:k\b|eur|euros?|usd|dollars?)", re.I)
DEFERRAL = re.compile(r"\b(check|confirm|get back|come back|find out|follow up|let you know)\b", re.I)


def score_draft(case: dict, text: str, signature: str, words: tuple[int, int] = (20, 110)) -> dict:
    slots = case["slots"]
    body = text.strip()
    sig = signature.strip()
    without_sig = body[: -len(sig)] if body.endswith(sig) else body
    low = body.lower()
    mentioned_days = _days_in(body)
    offered_times = {_slot_time(s["label"]) for s in slots}
    offered_days = {_slot_day(s["label"]) for s in slots}

    checks = {
        "ends_with_signature": body.endswith(sig),
        "no_invented_times": _times(without_sig) <= offered_times,
        "no_invented_days": mentioned_days <= offered_days,
        "word_count": words[0] <= len(without_sig.split()) <= words[1],
        "no_em_dash": "—" not in body,
        "no_markdown": "**" not in body and not re.search(r"^\s*#", body, re.M),
        "no_invented_money": not MONEY.search(without_sig),  # business.yaml has no pay information
    }
    if case.get("unknown"):  # the answer isn't in business.yaml: the draft must say it will check
        checks["defers_unknown"] = bool(DEFERRAL.search(without_sig))
    if case["expect"] == "offer":
        first = slots[0]["label"]
        checks["offers_first_slot"] = _slot_day(first) in mentioned_days and _slot_time(first) in _times(without_sig)
        checks["mentions_meet_link"] = "meet" in low
    if case["expect"] in ("not_open", "answer"):
        checks["no_slot_offered"] = not _times(without_sig) and not mentioned_days
    return checks


def summarise(rows: list[dict]) -> dict:
    """Pass rate per check, and the share of cases where every check passed."""
    by_check: dict[str, list[bool]] = {}
    for r in rows:
        for k, v in r["checks"].items():
            by_check.setdefault(k, []).append(bool(v))
    rates = {k: round(100 * sum(v) / len(v), 1) for k, v in sorted(by_check.items())}
    all_pass = round(100 * sum(all(r["checks"].values()) for r in rows) / max(len(rows), 1), 1)
    return {"cases": len(rows), "all_checks_pass_pct": all_pass, "check_pass_pct": rates}
