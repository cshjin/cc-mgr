"""The suite must never touch real agent data or run real agent CLIs."""
import shutil
import subprocess

import pytest

from backend.agents import all_adapters


def test_adapters_see_no_real_data_without_fixtures():
    for adapter in all_adapters():
        assert adapter.list_projects() == [], adapter.capabilities.agent_id


def test_agent_clis_are_blocked():
    assert shutil.which("opencode") is None
    with pytest.raises(AssertionError):
        subprocess.run(["opencode", "--version"])
