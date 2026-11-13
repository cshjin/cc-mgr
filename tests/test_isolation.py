"""The suite must never touch real agent data or run real agent CLIs."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.agents import all_adapters


def test_adapters_see_no_real_data_without_fixtures():
    for adapter in all_adapters():
        assert adapter.list_projects() == [], adapter.capabilities.agent_id


def test_agent_clis_are_blocked():
    assert shutil.which("opencode") is None
    with pytest.raises(AssertionError):
        subprocess.run(["opencode", "--version"])


def test_every_home_lookup_lands_in_the_sandbox():
    # Windows resolves ~ from USERPROFILE, so HOME alone isn't enough.
    home = Path(os.environ["HOME"])
    assert Path.home() == home and os.environ["USERPROFILE"] == str(home)
    for var in ("CLAUDE_HOME", "AGY_HOME", "GEMINI_HOME", "CODEX_HOME", "COPILOT_HOME", "OPENCODE_DB"):
        assert Path(os.environ[var]).parent == home.parent, var
