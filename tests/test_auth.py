"""Access control (#6), response hardening and attribute escaping (#7)."""
import json
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import app

ROOT = Path(__file__).resolve().parent.parent


def test_api_requires_token_when_configured(claude_home, monkeypatch):
    monkeypatch.setenv("CC_MGR_TOKEN", "s3cret")
    c = TestClient(app)
    r = c.get("/api/agents")
    assert r.status_code == 401 and "token" in r.json()["detail"]
    assert c.get("/api/agents", headers={"X-CC-MGR-Token": "wrong"}).status_code == 401
    assert c.get("/api/agents", headers={"X-CC-MGR-Token": "s3cret"}).status_code == 200
    assert c.get("/").status_code == 200  # the static UI itself stays public


def test_url_token_is_exchanged_for_httponly_cookie(claude_home, monkeypatch):
    monkeypatch.setenv("CC_MGR_TOKEN", "s3cret")
    c = TestClient(app)
    r = c.get("/?token=s3cret", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/projects").status_code == 200  # cookie kept by the client
    assert c.get("/?token=nope", follow_redirects=False).status_code == 401


def test_no_token_configured_keeps_api_open(claude_home):
    assert TestClient(app).get("/api/agents").status_code == 200


def test_host_allowlist_blocks_dns_rebinding(claude_home, monkeypatch):
    monkeypatch.setenv("CC_MGR_ALLOWED_HOSTS", "localhost,127.0.0.1,::1")
    c = TestClient(app)
    assert c.get("/api/agents", headers={"host": "attacker.example:8765"}).status_code == 400
    for host in ("localhost:8765", "127.0.0.1:8765", "[::1]:8765", "localhost"):
        assert c.get("/api/agents", headers={"host": host}).status_code == 200, host


def test_read_only_mode_blocks_writes(claude_home, monkeypatch):
    monkeypatch.setenv("CC_MGR_READ_ONLY", "1")
    c = TestClient(app)
    pname = c.get("/api/projects").json()[0]["name"]
    assert c.put(f"/api/projects/{pname}/doc", json={"content": "x"}).status_code == 403
    r = c.post(f"/api/projects/{pname}/sessions/sess-c1/delete", json={"export_first": False})
    assert r.status_code == 403
    assert (claude_home / "projects" / pname / "sess-c1.jsonl").is_file()
    assert c.get(f"/api/projects/{pname}/doc").status_code == 200


def test_security_headers(claude_home):
    r = TestClient(app).get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_access_log_never_records_the_token():
    import logging
    filters = logging.getLogger("uvicorn.access").filters
    assert any(getattr(f, "_cc_mgr_redact", False) for f in filters)
    # the shape of uvicorn's access record: client, method, path, http version, status
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                               '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5", "GET", "/?token=s3cret&x=1", "1.1", 303), None)
    for f in filters:
        f.filter(record)
    msg = record.getMessage()
    assert "s3cret" not in msg and "token=<redacted>&x=1" in msg


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def test_app_js_parses():
    subprocess.run([_node(), "--check", str(ROOT / "frontend" / "app.js")], check=True)


def test_esc_escapes_quotes_for_attributes():
    src = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    fn = re.search(r"function esc\(s\) \{.*?\n\}", src, re.S).group(0)
    payload = 'x" onmouseover="alert(1)\' <b>&'
    out = subprocess.run([_node(), "-e", fn + f"\nprocess.stdout.write(esc({json.dumps(payload)}));"],
                         capture_output=True, text=True, check=True).stdout
    assert out == "x&quot; onmouseover=&quot;alert(1)&#39; &lt;b&gt;&amp;"


def test_launcher_security_env_and_url(launcher):
    env = launcher._security_env("loopback", "tok", read_only=False)
    assert env == {"CC_MGR_TOKEN": "tok", "CC_MGR_READ_ONLY": "",
                   "CC_MGR_ALLOWED_HOSTS": "localhost,127.0.0.1,::1"}
    env = launcher._security_env("0.0.0.0", "", read_only=True)
    assert env["CC_MGR_ALLOWED_HOSTS"] == "" and env["CC_MGR_READ_ONLY"] == "1"
    assert launcher._url("loopback", 8765, "abc") == "http://localhost:8765/?token=abc"
    assert launcher._url("::1", 8765) == "http://[::1]:8765/"


def test_launcher_pidfile_is_owner_only(launcher):
    launcher._write_pidfile(123, "loopback", 8765, "tok")
    assert stat.S_IMODE(launcher._pid_file().stat().st_mode) == 0o600
    assert launcher._read_pidfile()["token"] == "tok"
