from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_agents_endpoint_lists_four(claude_home):
    r = client.get("/api/agents")
    assert r.status_code == 200
    ids = {a["agent_id"] for a in r.json()}
    assert ids == {"claude", "gemini", "codex", "copilot"}


def test_projects_default_claude(claude_home):
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_projects_gemini(gemini_home, claude_home):
    r = client.get("/api/projects", params={"agent": "gemini"})
    assert r.status_code == 200
    assert r.json()[0]["name"] == "repo_gemini"


def test_doc_get_and_put_gemini(gemini_home, claude_home):
    pname = client.get("/api/projects", params={"agent": "gemini"}).json()[0]["name"]
    g = client.get(f"/api/projects/{pname}/doc", params={"agent": "gemini"})
    assert g.status_code == 200 and "beta" in g.json()["content"]
    p = client.put(f"/api/projects/{pname}/doc", params={"agent": "gemini"},
                   json={"content": "# g\nsigma\n"})
    assert p.status_code == 200
    assert "sigma" in client.get(f"/api/projects/{pname}/doc",
                                 params={"agent": "gemini"}).json()["content"]


def test_unsupported_tasks_on_gemini_404(gemini_home):
    pname = client.get("/api/projects", params={"agent": "gemini"}).json()[0]["name"]
    r = client.get(f"/api/projects/{pname}/tasks", params={"agent": "gemini"})
    assert r.status_code == 404
