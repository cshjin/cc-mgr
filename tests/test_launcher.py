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


def _stand_in_server(tmp_path: Path) -> Path:
    """A script really named cc-mgr.py that just sleeps (run it with `run`)."""
    script = tmp_path / "bin" / "cc-mgr.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    return script


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc")
def test_stop_terminates_a_real_cc_mgr_process(launcher, tmp_path):
    # Same command line as a server (`python …/cc-mgr.py run …`), in its own
    # session like --background.
    proc = subprocess.Popen([sys.executable, str(_stand_in_server(tmp_path)), "run"],
                            start_new_session=True)
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
def test_identity_needs_the_server_argv_and_the_recorded_start(launcher, tmp_path):
    script, sleep = str(_stand_in_server(tmp_path)), "import time; time.sleep(60)"
    argvs = {
        "mentions the file": [sys.executable, "-c", sleep, script],
        "python -c … cc-mgr.py run": [sys.executable, "-c", sleep, "cc-mgr.py", "run"],
        "not the run command": [sys.executable, script],
        "server": [sys.executable, "-u", script, "run", "--port", "1"],
    }
    procs = {name: subprocess.Popen(argv) for name, argv in argvs.items()}
    try:
        assert {name: launcher._is_cc_mgr_process(p.pid) for name, p in procs.items()} == {
            "mentions the file": False, "python -c … cc-mgr.py run": False,
            "not the run command": False, "server": True}
        pid = procs["server"].pid
        start = launcher._proc_start(pid)
        assert launcher._is_cc_mgr_process(pid, start)
        assert not launcher._is_cc_mgr_process(pid, start + 1)  # PID reused since
    finally:
        for p in procs.values():
            p.kill()
            p.wait()


@pytest.mark.parametrize("argv, expected", [
    (["/usr/bin/python3", "/repo/cc-mgr.py", "run"], True),
    (["python3.12", "-u", "-X", "utf8", "-Wignore", "-OO", "cc-mgr.py", "run", "--port", "1"], True),
    (["C:\\Program Files\\Python312\\python.exe", "G:\\My Drive\\cc_mgr\\cc-mgr.py", "run"], True),
    (["pythonw.exe", "C:\\cc\\cc-mgr.py", "run"], True),
    (["python", "cc-mgr.py", "stop"], False),
    (["python", "cc-mgr.py"], False),
    (["python", "-c", "pass", "cc-mgr.py", "run"], False),
    (["python", "-uc", "pass", "cc-mgr.py", "run"], False),   # -c inside a flag group
    (["python", "-m", "cc-mgr.py", "run"], False),
    (["python", "-X", "cc-mgr.py", "run"], False),            # cc-mgr.py is -X's value
    (["python", "-", "cc-mgr.py", "run"], False),             # script read from stdin
    (["vim", "cc-mgr.py", "run"], False),
    (["notepad.exe", "C:\\cc\\cc-mgr.py", "run"], False),
    ([], False),
])
def test_server_argv_is_parsed_like_the_interpreter_does(launcher, argv, expected):
    assert launcher._looks_like_server(argv) is expected


TICKS = "638670000000000000"


@pytest.mark.parametrize("cmdline, start, expected", [
    ('"C:\\Program Files\\Python312\\python.exe" "G:\\My Drive\\cc_mgr\\cc-mgr.py" run --port 8765',
     TICKS, True),
    ("C:\\Python\\python.exe C:\\cc\\cc-mgr.py run", "638670000000000001", False),  # PID reused
    ("notepad.exe C:\\cc\\cc-mgr.py run", TICKS, False),          # merely mentions the file
    ('"C:\\Python\\python.exe" -c "import time" C:\\cc\\cc-mgr.py run', TICKS, False),
    ('"C:\\Python\\python.exe C:\\cc\\cc-mgr.py run', TICKS, False),  # unbalanced quotes
    ("", TICKS, False),                                           # couldn't be read
])
def test_windows_identity_is_verified_not_assumed(launcher, monkeypatch, cmdline, start, expected):
    monkeypatch.setattr(launcher, "IS_WINDOWS", True)
    monkeypatch.setattr(launcher, "_has_proc", lambda: False)
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: True)

    class Out:  # what the CIM query prints: creation ticks, then the command line
        stdout = f"{TICKS}\r\n{cmdline}\r\n" if cmdline else ""

    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: Out())
    assert launcher._is_cc_mgr_process(4242, start=start) is expected
    assert launcher._proc_start(4242) == (TICKS if cmdline else None)


def test_ps_identity_without_proc(launcher, monkeypatch):
    # macOS / BSD: start time from `ps -o lstart=`, command line from `ps -o command=`
    monkeypatch.setattr(launcher, "IS_WINDOWS", False)
    monkeypatch.setattr(launcher, "_has_proc", lambda: False)
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: True)
    out = {"lstart=": "Fri Nov 13 10:00:00 2026\n",
           "command=": "/usr/local/bin/python3 /Users/me/cc_mgr/cc-mgr.py run --port 8765\n"}
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda cmd, **k: type("Out", (), {"stdout": out[cmd[2]]})())
    start = launcher._proc_start(4242)
    assert start == "Fri Nov 13 10:00:00 2026"
    assert launcher._is_cc_mgr_process(4242, start)
    assert not launcher._is_cc_mgr_process(4242, "Thu Nov 12 09:00:00 2026")  # PID reused
    out["command="] = "/usr/bin/vim /Users/me/cc_mgr/cc-mgr.py run\n"
    assert not launcher._is_cc_mgr_process(4242, start)


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


def test_secure_state_tightens_what_an_older_version_left_open(launcher, tmp_path, monkeypatch):
    import stat
    exports = tmp_path / "exports"
    monkeypatch.setenv("CC_MGR_EXPORT_DIR", str(exports))
    launcher.DATA_DIR.mkdir(parents=True)
    (exports / "proj").mkdir(parents=True)
    files = [launcher.DATA_DIR / "cc_mgr.db", launcher.DATA_DIR / "cc_mgr.log",
             exports / "proj" / "s.20260101T000000Z.md"]
    for f in files:
        f.write_text("private")
        f.chmod(0o644)
    for d in (launcher.DATA_DIR, exports, exports / "proj"):
        d.chmod(0o755)
    launcher._secure_state()
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)
    assert all(stat.S_IMODE(d.stat().st_mode) == 0o700
               for d in (launcher.DATA_DIR, exports, exports / "proj"))
