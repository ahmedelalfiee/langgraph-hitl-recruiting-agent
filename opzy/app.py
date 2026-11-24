"""The running service: polls Gmail and receives the owner's WhatsApp replies.

Run it with:  uvicorn opzy.app:app --port 8000
"""
from __future__ import annotations

import logging
import threading
import time
from xml.sax.saxutils import escape

from fastapi import FastAPI, HTTPException, Request, Response

from .config import load_business, settings
from .engine import Engine
from .google_tools import Calendar, Gmail, google_creds
from .graph import Deps
from .llm import make_llm
from .memory import Memory
from .whatsapp import TwilioWhatsApp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("opzy")


def build_engine() -> Engine:
    creds = google_creds(settings.google_credentials_file, settings.google_token_file)
    deps = Deps(
        biz=load_business(),
        llm=make_llm(settings),
        gmail=Gmail(creds, settings.gmail_query),
        calendar=Calendar(creds, settings.calendar_id),
        whatsapp=TwilioWhatsApp(settings.twilio_sid, settings.twilio_token, settings.twilio_from),
        memory=Memory(settings.db_path),
    )
    return Engine(deps, settings.db_path.replace(".db", "") + "_checkpoints.db")


app = FastAPI(title="Opzy MVP")
engine: Engine | None = None
_lock = threading.Lock()  # one graph step at a time keeps SQLite and Gmail simple


def _poller():
    while True:
        try:
            with _lock:
                n = engine.poll()
            if n:
                log.info("handled %d new email(s)", n)
        except Exception:
            log.exception("poll failed")
        time.sleep(settings.poll_seconds)


@app.on_event("startup")
def _start():
    global engine
    engine = build_engine()
    threading.Thread(target=_poller, daemon=True).start()
    log.info("Opzy is running. Watching %s every %ss.", engine.d.gmail.me, settings.poll_seconds)


@app.get("/health")
def health():
    return {"ok": True, "pending_approvals": engine.d.memory.pending_count() if engine else None}


@app.post("/whatsapp")
async def whatsapp(request: Request):
    form = dict(await request.form())
    if settings.public_base_url:
        from twilio.request_validator import RequestValidator

        ok = RequestValidator(settings.twilio_token).validate(
            settings.public_base_url.rstrip("/") + "/whatsapp", form, request.headers.get("X-Twilio-Signature", "")
        )
        if not ok:
            raise HTTPException(403, "bad signature")
    owner = engine.d.biz.owner_whatsapp
    if form.get("From") != owner:
        log.warning("ignored WhatsApp from %s (not the owner)", form.get("From"))
        return Response("<Response/>", media_type="application/xml")
    with _lock:
        reply = engine.handle_owner_reply(form.get("Body", ""))
    body = f"<Response><Message>{escape(reply)}</Message></Response>" if reply else "<Response/>"
    return Response(body, media_type="application/xml")
