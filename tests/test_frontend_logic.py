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
