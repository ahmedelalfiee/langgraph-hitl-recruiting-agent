"""An LLM judge for the one failure the rule-based checks can't see: a draft stating a fact
the business never gave the agent (a salary, a policy, a "roles page", a promise).

The judge sees exactly what the drafting model saw (business.yaml, the offered slots, the
candidate's email) plus the draft. For every factual claim it must quote the words that
support it, or write NONE. Its quotes are then checked against the sources in code, so a
claim only counts as grounded if its support really exists.

Why evidence: a first version that simply asked "list the unsupported claims" flagged almost
every sentence, including the offered slots and "I'll check and get back to you", even though
the prompt said not to (7 of 8 grounded drafts in evals/cases/judge.jsonl wrongly failed).

The judge is itself evaluated on evals/cases/judge.jsonl, so its verdicts are trusted only as
far as that set shows.
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

Go through the draft sentence by sentence and list each factual claim about the business, a role,
pay, policies, the team, the interview process, the slots, or a decision about the candidate.
For each one, copy the exact words from the SOURCES that support it, or write NONE.

Skip sentences that are only: greetings or thanks; a brief positive remark about experience the
candidate mentioned; saying the assistant will check or get back to them; asking the candidate
for something; or polite sign-offs. Those are not claims.

Reply with JSON only:
{{"claims": [{{"claim": "<short quote from the draft>", "support": "<exact words copied from the SOURCES, or NONE>"}}]}}"""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip(" .\"'").lower()


def _supported(support: str, sources: str) -> bool:
    """The quoted support must really be in the sources (the judge can't invent evidence)."""
    s = _norm(support)
    return bool(s) and s != "none" and s in _norm(sources)


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
    unsupported = [c.get("claim", "") for c in claims if not _supported(c.get("support", ""), sources)]
    return {"grounded": not unsupported, "unsupported": unsupported, "claims": claims}
