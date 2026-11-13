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
