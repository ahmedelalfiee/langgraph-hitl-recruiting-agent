"""An LLM judge for the one failure the rule-based checks can't see: a draft stating a fact
the business never gave the agent (a salary, a policy, a "roles page", a promise).

The judge sees exactly what the drafting model saw (business.yaml, the offered slots, the
candidate's email) plus the draft. For every factual claim it must quote the words that
support it, or write NONE. Its quotes are then checked against the sources in code, so a
claim only counts as grounded if its support really exists.

Why evidence: a first version that simply asked "list the unsupported claims" flagged almost
every sentence, including the offered slots and "I'll check and get back to you", even though
the prompt said not to (7 of 8 grounded drafts in evals/cases/judge.jsonl wrongly failed).

Telling it which sentences to skip didn't hold (each rewording moved the false alarms around),
so it labels every sentence instead (fact / commitment / courtesy) and only facts and
commitments are checked.

The judge is itself evaluated on evals/cases/judge.jsonl, and until it clears the "judge"
threshold its verdict on drafts is advisory: reported, but not part of the pass/fail gate.
"""
from __future__ import annotations

import re
import unicodedata

from opzy.llm import _json, _slots_text

JUDGE_PROMPT = """You check an email that an assistant drafted for a recruiting business. The
assistant may only state facts found in the SOURCES below.

SOURCES
[Business profile]
Business: {business}, emails signed by {sender_name}
Open roles (complete list; any other role is not open):
{roles}
Interview: {minutes}-minute {format}
Free slots offered:
{slots}

[Candidate's email]
{email}

DRAFT
{draft}

Go through the draft sentence by sentence (skip the signature) and give each sentence a type:
- "fact": states something about the business, a role, pay, policies, the team, the interview
  process or the slots.
- "commitment": promises something to or about the candidate, such as reviewing their CV,
  shortlisting them, interviewing them, keeping their CV on file, or sharing information later.
  Saying the assistant will check and get back to them is "courtesy", not a commitment.
- "courtesy": greetings, thanks, sign-offs, a positive remark about the candidate's own
  experience, inviting them to ask or apply, or saying the assistant will check, doesn't know
  yet, or will get back to them.

For each "fact" or "commitment", copy the exact words from the SOURCES that say the same thing,
or write NONE. The support must state it, not just mention the same topic. A commitment about
this candidate is never in the SOURCES, so it is always NONE. Saying a role isn't open is
supported by "any other role is not open".

Reply with JSON only:
{{"claims": [{{"claim": "<the sentence>", "type": "fact" | "commitment" | "courtesy", "support": "<exact words copied from the SOURCES, or NONE>"}}]}}"""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip(" .\"'").lower()


# Deferrals are allowed by the drafting prompt; the judge sometimes lists them anyway.
DEFERRAL = re.compile(r"\b(check|get back|let you know|find out|come back|don'?t have|do not have|"
                      r"don'?t know|not sure|confirm with)\b", re.I)


def _supported(support: str, sources: str) -> bool:
    """Every line the judge quotes must really be in the sources (it can't invent evidence).
    A quote may join several lines of the profile, e.g. a heading and one of its bullets."""
    src = _norm(sources)
    parts = [_norm(re.sub(r"^\s*[-•]\s*", "", p)) for p in re.split(r"\n|\s-\s", str(support))]
    parts = [p for p in parts if p]
    return bool(parts) and "none" not in parts and all(p in src for p in parts)


def judge_draft(judge, biz, case: dict, draft: str) -> dict:
    """Returns {"grounded", "unsupported", "claims"}; judge is any opzy PromptLLM."""
    iv = biz.interview
    e = case["email"]
    email = f"From: {e['from']}\nSubject: {e['subject']}\n\n{e['body']}"
    slots = _slots_text(case["slots"])
    prompt = JUDGE_PROMPT.format(
        business=biz.business_name, sender_name=biz.sender_name, roles=biz.roles_text(),
        minutes=iv["minutes"], format=iv["format"], slots=slots, email=email, draft=draft,
    )
    # everything above the DRAFT is a valid source, including "any other role is not open"
    profile = prompt.split("\nDRAFT\n")[0]
    claims = _json(judge._ask(prompt, 700)).get("claims") or []
    sources = profile
    checked = [c for c in claims if c.get("type", "fact") != "courtesy"]
    unsupported = [c.get("claim", "") for c in checked
                   if not _supported(c.get("support", ""), sources) and not DEFERRAL.search(_norm(c.get("claim", "")))]
    return {"grounded": not unsupported, "unsupported": unsupported, "claims": claims}
