"""Pure frontend helpers from frontend/app.js, run under node (skipped without node)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "frontend" / "app.js"


def run_js(functions, script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    src = APP_JS.read_text(encoding="utf-8")
    parts = []
    for name in functions:
        m = re.search(rf"^function {name}\(.*?^\}}", src, re.S | re.M)
        assert m, f"{name} not found in app.js"
        parts.append(m.group(0))
    out = subprocess.run([node, "-e", "\n".join(parts) + "\n" + script],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_session_tree_hides_and_nests_subagents():
    sessions = [
        {"session_id": "p1"}, {"session_id": "c1", "parent_id": "p1"},
        {"session_id": "g1", "parent_id": "c1"}, {"session_id": "p2", "parent_id": ""},
        {"session_id": "orphan", "parent_id": "gone"},
    ]
    r = run_js(["sessionTree"], f"""
        const ss = {json.dumps(sessions)};
        const hide = sessionTree(ss, false), show = sessionTree(ss, true);
        process.stdout.write(JSON.stringify({{
          hidden: hide.visible.map(v => v.s.session_id),
          shown: show.visible.map(v => [v.s.session_id, v.depth]),
          tops: hide.topCount, kids: hide.childTotal, p1kids: (hide.kids.p1 || []).length,
        }}));""")
    assert r["hidden"] == ["p1", "p2", "orphan"]  # an orphan (parent not listed) stays reachable
    assert r["shown"] == [["p1", 0], ["c1", 1], ["g1", 2], ["p2", 0], ["orphan", 0]]
    assert (r["tops"], r["kids"], r["p1kids"]) == (3, 2, 1)


def test_order_agents_puts_agents_with_data_first():
    agents = [{"agent_id": "claude", "has_data": True}, {"agent_id": "agy", "has_data": False},
              {"agent_id": "opencode", "has_data": True}, {"agent_id": "gemini", "has_data": False},
              {"agent_id": "legacy-fallback"}]
    r = run_js(["orderAgents"], f"""
        process.stdout.write(JSON.stringify(orderAgents({json.dumps(agents)}).map(a => a.agent_id)));""")
    assert r == ["claude", "opencode", "legacy-fallback", "agy", "gemini"]


KANBAN_HARNESS = """
const state = { agent: "claude" };
const location = { origin: "http://localhost" };
const calls = [];
async function fetch(u, opts) { calls.push([String(u), opts.method, opts.body]); return { ok: true }; }
async function httpError(r) { return new Error("http"); }
function flash(msg) { calls.push(["flash", msg]); }
function reloadTasksView() {}
function el(data, parent) {
  return { dataset: data, parentElement: parent || null, listeners: {},
           classList: { add() {}, remove() {} },
           addEventListener(t, f) { this.listeners[t] = f; },
           appendChild(c) { c.parentElement = this; } };
}
"""


def test_project_board_moves_the_dragged_card_even_when_task_ids_repeat():
    # Session A's task "1" is already completed; session B's task "1" is pending.
    # The old drop handler re-found the card by data-id, got A's card (already in
    # the target column), and silently skipped the PATCH.
    r = run_js(["wireKanban"], KANBAN_HARNESS + """
        const pending = el({ status: "pending" }), done = el({ status: "completed" });
        const cardA = el({ id: "1", sid: "A" }, done), cardB = el({ id: "1", sid: "B" }, pending);
        const scope = {
          querySelectorAll(sel) { return sel === ".kcard" ? [cardA, cardB] : [pending, done]; },
          querySelector() { return cardA; },
        };
        wireKanban(scope, { perCard: true });
        const ev = { preventDefault() {}, dataTransfer: {} };
        cardB.listeners.dragstart(ev);
        (async () => {
          await done.listeners.drop(ev);
          process.stdout.write(JSON.stringify({ moved: cardB.parentElement === done,
                                                untouched: cardA.parentElement === done, calls }));
        })();""")
    assert r["moved"] and r["untouched"]
    assert r["calls"] == [["http://localhost/api/sessions/B/tasks/1?agent=claude", "PATCH",
                           '{"status":"completed"}']]


def test_visible_projects_filters_missing_directories_and_sorts():
    projects = [{"name": "b", "mtime": 1, "cwd_missing": True}, {"name": "a", "mtime": 3},
                {"name": "c", "mtime": 2, "cwd_missing": True}]
    r = run_js(["visibleProjects"], f"""
        const ps = {json.dumps(projects)};
        process.stdout.write(JSON.stringify({{
          time: visibleProjects(ps, "time", false).map(p => p.name),
          name: visibleProjects(ps, "name", false).map(p => p.name),
          missing: visibleProjects(ps, "time", true).map(p => p.name),
          untouched: ps.map(p => p.name) }}));""")
    assert r == {"time": ["a", "c", "b"], "name": ["a", "b", "c"], "missing": ["c", "b"],
                 "untouched": ["b", "a", "c"]}


def test_project_delete_dialog_texts():
    r = run_js(["plural", "projectDeleteSummary", "confirmWord", "projectDeleteResultText"], r"""
        process.stdout.write(JSON.stringify({
          summary: projectDeleteSummary({sessions: 3, subagents: 1, hidden: 2, memory_files: 1}),
          one: projectDeleteSummary({sessions: 1, subagents: 0, hidden: 0, memory_files: 0}),
          words: [confirmWord({display_path: "/global/x/SwarmZoo_paper/", name: "n"}),
                  confirmWord({display_path: "C:\\work\\repo", name: "n"}),
                  confirmWord({display_path: "", name: "-tmp"})],
          soft: projectDeleteResultText({sessions: ["a", "b"], trash: ["/t/x"], kept: ["memory"],
                                         errors: {"cccccccc-1111": "busy"}}, false),
          archived: projectDeleteResultText({sessions: ["a"], trash: []}, false),
          hard: projectDeleteResultText({sessions: ["a"], trash: [], exported: ["/e/a.md"]}, true),
        }));""")
    assert r["summary"] == ("3 sessions (+1 subagent session) · 2 not listed (empty or archived) "
                            "· 1 memory file")
    assert r["one"] == "1 session"
    assert r["words"] == ["SwarmZoo_paper", "repo", "-tmp"]
    assert r["soft"] == ("Removed 2 sessions (reversible: /t/x). The project memory was kept. "
                         "Not deleted (1): cccccccc: busy")
    assert r["archived"] == "Removed 1 session (reversible)."
    assert r["hard"] == "Permanently deleted 1 session. Exported 1 to Markdown first."


def test_follow_project_after_the_list_reloads():
    projects = [{"name": "-w-repo-x", "cwd": "/w/repo/x"}, {"name": "-w-a", "cwd": "/w/a"},
                {"name": "-mem-only", "cwd": ""}]
    r = run_js(["followProject"], f"""
        const ps = {json.dumps(projects)};
        const name = (p) => p ? p.name : null;
        process.stdout.write(JSON.stringify({{
          same: name(followProject(ps, "-w-a", "/w/a")),
          renamed: name(followProject(ps, "-w-repo-x~0123456789ab", "/w/repo/x")),
          gone: name(followProject(ps, "-w-old", "/w/old")),
          noCwd: name(followProject(ps, "-mem-only", "")),
          cwdChanged: name(followProject(ps, "-w-a", "/w/a-before")) }}));""")
    assert r == {"same": "-w-a",
                 "renamed": "-w-repo-x",    # the name changed: follow the directory
                 "gone": None,              # deselect
                 "noCwd": "-mem-only",
                 "cwdChanged": "-w-a"}      # e.g. a Claude folder whose newest session moved
