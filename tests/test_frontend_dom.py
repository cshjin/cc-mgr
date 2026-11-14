"""The project-delete dialog in a real DOM (jsdom), when jsdom is available.

jsdom isn't a dependency: install it anywhere and point NODE_PATH at it, e.g.
    npm install --prefix /tmp/jsdomenv jsdom
    NODE_PATH=/tmp/jsdomenv/node_modules python -m pytest tests/test_frontend_dom.py
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_project_delete_dialog_in_a_dom():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    if subprocess.run([node, "-e", "require.resolve('jsdom')"], capture_output=True).returncode:
        pytest.skip("jsdom is not installed (npm install jsdom; NODE_PATH=<its node_modules>)")
    proc = subprocess.run([node, str(ROOT / "tests" / "js" / "project_delete_dialog.js"),
                           str(ROOT / "frontend")], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and "ALL UI CHECKS PASSED" in proc.stdout, proc.stdout + proc.stderr
