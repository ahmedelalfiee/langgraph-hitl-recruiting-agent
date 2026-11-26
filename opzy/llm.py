"""The two AI jobs: understand an incoming email, and draft the reply."""
from __future__ import annotations

import json
import re
from typing import Protocol

from .config import Business


class LLM(Protocol):
    def triage(self, email: dict, biz: Business) -> dict: ...
    def draft(self, email: dict, triage: dict, slots: list[dict], biz: Business, note: str = "") -> str: ...


TRIAGE_PROMPT = """You are the inbox agent for {business}. Classify this email and extract details.

Open roles:
{roles}

Email
From: {sender}
Subject: {subject}
Body:
\"\"\"{body}\"\"\"

Reply with JSON only, no other text:
{{"kind": "application" | "candidate_question" | "other" | "spam",
  "role": "<exact title from the open roles, or null>",
  "candidate_first_name": "<the candidate's first name as they sign the email; only use the From name if the email isn't signed>",
  "summary": "<one short sentence for the owner>"}}"""

DRAFT_PROMPT = """You write emails for {business}, signed by {sender_name}.

Tone: {tone}

Open roles:
{roles}

Interview: {minutes}-minute {format}.
Free slots (owner's local time, {tz}):
{slots}

Incoming email from {sender}, subject "{subject}":
\"\"\"{body}\"\"\"

What the inbox agent found: {summary} (kind: {kind}, role: {role})
{note}
Write only the reply body. If this is an application for an open role, thank them briefly,
offer the first slot and mention the second as a backup, and say a Google Meet link follows
once they confirm. If the role is not open, say so kindly and keep the door open.
If it is a question, answer it simply. End with exactly this signature:
{signature}"""


CONFIRM_PROMPT = """You help {business} schedule interviews. We offered the candidate these slots:
{slots}

The candidate replied:
\"\"\"{body}\"\"\"

Reply with JSON only:
{{"decision": "confirm" | "reschedule" | "decline" | "other",
  "slot_index": <0-based index of the offered slot they accepted, or null>,
  "summary": "<one short sentence for the owner>"}}
"confirm" only if they clearly accept one of the offered slots."""


def _slots_text(slots: list[dict]) -> str:
    return "\n".join(f"- {s['label']}" for s in slots) or "- (no free slots found, ask for their availability)"


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in model output: {text[:200]}")
    return json.loads(m.group(0))


class PromptLLM:
    """Shared prompts; subclasses only implement _ask()."""

    model = ""
    usage = None  # {"calls", "input_tokens", "output_tokens"}, filled in by _ask (used by evals)

    def _count(self, input_tokens, output_tokens) -> None:
        if self.usage is None:
            self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.usage["calls"] += 1
        self.usage["input_tokens"] += int(input_tokens or 0)
        self.usage["output_tokens"] += int(output_tokens or 0)

    def _ask(self, prompt: str, max_tokens: int = 600) -> str:  # pragma: no cover
        raise NotImplementedError

    def triage(self, email: dict, biz: Business) -> dict:
        out = self._ask(
            TRIAGE_PROMPT.format(
                business=biz.business_name, roles=biz.roles_text(),
                sender=email["from"], subject=email["subject"], body=email["body"][:6000],
            ),
            300,
        )
        return _json(out)

    def confirm(self, email: dict, slots: list[dict], biz: Business) -> dict:
        out = self._ask(CONFIRM_PROMPT.format(
            business=biz.business_name,
            slots="\n".join(f"{i}. {x['label']}" for i, x in enumerate(slots)),
            body=email["body"][:4000]), 200)
        return _json(out)

    def draft(self, email, triage, slots, biz, note=""):
        iv = biz.interview
        return self._ask(
            DRAFT_PROMPT.format(
                business=biz.business_name, sender_name=biz.sender_name, tone=biz.tone,
                roles=biz.roles_text(), minutes=iv["minutes"], format=iv["format"], tz=biz.timezone,
                slots=_slots_text(slots), sender=email["from"], subject=email["subject"],
                body=email["body"][:6000], summary=triage.get("summary", ""), kind=triage.get("kind"),
                role=triage.get("role"), signature=biz.signature.strip(),
                note=f"Owner's instruction for this reply: {note}" if note else "",
            )
        )


class AnthropicLLM(PromptLLM):
    def __init__(self, api_key: str, model: str):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _ask(self, prompt: str, max_tokens: int = 600) -> str:
        r = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}]
        )
        u = getattr(r, "usage", None)
        self._count(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0))
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()


class OpenAILLM(PromptLLM):
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model

    def _ask(self, prompt: str, max_tokens: int = 600) -> str:
        r = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=max_tokens * 4,  # reasoning models spend some tokens thinking
        )
        u = getattr(r, "usage", None)
        self._count(getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0))
        return (r.choices[0].message.content or "").strip()


def make_llm(settings) -> PromptLLM:
    key = settings.openai_api_key if settings.llm_provider == "openai" else settings.anthropic_api_key
    if not key.strip():
        raise SystemExit(
            f"No API key found for '{settings.llm_provider}'. Open the .env file in the opzy-mvp folder and "
            "fill in OPENAI_API_KEY=... (check the file is called exactly .env, not .env.txt)."
        )
    if settings.llm_provider == "openai":
        return OpenAILLM(settings.openai_api_key, settings.openai_model)
    return AnthropicLLM(settings.anthropic_api_key, settings.anthropic_model)
