"""Gmail and Google Calendar, used inside the business's own accounts."""
from __future__ import annotations

import base64
import datetime as dt
from email.mime.text import MIMEText
from email.utils import parseaddr
from zoneinfo import ZoneInfo

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


def google_creds(credentials_file: str, token_file: str):
    """Load the saved token, refreshing it if needed. Run `python -m opzy.auth` once first."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return creds


KEY_HEADER = "X-Opzy-Key"


def event_id_for(key: str) -> str:
    """A Calendar event id derived from an action key.

    Calendar accepts client-chosen ids (base32hex: 0-9 and a-v, 5-1024 chars) and refuses a
    second insert with the same id, so retrying an insert can't create a duplicate event."""
    import hashlib

    alphabet = "0123456789abcdefghijklmnopqrstuv"
    n = int.from_bytes(hashlib.sha256(key.encode()).digest()[:20], "big")
    out = ""
    while n:
        n, r = divmod(n, 32)
        out = alphabet[r] + out
    return "op" + out.rjust(32, "0")  # "op" prefix: y and z are not valid id characters


def _is_conflict(err: Exception) -> bool:
    status = getattr(getattr(err, "resp", None), "status", None)
    return str(status) == "409"


def _header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _strip_quoted(text: str) -> str:
    """Drop the quoted earlier email from a reply ('On Mon, ... wrote:' and '>' lines)."""
    import re

    head = re.split(r"\n\s*On [^\n]{0,300}?(?:\n[^\n]{0,200}?)?wrote:\s*\n", "\n" + text, maxsplit=1)[0]
    lines = [ln for ln in head.splitlines() if not ln.startswith(">")]
    return "\n".join(lines).strip() or text.strip()


def _body_text(payload) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        t = _body_text(part)
        if t:
            return t
    return ""


class Gmail:
    def __init__(self, creds, query: str = "in:inbox newer_than:2d -from:me"):
        from googleapiclient.discovery import build

        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.me = self.svc.users().getProfile(userId="me").execute()["emailAddress"]
        self.query = query

    @staticmethod
    def _parse(full: dict) -> dict:
        h = full["payload"]["headers"]
        return {
            "id": full["id"], "thread_id": full["threadId"],
            "from": _header(h, "From"), "from_email": parseaddr(_header(h, "From"))[1],
            "subject": _header(h, "Subject"), "message_id_header": _header(h, "Message-ID"),
            "body": _strip_quoted(_body_text(full["payload"]) or full.get("snippet", "")),
        }

    def new_messages(self) -> list[dict]:
        res = self.svc.users().messages().list(userId="me", q=self.query, maxResults=20).execute()
        return [self._parse(self.svc.users().messages().get(userId="me", id=m["id"], format="full").execute())
                for m in res.get("messages", [])]

    def thread_replies(self, thread_ids: list[str]) -> list[dict]:
        """Messages from other people in conversations Opzy is following."""
        out = []
        for tid in thread_ids:
            th = self.svc.users().threads().get(userId="me", id=tid, format="full").execute()
            for full in th.get("messages", []):
                m = self._parse(full)
                if m["from_email"].lower() != self.me.lower():
                    out.append(m)
        return out

    def sent_with_key(self, thread_id: str, key: str) -> str | None:
        """Id of a message we already sent in this thread with this action key, if any."""
        th = self.svc.users().threads().get(
            userId="me", id=thread_id, format="metadata", metadataHeaders=[KEY_HEADER]
        ).execute()
        for m in th.get("messages", []):
            if _header(m.get("payload", {}).get("headers", []), KEY_HEADER) == key:
                return m["id"]
        return None

    def send_reply(self, email: dict, body: str, key: str | None = None) -> str:
        """Reply in the candidate's thread. With a key, a retry finds the earlier send instead of
        sending again: the key travels in a header and is checked in the thread first."""
        if key:
            existing = self.sent_with_key(email["thread_id"], key)
            if existing:
                return existing
        subj = email["subject"] if email["subject"].lower().startswith("re:") else f"Re: {email['subject']}"
        msg = MIMEText(body)
        msg["To"] = email["from_email"]
        msg["From"] = self.me
        msg["Subject"] = subj
        if email.get("message_id_header"):
            msg["In-Reply-To"] = email["message_id_header"]
            msg["References"] = email["message_id_header"]
        if key:
            msg[KEY_HEADER] = key
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = self.svc.users().messages().send(
            userId="me", body={"raw": raw, "threadId": email["thread_id"]}
        ).execute()
        return sent["id"]


