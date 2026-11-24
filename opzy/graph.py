"""The Opzy agent graph for the recruiter flow.

New email                    Reply in a conversation Opzy started
   │                                   │
 triage ◄──────────────────────────────┘
   ├─(spam/other)──► skip ──► END
   ├─(application / question / reschedule)──► find_slots ──► draft ──► request_approval
   └─(candidate confirmed a slot)──► request_booking
                                                   │
                     wait_for_owner (interrupt: run pauses until the owner replies)
                       ├─ OK on a reply   ──► act   (send email + hold slot)      ──► END
                       ├─ OK on a booking ──► book  (invite + Meet link + email)  ──► END
                       ├─ EDIT            ──► redraft ──► draft ...
                       └─ NO              ──► rejected ──► END

Nothing reaches a candidate without the owner's OK. Side effects (WhatsApp, Gmail,
Calendar) live in their own nodes, because an interrupted node re-runs on resume.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .config import Business
from .memory import Memory


class State(TypedDict, total=False):
    email: dict
    triage: dict
    thread: dict
    confirm: dict
    slots: list
    draft: str
    note: str
    code: str
    mode: str
    decision: dict
    result: dict


@dataclass
class Deps:
    biz: Business
    llm: Any
    gmail: Any
    calendar: Any
    whatsapp: Any
    memory: Memory


def build_graph(d: Deps, checkpointer):
    biz = d.biz

    def run_id(s: State) -> str:
        return s["email"]["id"]

    def who(s: State) -> str:
        return (s.get("triage") or {}).get("candidate_first_name") or s["email"]["from"]

    # ---------------------------------------------------------------- understand
    def triage(s: State):
        thread = d.memory.open_thread(s["email"]["thread_id"])
        if thread:  # the candidate is answering our offer
            c = d.llm.confirm(s["email"], thread["slots"], biz)
            d.memory.log(run_id(s), "candidate_answer", c)
            base = {"role": thread["role"], "candidate_first_name": thread["candidate_name"],
                    "summary": c.get("summary", "")}
            if c.get("decision") == "confirm" and c.get("slot_index") is not None \
                    and 0 <= int(c["slot_index"]) < len(thread["slots"]):
                return {"thread": thread, "confirm": c, "triage": {**base, "kind": "confirmation"}}
            if c.get("decision") == "reschedule":
                return {"thread": thread, "triage": {**base, "kind": "application"},
                        "note": "The candidate already applied and was offered slots; they asked for a "
                                "different time. Offer the new slots. Do not thank them for applying again."}
            return {"thread": thread, "triage": {**base, "kind": "candidate_question"},
                    "note": "This is a reply in an ongoing conversation about an interview. Answer it simply."}
        t = d.llm.triage(s["email"], biz)
        d.memory.log(run_id(s), "triaged", t)
        return {"triage": t}

    def after_triage(s: State):
        k = s["triage"].get("kind")
        if k == "confirmation":
            return "request_booking"
        return "find_slots" if k in ("application", "candidate_question") else "skip"

    def skip(s: State):
        d.memory.log(run_id(s), "skipped", {"kind": s["triage"].get("kind")})
        return {"result": {"status": "skipped"}}

    # ---------------------------------------------------------------- reply path
    def find_slots(s: State):
        if s["triage"].get("kind") != "application":
            return {"slots": []}
        iv = biz.interview
        slots = d.calendar.free_slots(
            biz.timezone, iv["minutes"], tuple(iv["working_hours"]), iv["days_ahead"], iv["slots_to_offer"]
        )
        d.memory.log(run_id(s), "slots", {"slots": [x["label"] for x in slots]})
        return {"slots": slots}

    def draft(s: State):
        text = d.llm.draft(s["email"], s["triage"], s.get("slots", []), biz, s.get("note", ""))
        d.memory.log(run_id(s), "drafted", {"chars": len(text)})
        return {"draft": text}

    def request_approval(s: State):
        code = d.memory.new_approval(run_id(s), who(s))
        slot_line = f"\n🗓 Interview: {s['slots'][0]['label']} (hold)" if s.get("slots") else ""
        msg = (
            f"📨 Reply to {who(s)} is ready ({s['triage'].get('role') or s['triage'].get('kind')})"
            f"{slot_line}\n\n{s['draft']}\n\n"
            f"Reply *OK {code}* to send · *EDIT {code} <change>* to redraft · *NO {code}* to skip"
        )
        d.whatsapp.send(biz.owner_whatsapp, msg)
        d.memory.log(run_id(s), "approval_requested", {"code": code})
        return {"code": code, "mode": "reply"}

    # ---------------------------------------------------------------- booking path
    def request_booking(s: State):
        t = s["thread"]
        slot = t["slots"][int(s["confirm"]["slot_index"])]
        code = d.memory.new_approval(run_id(s), t["candidate_name"])
        d.whatsapp.send(biz.owner_whatsapp, (
            f"🎉 {t['candidate_name']} confirmed *{slot['label']}* ({t['role']}).\n\n"
            f"Book it, add a Google Meet link and send the invite?\n\n"
            f"Reply *OK {code}* to book · *NO {code}* to leave it"
        ))
        d.memory.log(run_id(s), "booking_requested", {"code": code, "slot": slot["label"]})
        return {"code": code, "mode": "booking", "slots": [slot], "draft": ""}

    # ---------------------------------------------------------------- owner decision
    def wait_for_owner(s: State):
        decision = interrupt({"code": s["code"], "mode": s.get("mode"), "draft": s.get("draft", "")})
        return {"decision": decision}

    def route_decision(s: State):
        a = s["decision"].get("action")
        if s.get("mode") == "booking":
            return "book" if a == "approve" else "rejected"
        return {"approve": "act", "edit": "redraft", "reject": "rejected"}.get(a, "rejected")

    def redraft(s: State):
        d.memory.close_approval(s["code"], "edited")
        return {"note": (s.get("note", "") + " " + s["decision"].get("note", "")).strip()}

    def rejected(s: State):
        d.memory.close_approval(s["code"], "rejected")
        d.whatsapp.send(biz.owner_whatsapp, f"👍 Got it, nothing was sent ({s['code']}).")
        d.memory.log(run_id(s), "rejected")
        return {"result": {"status": "rejected"}}

    # ---------------------------------------------------------------- actions
    def act(s: State):
        e = s["email"]
        sent_id = d.gmail.send_reply(e, s["draft"])
        event_id: Optional[str] = None
        if s.get("slots"):
            old = s.get("thread")
            if old and old.get("hold_event_id"):
                d.calendar.delete(old["hold_event_id"])  # new slots replace the old hold
            event_id = d.calendar.hold(
                s["slots"][0],
                f"HOLD: {biz.interview['title']} · {who(s)} ({s['triage'].get('role')})",
                f"Offered by Opzy, approved by owner. Candidate: {e['from']}",
            )
            d.memory.save_thread(e["thread_id"], (s.get("thread") or {}).get("candidate_email") or e["from_email"],
                                 who(s), s["triage"].get("role"), s["slots"], event_id)
        d.memory.close_approval(s["code"], "approved")
        d.memory.log(run_id(s), "sent", {"gmail_id": sent_id, "event_id": event_id})
        done = "✅ Sent from your Gmail" + (", slot held in your calendar." if event_id else ".")
        d.whatsapp.send(biz.owner_whatsapp, done)
        return {"result": {"status": "sent", "gmail_id": sent_id, "event_id": event_id}}

    def book(s: State):
        t, slot, e = s["thread"], s["slots"][0], s["email"]
        if t.get("hold_event_id"):
            d.calendar.delete(t["hold_event_id"])
        event_id, meet = d.calendar.book(
            slot, f"{biz.interview['title']}: {t['candidate_name']} · {t['role']} ({biz.business_name})",
            t["candidate_email"], f"{biz.interview['minutes']}-minute interview. Booked by Opzy, approved by owner.",
        )
        body = (
            f"Hi {t['candidate_name']},\n\nGreat, you're booked for {slot['label']} "
            f"({biz.interview['minutes']} minutes, {biz.timezone.split('/')[-1]} time).\n\n"
            + (f"Google Meet link: {meet}\n" if meet else "")
            + "You'll also get a calendar invite.\n\n" + biz.signature.strip()
        )
        sent_id = d.gmail.send_reply(e, body)
        d.memory.set_thread_status(t["thread_id"], "booked")
        d.memory.close_approval(s["code"], "approved")
        d.memory.log(run_id(s), "booked", {"event_id": event_id, "meet": meet})
        d.whatsapp.send(biz.owner_whatsapp,
                        f"📅 Booked: {t['candidate_name']}, {slot['label']}. Invite and Meet link sent.")
        return {"result": {"status": "booked", "event_id": event_id, "meet": meet, "gmail_id": sent_id}}

    g = StateGraph(State)
    for name, fn in [("triage", triage), ("skip", skip), ("find_slots", find_slots), ("draft", draft),
                     ("request_approval", request_approval), ("request_booking", request_booking),
                     ("wait_for_owner", wait_for_owner), ("redraft", redraft), ("rejected", rejected),
                     ("act", act), ("book", book)]:
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", after_triage,
                            {"find_slots": "find_slots", "skip": "skip", "request_booking": "request_booking"})
    g.add_edge("skip", END)
    g.add_edge("find_slots", "draft")
    g.add_edge("draft", "request_approval")
    g.add_edge("request_approval", "wait_for_owner")
    g.add_edge("request_booking", "wait_for_owner")
    g.add_conditional_edges("wait_for_owner", route_decision,
                            {"act": "act", "book": "book", "redraft": "redraft", "rejected": "rejected"})
    g.add_edge("redraft", "draft")
    g.add_edge("act", END)
    g.add_edge("book", END)
    g.add_edge("rejected", END)
    return g.compile(checkpointer=checkpointer)
