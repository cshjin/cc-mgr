from backend import store


def test_store_still_exposes_claude_home_and_dirs(claude_home):
    assert store.claude_home() == claude_home
    assert store.projects_dir() == claude_home / "projects"
    assert store.tasks_dir() == claude_home / "tasks"


def test_store_list_projects_unchanged(claude_home):
    projs = store.list_projects()
    assert len(projs) == 1
    assert projs[0]["has_claude_md"] is True
