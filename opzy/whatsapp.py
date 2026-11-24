"""WhatsApp messages to the owner through Twilio, and parsing their replies."""
from __future__ import annotations

import re


class TwilioWhatsApp:
    def __init__(self, sid: str, token: str, from_number: str):
        from twilio.rest import Client

        self.client = Client(sid, token)
        self.from_number = from_number

    def send(self, to: str, body: str) -> str:
        # WhatsApp messages max out at 1600 characters through Twilio
        return self.client.messages.create(from_=self.from_number, to=to, body=body[:1590]).sid


def parse_reply(text: str) -> dict:
    """Owner replies:
      OK            approve the latest pending reply
      OK A3F2       approve that specific one
      NO [A3F2]     don't send
      EDIT [A3F2] <instruction>   redraft with the instruction, e.g. "EDIT offer Thursday instead"
    """
    t = (text or "").strip()
    m = re.match(r"^(ok|yes|approve|y|1|👍|✅)(?=\s|$)\s*([0-9A-Fa-f]{4})?\s*$", t, re.I)
    if m:
        return {"action": "approve", "code": (m.group(2) or "").upper() or None}
    m = re.match(r"^(no|reject|cancel|n|2)(?=\s|$)\s*([0-9A-Fa-f]{4})?\s*$", t, re.I)
    if m:
        return {"action": "reject", "code": (m.group(2) or "").upper() or None}
    m = re.match(r"^edit\b\s*([0-9A-Fa-f]{4}\b)?\s*(.*)$", t, re.I | re.S)
    if m and m.group(2).strip():
        return {"action": "edit", "code": (m.group(1) or "").upper() or None, "note": m.group(2).strip()}
    return {"action": "unknown"}
