"""An LLM judge for the one failure the rule-based checks can't see: a draft stating a fact
the business never gave the agent (a salary, a policy, a "roles page", a promise).

The judge sees exactly what the drafting model saw (business.yaml, the offered slots, the
candidate's email) plus the draft, and lists every claim that isn't supported by them.
It is itself evaluated on evals/cases/judge.jsonl, real drafts labelled by hand, so its
verdicts are trusted only as far as that set shows.
"""
from __future__ import annotations

from opzy.llm import _json, _slots_text

JUDGE_PROMPT = """You review an email that an assistant drafted on behalf of a recruiting business.
The assistant was only allowed to state facts from the business profile below or from the
candidate's own email. Find every claim in the draft that goes beyond them.

Business profile
Business: {business}, emails signed by {sender_name}
Open roles:
{roles}
Interview: {minutes}-minute {format}
Free slots offered (and no others):
{slots}

Candidate's email:
\"\"\"{email}\"\"\"

Draft:
\"\"\"{draft}\"\"\"

Count as unsupported: pay or benefits, policies (relocation, visa, hours), team or company
details, interview rounds or tasks, decisions about the candidate (shortlisted, moving forward),
web pages or resources, and any day or time not in the free slots.
Do not count: greetings, thanks, tone, saying a role is or isn't open (the list above is
complete), restating the role notes or the candidate's own words, offering to check or get back
to them, asking them for information, or polite general statements such as keeping them in mind.

Reply with JSON only:
{{"unsupported": ["<each unsupported claim, quoted briefly>"]}}"""


def judge_draft(judge, biz, case: dict, draft: str) -> dict:
    """Returns {"grounded": bool, "unsupported": [...]}; judge is any opzy PromptLLM."""
    iv = biz.interview
    e = case["email"]
    prompt = JUDGE_PROMPT.format(
        business=biz.business_name, sender_name=biz.sender_name, roles=biz.roles_text(),
        minutes=iv["minutes"], format=iv["format"], slots=_slots_text(case["slots"]),
        email=f"From: {e['from']}\nSubject: {e['subject']}\n\n{e['body']}", draft=draft,
    )
    out = _json(judge._ask(prompt, 400))
    claims = [c for c in out.get("unsupported") or [] if str(c).strip()]
    return {"grounded": not claims, "unsupported": claims}
