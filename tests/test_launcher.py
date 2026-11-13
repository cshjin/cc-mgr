"""#19: per-host pidfiles; never signal a process that isn't this host's cc_mgr."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ARGS = SimpleNamespace()


def test_pidfile_is_per_host(launcher):
    launcher._write_pidfile(1, "loopback", 8765)
    assert launcher._pid_file().name == f"cc_mgr.{launcher._host_tag()}.pid"
    assert launcher._read_pidfile()["hostname"] == launcher._host_tag()
    assert launcher._log_file().name == f"cc_mgr.{launcher._host_tag()}.log"


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")
def test_a_reused_pid_is_never_signalled(launcher, monkeypatch, capsys):
    # The pidfile points at a live process that is not cc_mgr: this test run.
    launcher._write_pidfile(os.getpid(), "loopback", 8765)
    killed = []
    monkeypatch.setattr(launcher, "_terminate", killed.append)
    assert launcher.cmd_status(ARGS) == 1
    assert launcher.cmd_stop(ARGS) == 0
    assert killed == [] and launcher._read_pidfile() is None
    assert "stale pidfile" in capsys.readouterr().out


def test_other_hosts_pidfiles_are_left_alone(launcher, monkeypatch, capsys):
    launcher.DATA_DIR.mkdir(parents=True, exist_ok=True)
    other = launcher.DATA_DIR / "cc_mgr.othernode.pid"
    other.write_text(json.dumps({"pid": os.getpid(), "host": "loopback", "port": 8765,
                                 "hostname": "othernode"}))
    legacy = launcher.DATA_DIR / "cc_mgr.pid"  # pre-0.5: no host recorded
    legacy.write_text(json.dumps({"pid": 594419, "host": "loopback", "port": 8765}))
    killed = []
    monkeypatch.setattr(launcher, "_terminate", killed.append)
    monkeypatch.setattr(launcher, "_is_cc_mgr_process", lambda pid, start=None: False)
    assert launcher.cmd_stop(ARGS) == 1
    assert launcher.cmd_status(ARGS) == 1
    out = capsys.readouterr().out
    assert "othernode" in out and "unknown host" in out
    assert killed == [] and other.exists() and legacy.exists()


def test_terminate_survives_permission_errors(launcher, monkeypatch):
    def deny(*_args):
        raise PermissionError

    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(launcher.os, "killpg", deny)
    monkeypatch.setattr(launcher.os, "kill", deny)
    launcher._terminate(12345)  # used to raise from the os.kill fallback


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")
def test_stop_terminates_a_real_cc_mgr_process(launcher):
    # Stand-in with a server's command line (`… cc-mgr.py run …`), in its own
    # session like --background.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                             "cc-mgr.py", "run"], start_new_session=True)
    try:
        launcher._write_pidfile(proc.pid, "loopback", 8765)
        assert launcher.cmd_status(ARGS) == 0
        assert launcher.cmd_stop(ARGS) == 0
        assert proc.wait(timeout=10) == -signal.SIGTERM
        assert launcher._read_pidfile() is None
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")
def test_identity_needs_the_server_argv_and_the_recorded_start(launcher):
    mention = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                "/somewhere/cc-mgr.py"])  # e.g. an editor on the file
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                               "cc-mgr.py", "run"])
    try:
        assert not launcher._is_cc_mgr_process(mention.pid)
        assert launcher._is_cc_mgr_process(server.pid)
        start = launcher._proc_start(server.pid)
        assert launcher._is_cc_mgr_process(server.pid, start)
        assert not launcher._is_cc_mgr_process(server.pid, start + 1)  # PID reused since
    finally:
        for p in (mention, server):
            p.kill()
            p.wait()


def test_background_log_never_contains_the_token(launcher):
    line = launcher._banner("loopback", 8765, "s3cret-token", read_only=False, tty=False)
    assert "s3cret-token" not in line and "cc-mgr.py status" in line
    assert "s3cret-token" in launcher._banner("loopback", 8765, "s3cret-token", False, tty=True)


def test_log_is_owner_only_even_if_it_existed(launcher):
    import stat
    launcher.DATA_DIR.mkdir(parents=True)
    log = launcher._log_file()
    log.write_text("old\n")
    log.chmod(0o644)
    os.close(launcher._open_log())
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
