"""Test the flow from your terminal before setting up the WhatsApp webhook.

    python -m opzy.cli            # real Gmail + Calendar + AI, approvals typed here
    python -m opzy.cli --whatsapp # also sends the approval message to your WhatsApp

It checks the inbox once, shows the draft, and waits for you to type OK / EDIT ... / NO.
"""
import argparse

from .config import load_business, settings
from .engine import Engine
from .google_tools import Calendar, Gmail, google_creds
from .graph import Deps
from .llm import make_llm
from .memory import Memory


class ConsoleWhatsApp:
    def send(self, to, body):
        print("\n" + "─" * 60 + f"\n📱 WhatsApp to {to}:\n{body}\n" + "─" * 60)
        return "console"


class Both:
    def __init__(self, a, b):
        self.a, self.b = a, b

    def send(self, to, body):
        self.a.send(to, body)
        return self.b.send(to, body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--whatsapp", action="store_true", help="also send approvals to WhatsApp via Twilio")
    args = ap.parse_args()

    creds = google_creds(settings.google_credentials_file, settings.google_token_file)
    wa = ConsoleWhatsApp()
    if args.whatsapp:
        from .whatsapp import TwilioWhatsApp
        wa = Both(wa, TwilioWhatsApp(settings.twilio_sid, settings.twilio_token, settings.twilio_from))
    deps = Deps(load_business(), make_llm(settings),
                Gmail(creds, settings.gmail_query), Calendar(creds, settings.calendar_id), wa, Memory(settings.db_path))
    eng = Engine(deps, settings.db_path.replace(".db", "") + "_checkpoints.db")

    print(f"Checking {deps.gmail.me} for: {settings.gmail_query}  (AI: {settings.llm_provider})")
    n = eng.poll()
    print(f"{n} new email(s) handled.")
    while deps.memory.pending_count():
        reply = input("\nYour reply (OK / EDIT <change> / NO): ").strip()
        msg = eng.handle_owner_reply(reply)
        if msg:
            print(msg)
    print("Nothing waiting for approval. Done.")


if __name__ == "__main__":
    main()