class Calendar:
    def __init__(self, creds, calendar_id: str = "primary"):
        from googleapiclient.discovery import build

        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.cal = calendar_id

    def free_slots(self, tz: str, minutes: int, hours: tuple[int, int], days_ahead: int, count: int,
                   now: dt.datetime | None = None) -> list[dict]:
        z = ZoneInfo(tz)
        now = now or dt.datetime.now(z)
        start = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + dt.timedelta(days=days_ahead)
        fb = self.svc.freebusy().query(body={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": tz,
            "items": [{"id": self.cal}],
        }).execute()
        busy = [(dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                 dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
                for b in fb["calendars"][self.cal]["busy"]]
        return pick_slots(busy, start, days_ahead, tz, minutes, hours, count)

    def _insert_once(self, body: dict, key: str | None, **kwargs) -> dict:
        """Insert an event; with a key, the id is fixed so a retry returns the existing event."""
        if key:
            body = {**body, "id": event_id_for(key)}
        try:
            return self.svc.events().insert(calendarId=self.cal, body=body, **kwargs).execute()
        except Exception as err:
            if key and _is_conflict(err):  # already created by an earlier attempt
                return self.svc.events().get(calendarId=self.cal, eventId=body["id"]).execute()
            raise

    def hold(self, slot: dict, title: str, description: str, key: str | None = None) -> str:
        ev = self._insert_once({
            "summary": title, "description": description,
            "start": {"dateTime": slot["start"]}, "end": {"dateTime": slot["end"]},
            "transparency": "opaque", "colorId": "9",
        }, key)
        return ev["id"]

    def book(self, slot: dict, title: str, attendee_email: str, description: str,
             key: str | None = None) -> tuple[str, str]:
        """Real meeting: candidate invited, Google Meet link added, invite email sent by Google.

        With a key, a retry can't send the candidate a second invite: the event id is fixed."""
        import uuid

        request_id = event_id_for(key) if key else uuid.uuid4().hex
        ev = self._insert_once({
            "summary": title, "description": description,
            "start": {"dateTime": slot["start"]}, "end": {"dateTime": slot["end"]},
            "attendees": [{"email": attendee_email}],
            "conferenceData": {"createRequest": {"requestId": request_id,
                                                 "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
        }, key, conferenceDataVersion=1, sendUpdates="all")
        return ev["id"], ev.get("hangoutLink", "")

    def delete(self, event_id: str) -> None:
        try:
            self.svc.events().delete(calendarId=self.cal, eventId=event_id).execute()
        except Exception:
            pass  # already gone


def _noop():
    pass


def pick_slots(busy, start, days_ahead, tz, minutes, hours, count) -> list[dict]:
    """Pure function: first `count` free slots on weekdays inside working hours, one per day."""
    z = ZoneInfo(tz)
    out = []
    for d in range(days_ahead):
        day = start + dt.timedelta(days=d)
        if day.weekday() >= 5:
            continue
        t = day.replace(hour=hours[0], minute=0)
        while t + dt.timedelta(minutes=minutes) <= day.replace(hour=hours[1], minute=0):
            e = t + dt.timedelta(minutes=minutes)
            if not any(t < b1 and e > b0 for b0, b1 in busy):
                out.append({"start": t.isoformat(), "end": e.isoformat(),
                            "label": t.astimezone(z).strftime("%A %d %B at %H:%M")})
                break  # one slot per day gives the candidate real choice
            t += dt.timedelta(minutes=30)
        if len(out) >= count:
            break
    return out
