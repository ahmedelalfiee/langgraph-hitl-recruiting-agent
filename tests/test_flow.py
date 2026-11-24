"""End-to-end tests with fake Gmail, Calendar, WhatsApp and AI. No network, no keys."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from opzy.config import load_business
from opzy.engine import Engine
from opzy.google_tools import pick_slots
from opzy.graph import Deps
from opzy.memory import Memory
from opzy.whatsapp import parse_reply


class FakeLLM:
    def __init__(self, kind="application"):
        self.kind, self.notes = kind, []

    def triage(self, email, biz):
        return {"kind": self.kind, "role": "Senior Frontend Developer",
                "candidate_first_name": "Omar", "summary": "Applying for frontend role"}

    def confirm(self, email, slots, biz):
        b = email["body"].lower()
        if "tuesday" in b:
            return {"decision": "confirm", "slot_index": 0, "summary": "Confirmed Tuesday"}
        if "other time" in b:
            return {"decision": "reschedule", "slot_index": None, "summary": "Wants another time"}
        return {"decision": "other", "slot_index": None, "summary": "Question"}

    def draft(self, email, triage, slots, biz, note=""):
        self.notes.append(note)
        first = slots[0]["label"] if slots else "no slot"
        return f"Hi Omar, would {first} work?{' [' + note + ']' if note else ''}\nBest,\nSara"


class FakeGmail:
    me = "talentco@example.com"

    def __init__(self, msgs=None):
        self.msgs, self.sent, self.replies = msgs or [], [], []

    def new_messages(self):
        return self.msgs

    def thread_replies(self, thread_ids):
        return [m for m in self.replies if m["thread_id"] in thread_ids]

    def send_reply(self, email, body):
        self.sent.append((email["from_email"], body))
        return "sent-1"


class FakeCalendar:
    def __init__(self):
        self.holds = []

    def free_slots(self, tz, minutes, hours, days_ahead, count):
        return [{"start": "2026-11-24T15:00:00+00:00", "end": "2026-11-24T15:30:00+00:00",
                 "label": "Tuesday 24 November at 15:00"},
                {"start": "2026-11-25T11:00:00+00:00", "end": "2026-11-25T11:30:00+00:00",
                 "label": "Wednesday 25 November at 11:00"}][:count]

    def hold(self, slot, title, description):
        self.holds.append((slot["label"], title))
        return f"hold-{len(self.holds)}"

    def book(self, slot, title, attendee, description):
        self.booked = (slot["label"], attendee)
        return "evt-booked", "https://meet.google.com/abc-defg-hij"

    def delete(self, event_id):
        self.deleted = getattr(self, "deleted", []) + [event_id]


class FakeWA:
    def __init__(self):
        self.out = []

    def send(self, to, body):
        self.out.append(body)
        return "wa"


EMAIL = {"id": "m1", "thread_id": "t1", "from": "Omar Hassan <omar@example.com>",
         "from_email": "omar@example.com", "subject": "Application: Senior Frontend Developer",
         "message_id_header": "<x@y>", "body": "Hi, I'm applying. Omar"}


def make(tmp_path, kind="application"):
    d = Deps(biz=load_business(), llm=FakeLLM(kind), gmail=FakeGmail([EMAIL]),
             calendar=FakeCalendar(), whatsapp=FakeWA(), memory=Memory(str(tmp_path / "m.db")))
    return Engine(d, str(tmp_path / "cp.db")), d


def code_from(msg):
    import re
    return re.search(r"OK ([0-9A-F]{4})", msg).group(1)


def test_nothing_sent_before_approval(tmp_path):
    eng, d = make(tmp_path)
    res = eng.handle_email(EMAIL)
    assert res["status"] == "waiting_for_owner"
    assert d.gmail.sent == [] and d.calendar.holds == []
    assert "Reply to Omar is ready" in d.whatsapp.out[0]
    assert "Tuesday 24 November at 15:00" in d.whatsapp.out[0]


def test_approve_sends_and_books(tmp_path):
    eng, d = make(tmp_path)
    eng.handle_email(EMAIL)
    code = code_from(d.whatsapp.out[0])
    eng.handle_owner_reply(f"ok {code.lower()}")
    assert d.gmail.sent[0][0] == "omar@example.com"
    assert d.calendar.holds[0][0] == "Tuesday 24 November at 15:00"
    assert d.whatsapp.out[-1].startswith("✅ Sent")
    assert d.memory.pending_count() == 0


def test_edit_redrafts_then_asks_again(tmp_path):
    eng, d = make(tmp_path)
    eng.handle_email(EMAIL)
    eng.handle_owner_reply("EDIT mention we're hybrid in Lisbon")
    assert d.gmail.sent == []
    assert "hybrid in Lisbon" in d.whatsapp.out[-1]  # new draft shown for approval
    eng.handle_owner_reply("👍")  # approves the latest pending
    assert "[mention we're hybrid in Lisbon]" in d.gmail.sent[0][1]


def test_reject_sends_nothing(tmp_path):
    eng, d = make(tmp_path)
    eng.handle_email(EMAIL)
    eng.handle_owner_reply("no")
    assert d.gmail.sent == [] and d.calendar.holds == []
    assert "nothing was sent" in d.whatsapp.out[-1]


def test_spam_is_skipped_quietly(tmp_path):
    eng, d = make(tmp_path, kind="spam")
    assert eng.handle_email(EMAIL)["status"] == "skipped"
    assert d.whatsapp.out == []


def test_same_email_only_once(tmp_path):
    eng, d = make(tmp_path)
    eng.poll()
    eng.poll()
    assert len(d.whatsapp.out) == 1


def test_resume_survives_restart(tmp_path):
    eng, d = make(tmp_path)
    eng.handle_email(EMAIL)
    code = code_from(d.whatsapp.out[0])
    eng2 = Engine(d, str(tmp_path / "cp.db"))  # fresh process, same checkpoint file
    eng2.handle_owner_reply(f"OK {code}")
    assert len(d.gmail.sent) == 1


@pytest.mark.parametrize("text,action", [("OK", "approve"), ("yes A3F2", "approve"), ("✅", "approve"),
                                         ("no", "reject"), ("EDIT A3F2 shorter please", "edit"),
                                         ("hello", "unknown"), ("EDIT", "unknown")])
def test_parse_reply(text, action):
    assert parse_reply(text)["action"] == action


def test_pick_slots_skips_busy_and_weekends():
    z = ZoneInfo("Europe/Lisbon")
    start = dt.datetime(2026, 11, 23, tzinfo=z)  # Monday
    busy = [(start.replace(hour=9), start.replace(hour=17))]  # Monday fully booked
    s = pick_slots(busy, start, 7, "Europe/Lisbon", 30, (9, 17), 2)
    assert s[0]["label"].startswith("Tuesday 24 November at 09:00")
    assert s[1]["label"].startswith("Wednesday")


def test_failed_email_is_retried(tmp_path):
    eng, d = make(tmp_path)

    class Boom(FakeLLM):
        def triage(self, email, biz):
            raise RuntimeError("no key")
    d.llm = Boom()
    eng2 = Engine(d, str(tmp_path / "cp2.db"))
    eng2.poll()
    assert not d.memory.seen("m1")          # not marked done
    d.llm = FakeLLM()
    eng3 = Engine(d, str(tmp_path / "cp3.db"))
    eng3.poll()
    assert "Reply to Omar is ready" in d.whatsapp.out[0]


REPLY = {"id": "m2", "thread_id": "t1", "from": "Omar Hassan <omar@example.com>",
         "from_email": "omar@example.com", "subject": "Re: Application: Senior Frontend Developer",
         "message_id_header": "<z@y>", "body": "Yes, Tuesday works for me!"}


def _approve_first(eng, d):
    eng.handle_email(EMAIL)
    eng.handle_owner_reply(f"OK {code_from(d.whatsapp.out[0])}")


def test_candidate_confirms_then_owner_books(tmp_path):
    eng, d = make(tmp_path)
    _approve_first(eng, d)
    d.gmail.replies = [REPLY]
    eng.poll()
    assert "Omar confirmed *Tuesday 24 November at 15:00*" in d.whatsapp.out[-1]
    assert not hasattr(d.calendar, "booked")            # nothing booked before OK
    eng.handle_owner_reply("OK")
    assert d.calendar.booked == ("Tuesday 24 November at 15:00", "omar@example.com")
    assert d.calendar.deleted == ["hold-1"]              # hold replaced by the real meeting
    assert "meet.google.com" in d.gmail.sent[-1][1]
    assert d.whatsapp.out[-1].startswith("📅 Booked")
    assert d.memory.open_thread("t1") is None            # conversation closed


def test_candidate_asks_other_time_gets_new_offer(tmp_path):
    eng, d = make(tmp_path)
    _approve_first(eng, d)
    d.gmail.replies = [dict(REPLY, body="Could we find some other time?")]
    eng.poll()
    assert "Reply to Omar is ready" in d.whatsapp.out[-1]
    eng.handle_owner_reply("OK")
    assert len(d.gmail.sent) == 2 and d.calendar.deleted == ["hold-1"]


def test_owner_can_refuse_booking(tmp_path):
    eng, d = make(tmp_path)
    _approve_first(eng, d)
    d.gmail.replies = [REPLY]
    eng.poll()
    eng.handle_owner_reply("NO")
    assert not hasattr(d.calendar, "booked")
    assert "nothing was sent" in d.whatsapp.out[-1]


def test_strip_quoted_reply():
    from opzy.google_tools import _strip_quoted
    t = "Yes, Tuesday works!\n\nMichael\n\nOn Mon, 23 Nov 2026 at 13:30, Talent Co. <\ntalentco@example.com> wrote:\n> Hi Michael,\n> old text"
    assert _strip_quoted(t) == "Yes, Tuesday works!\n\nMichael"
    assert _strip_quoted("Just a plain email") == "Just a plain email"
