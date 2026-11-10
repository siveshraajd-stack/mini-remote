"""Run from the server folder:  pytest -q"""
import json, os, sys, tempfile
os.environ.update(DATABASE_URL="sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db"), SECRET_KEY="test-secret",
                  ADMIN_EMAIL="admin@team.com", ADMIN_PASSWORD="admin-pass-123", GOOGLE_CLIENT_ID="test-client")
sys.path.insert(0, os.path.dirname(__file__))
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import main


def fake_google(monkeypatch, email, name="Test User", sub=None, verified=True):
    monkeypatch.setattr(main, "verify_google", lambda tok: {"email": email, "name": name, "sub": sub or "sub-" + email,
                                                            "picture": "https://lh3.googleusercontent.com/a/x", "email_verified": verified})


@pytest.fixture(scope="module")
def app():
    with TestClient(main.app) as c:
        yield c


def google(monkeypatch, email, name="Test User"):
    fake_google(monkeypatch, email, name)
    c = TestClient(main.app)
    r = c.post("/api/auth/google", json={"credential": "tok"})
    assert r.status_code == 200, r.text
    return c, r.json()


def admin(app):
    c = TestClient(main.app)
    assert c.post("/api/login", json={"email": "admin@team.com", "password": "admin-pass-123"}).status_code == 200
    return c


def hdr(c): return {"cookie": "session=" + c.cookies.get("session")}


def test_google_login_creates_then_reuses_user(app, monkeypatch):
    c, u1 = google(monkeypatch, "sivesh@gmail.com", "Sivesh")
    assert u1["name"] == "Sivesh" and u1["role"] == "member" and c.get("/api/me").json()["email"] == "sivesh@gmail.com"
    _, u2 = google(monkeypatch, "sivesh@gmail.com", "Sivesh R")
    assert u2["id"] == u1["id"] and u2["name"] == "Sivesh R"


def test_google_login_errors(app, monkeypatch):
    def bad(tok): raise ValueError("bad token")
    monkeypatch.setattr(main, "verify_google", bad)
    assert TestClient(main.app).post("/api/auth/google", json={"credential": "x"}).status_code == 401
    fake_google(monkeypatch, "x@y.com", verified=False)
    assert TestClient(main.app).post("/api/auth/google", json={"credential": "x"}).status_code == 401
    fake_google(monkeypatch, "stranger@evil.com")
    monkeypatch.setenv("ALLOWED_EMAILS", "friend@gmail.com")
    assert TestClient(main.app).post("/api/auth/google", json={"credential": "x"}).status_code == 403


def test_remove_device(app, monkeypatch):
    a = admin(app)
    made = a.post("/api/admin/devices", json={"name": "Lab PC"}).json()
    did, key = made["id"], made["device_key"]
    member, _ = google(monkeypatch, "member@gmail.com", "Mem")
    with a.websocket_connect("/ws/agent") as agent:
        agent.send_text(json.dumps({"type": "register", "device_key": key}))
        assert agent.receive_json()["type"] == "registered"
        assert next(d for d in a.get("/api/devices").json() if d["id"] == did)["online"]
        assert member.delete(f"/api/devices/{did}").status_code == 403      # not the owner
        assert a.delete(f"/api/devices/{did}").status_code == 200
        with pytest.raises(WebSocketDisconnect):
            agent.receive_text()                                              # live session cut
    assert a.delete(f"/api/devices/{did}").status_code == 404                 # already removed
    with a.websocket_connect("/ws/agent") as again:                           # cannot come back with the old key
        again.send_text(json.dumps({"type": "register", "device_key": key}))
        assert again.receive_json()["type"] == "error"
    assert any(r["status"] == "device_removed" for r in a.get("/api/history").json())
    assert any(r["event"] == "device_removed" for r in a.get("/api/admin/audit").json())


def test_history_rows_saved(app, monkeypatch):
    a = admin(app)
    made = a.post("/api/admin/devices", json={"name": "Reception"}).json()
    key, did = made["device_key"], made["id"]
    a.put(f"/api/admin/devices/{did}/grants", json={"user_ids": []})
    viewer_c, _ = google(monkeypatch, "asker@gmail.com", "Asker")
    with main.Session() as s:  # give the asker access
        asker = s.query(main.User).filter_by(email="asker@gmail.com").one()
        s.add(main.Grant(user_id=asker.id, device_id=did)); s.commit()
    send = lambda ws, **m: ws.send_text(json.dumps(m))
    # both sockets must go through ONE TestClient (one event loop), as they do on a real server
    with app.websocket_connect("/ws/agent") as agent, app.websocket_connect("/ws/viewer", headers=hdr(viewer_c)) as v:
        send(agent, type="register", device_key=key); agent.receive_json()
        send(v, type="connect", device_id=did)
        req = agent.receive_json()
        assert req["type"] == "connect_request" and req["requester"]["email"] == "asker@gmail.com"
        assert v.receive_json()["type"] == "waiting"
        send(agent, type="connect_response", accepted=True, view_only=False)
        assert v.receive_json()["accepted"] is True
        # request 1: accepted and completed
        send(v, type="file_request", req_id="browser-chosen")
        fr = agent.receive_json(); rid = fr["req_id"]
        assert rid != "browser-chosen" and fr["requester"]["name"] == "Asker"
        send(agent, type="file_info", req_id=rid, name="report.pdf", path="C:\\Docs\\report.pdf", size=1234)
        send(agent, type="file_offer", req_id=rid, name="report.pdf", size=1234); v.receive_json()
        send(agent, type="file_done", req_id=rid, sha256="ab"); v.receive_json()
        # request 2: owner denies
        send(v, type="file_request"); rid2 = agent.receive_json()["req_id"]
        send(agent, type="file_denied", req_id=rid2, reason="Denied by the owner", code="denied"); v.receive_json()
        # request 3: nobody answers in time
        send(v, type="file_request"); rid3 = agent.receive_json()["req_id"]
        send(agent, type="file_denied", req_id=rid3, reason="expired", code="expired"); v.receive_json()
    rows = {r["status"]: r for r in a.get("/api/history").json() if r["requester_email"] == "asker@gmail.com"}
    assert set(rows) == {"completed", "denied", "expired"}
    done = rows["completed"]
    assert (done["file_name"], done["file_size"], done["device_name"]) == ("report.pdf", 1234, "Reception") and done["completed_ts"]
    assert len(viewer_c.get("/api/history").json()) == 3 and viewer_c.get("/api/history?status=denied").json()[0]["status"] == "denied"
    other, _ = google(monkeypatch, "nosy@gmail.com", "Nosy")
    assert other.get("/api/history").json() == []                             # users only see their own
    assert "report.pdf" in a.get("/api/history.csv").text


def test_history_survives_restart_and_closes_open_rows(app):
    with main.Session() as s:
        s.add(main.Transfer(ts=main.now_iso(), req_id="stuck", status="requested")); s.commit()
    main.init_db()
    with main.Session() as s:
        assert s.query(main.Transfer).filter_by(req_id="stuck").one().status == "failed"