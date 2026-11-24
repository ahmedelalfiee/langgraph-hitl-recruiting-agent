"""The WhatsApp webhook only listens to the owner and resumes the right run."""
from fastapi.testclient import TestClient

import opzy.app as appmod
from tests.test_flow import EMAIL, code_from, make


def test_webhook(tmp_path, monkeypatch):
    eng, d = make(tmp_path)
    monkeypatch.setattr(appmod, "build_engine", lambda: eng)
    monkeypatch.setattr(appmod, "_poller", lambda: None)
    with TestClient(appmod.app) as c:
        eng.handle_email(EMAIL)
        owner = d.biz.owner_whatsapp
        r = c.post("/whatsapp", data={"From": "whatsapp:+999", "Body": "OK"})
        assert r.status_code == 200 and d.gmail.sent == []        # stranger ignored
        r = c.post("/whatsapp", data={"From": owner, "Body": "hello"})
        assert "Reply *OK*" in r.text                             # help text
        r = c.post("/whatsapp", data={"From": owner, "Body": f"OK {code_from(d.whatsapp.out[0])}"})
        assert len(d.gmail.sent) == 1
        assert c.get("/health").json()["pending_approvals"] == 0
