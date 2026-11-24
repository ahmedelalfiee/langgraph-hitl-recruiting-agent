# Human-in-the-loop recruiting agent (LangGraph)

[![tests](https://github.com/ahmedelalfiee/langgraph-hitl-recruiting-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/ahmedelalfiee/langgraph-hitl-recruiting-agent/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-interrupt%20%2B%20checkpointer-1f6feb)

An AI agent that runs a recruiter's candidate conversations **inside the tools they already use**
(Gmail, Google Calendar, WhatsApp) and never sends anything without a human's approval.

A candidate emails. The agent understands the email, finds real free slots in the recruiter's
calendar, drafts a reply, and **pauses**. The recruiter gets the draft on WhatsApp and replies
`OK`, `EDIT <change>` or `NO`. On approval, the reply goes out from their own Gmail and the slot is
held. When the candidate confirms, the agent asks once more and then books the interview with a
Google Meet link and a calendar invite.

<p align="center"><img src="docs/demo.gif" width="300" alt="Demo: candidate email, approval on WhatsApp, interview booked"></p>

## Why this is harder than it looks

Drafting a reply with an LLM takes one API call. The engineering is everything around it:

- a run that **waits hours** for a human, survives a restart, and resumes exactly where it stopped;
- **side effects that must happen once**: an email sent twice or a double-booked slot is worse than no automation;
- a conversation that **continues across emails** (the candidate's "yes, Tuesday works" must be tied to the offer it answers);
- failures that **don't lose work silently**.

## Architecture

```mermaid
flowchart LR
    A[New email<br/>Gmail poll] --> T{triage<br/>LLM}
    T -- spam / other --> S[skip]
    T -- application / question --> F[find_slots<br/>Calendar free/busy]
    F --> D[draft<br/>LLM]
    D --> R[request_approval<br/>WhatsApp]
    T -- reply in an open thread:<br/>candidate confirms --> B[request_booking<br/>WhatsApp]
    R --> W((wait_for_owner<br/>interrupt))
    B --> W
    W -- EDIT --> E[redraft] --> D
    W -- OK, reply --> ACT[act<br/>send in thread + hold slot]
    W -- OK, booking --> BOOK[book<br/>Meet link + invite]
    W -- NO --> X[rejected<br/>nothing sent]
```

| Module | Responsibility |
|---|---|
| `opzy/graph.py` | The `StateGraph`: nodes, routing, the `interrupt()` approval step |
| `opzy/engine.py` | One run per email (`thread_id` = email id); resumes runs with `Command(resume=...)` |
| `opzy/llm.py` | Triage, confirmation parsing and drafting; provider-agnostic (OpenAI or Anthropic) |
| `opzy/google_tools.py` | Gmail (poll, reply in thread, strip quoted text) and Calendar (free/busy, hold, book with Meet) |
| `opzy/memory.py` | SQLite: processed emails, approval codes, open conversation threads, audit log |
| `opzy/whatsapp.py` | Twilio WhatsApp + parsing of `OK / EDIT / NO [CODE]` |
| `opzy/app.py` | FastAPI service: background Gmail poller + signed WhatsApp webhook |
| `business.yaml` | Per-client config: roles, tone, working hours, interview settings, signature |

## Design decisions

**1. Durable pauses with a checkpointer.** Approval is a LangGraph `interrupt()` backed by a
`SqliteSaver`. A pending approval is just persisted graph state, so the process can restart and
the owner's `OK` the next morning still resumes the right run (`test_resume_survives_restart`).

**2. Side effects live in their own nodes.** When a run resumes, LangGraph re-executes the
interrupted node from the top. If sending the email lived in the same node as the interrupt, the
candidate would get it twice. So `wait_for_owner` does nothing but wait, and every irreversible
action (`act`, `book`) runs in a node after it, exactly once.

**3. Approval codes, not "the latest message".** Each pending decision gets a short code
(`OK A3F2`), so an owner with several drafts waiting can't approve the wrong one. A bare `OK`
falls back to the most recent.

**4. Conversations, not single emails.** After the agent replies, it stores the thread, the slots
it offered and the calendar hold. When the candidate answers, triage recognises the thread and an
LLM classifies the answer as *confirm / reschedule / decline / other*. A confirmation goes to
booking (deleting the hold first); a reschedule produces a fresh offer and replaces the old hold.

**5. Fail loudly, retry safely.** An email is marked as processed before its run starts (so two
polls can't race), but if the run fails before reaching the owner, the mark is removed and the next
poll retries it. One bad email never blocks the others (`test_failed_email_is_retried`).

**6. Least privilege at the edges.** Gmail reading is scoped by a query (e.g. one alias), and
the WhatsApp webhook ignores anyone but the owner's number and verifies Twilio's request
signature when a public URL is configured.

**7. Model-agnostic.** The two LLM jobs sit behind a small interface with OpenAI and Anthropic
implementations, picked from whichever key is configured.

## Tests

21 tests run end to end against fake Gmail, Calendar, WhatsApp and LLM, so no keys are needed:

```bash
pip install -r requirements.txt
python -m pytest -q        # 21 passed
```

They cover the guarantees that matter:

| Guarantee | Test |
|---|---|
| Nothing is sent before approval | `test_nothing_sent_before_approval` |
| Approve sends the reply and holds the slot | `test_approve_sends_and_books` |
| `EDIT` redrafts and asks again | `test_edit_redrafts_then_asks_again` |
| `NO` sends nothing | `test_reject_sends_nothing`, `test_owner_can_refuse_booking` |
| The same email is handled once | `test_same_email_only_once` |
| A pending approval survives a restart | `test_resume_survives_restart` |
| A failed email is retried, not lost | `test_failed_email_is_retried` |
| Candidate confirms, owner approves, interview booked | `test_candidate_confirms_then_owner_books` |
| Candidate asks for another time, gets a new offer | `test_candidate_asks_other_time_gets_new_offer` |
| Slots skip busy times and weekends | `test_pick_slots_skips_busy_and_weekends` |
| The webhook ignores anyone but the owner | `test_webhook` |

## Run it

Requires Python 3.11+, a Google Cloud OAuth client (Gmail + Calendar APIs), a Twilio WhatsApp
sandbox, and an OpenAI or Anthropic key.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # add your keys; never commit .env
python -m opzy.auth               # one-time Google sign-in (creates token.json locally)
python -m opzy.cli                # terminal mode: approve drafts by typing OK / EDIT / NO
```

For WhatsApp approvals, run the service and expose it (e.g. with ngrok), then point the Twilio
sandbox's incoming-message webhook at `https://<public-url>/whatsapp`:

```bash
uvicorn opzy.app:app --port 8000
```

Edit `business.yaml` for your business: roles, tone, working hours, interview length and
signature. Set `GMAIL_QUERY` so the agent only reads the emails it should.

## Limitations and next steps

- Gmail is polled; Gmail push notifications (Pub/Sub) would cut latency and API calls.
- The Twilio sandbox needs re-joining every 72 hours; production would use the WhatsApp Business
  Platform with interactive Approve / Edit buttons.
- Triage quality is checked by tests with a fake LLM. A labelled set of real emails, scored per
  model and prompt version, is the next step for measuring it properly.
- One business per deployment; multi-tenant storage and per-client OAuth are next.

## License

MIT
