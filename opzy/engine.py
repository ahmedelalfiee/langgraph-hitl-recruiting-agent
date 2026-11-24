"""Ties the graph to the outside world: new emails start runs, WhatsApp replies resume them."""
from __future__ import annotations

import logging
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .graph import Deps, build_graph
from .whatsapp import parse_reply

log = logging.getLogger("opzy")

HELP = ("Reply *OK* to send the latest draft, *EDIT <change>* to redraft it, or *NO* to skip it. "
        "Add the 4-letter code to pick a specific one, e.g. OK A3F2.")


class Engine:
    def __init__(self, deps: Deps, checkpoint_path: str):
        self.d = deps
        conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
        self.graph = build_graph(deps, SqliteSaver(conn))

    @staticmethod
    def _cfg(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def handle_email(self, email: dict) -> dict:
        if self.d.memory.seen(email["id"]):
            return {"status": "already_seen"}
        self.d.memory.mark_seen(email["id"])
        log.info("new email %s from %s", email["id"], email["from"])
        try:
            out = self.graph.invoke({"email": email}, self._cfg(email["id"]))
        except Exception:
            # failed before reaching the owner: forget it so the next poll retries it
            self.d.memory.unmark_seen(email["id"])
            raise
        return out.get("result", {"status": "waiting_for_owner"} if "__interrupt__" in out else {})

    def poll(self) -> int:
        n = 0
        msgs = list(self.d.gmail.new_messages())
        threads = self.d.memory.open_thread_ids()
        if threads:  # follow conversations Opzy started (the candidate's answer)
            msgs += self.d.gmail.thread_replies(threads)
        unique = {m["id"]: m for m in msgs}.values()
        for e in unique:
            if not self.d.memory.seen(e["id"]):
                try:
                    self.handle_email(e)
                    n += 1
                except Exception as err:  # one bad email must not stop the others
                    log.error("failed on %s: %s", e["id"], err)
        return n

    def handle_owner_reply(self, text: str) -> str:
        r = parse_reply(text)
        if r["action"] == "unknown":
            return HELP
        row = self.d.memory.find_pending(r.get("code"))
        if not row:
            return "Nothing is waiting for your OK right now 👌"
        code, run_id, who = row
        decision = {"action": r["action"], "note": r.get("note", "")}
        self.graph.invoke(Command(resume=decision), self._cfg(run_id))
        # act / rejected / redraft send their own WhatsApp messages
        return ""
