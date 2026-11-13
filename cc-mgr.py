#!/usr/bin/env python3
"""cc_mgr — single-file launcher for the local Claude project viewer.

Usage:
    python cc-mgr.py run [--host H] [--port P] [--reload] [--background]
                         [--read-only] [--no-token]
    python cc-mgr.py stop [--port P]
    python cc-mgr.py status [--port P]

`run` serves the app (foreground; Ctrl+C to stop). Add --background to detach
the server and return to the shell. `stop` terminates a server previously
started by this script. Cross-platform (Windows + Linux).

Every start generates a random access token (or uses $CC_MGR_TOKEN) and prints
http://localhost:PORT/?token=… — the API refuses requests without it, so other
users on a shared machine (loopback is shared by everyone on the host) can't
read or change your data.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# cc_mgr's own state (pidfile, log, search index); $CC_MGR_DATA_DIR relocates it,
# e.g. off a slow network filesystem.
DATA_DIR = Path(os.environ.get("CC_MGR_DATA_DIR") or HERE / "data").expanduser()
LEGACY_PID_NAME = "cc_mgr.pid"  # pre-0.5 pidfile, which didn't record its host
IS_WINDOWS = os.name == "nt"


def _host_tag() -> str:
    """This machine's short hostname, safe to use in a file name."""
    name = socket.gethostname().split(".")[0] or "localhost"
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def _pid_file() -> Path:
    # One pidfile per host: the repo (and data/) often lives on a filesystem
    # every node mounts, and a PID only means something on the host that wrote it.
    return DATA_DIR / f"cc_mgr.{_host_tag()}.pid"


def _log_file() -> Path:
    return DATA_DIR / f"cc_mgr.{_host_tag()}.log"

# Sentinel default for --host: bind both loopback addresses (IPv4 127.0.0.1 and
# IPv6 ::1) so both `localhost` and `127.0.0.1` work regardless of how the OS
# resolves `localhost`, while staying loopback-only (not exposed to the network).
# Note: loopback is still shared by every user on the same machine, hence the
# access token.
LOOPBACK = "loopback"
LOOPBACK_NAMES = (LOOPBACK, "127.0.0.1", "::1", "localhost")


def _display_host(host: str) -> str:
    if host == LOOPBACK:
        return "localhost"
    return f"[{host}]" if ":" in host else host


def _url(host: str, port: int, token: str = "") -> str:
    return f"http://{_display_host(host)}:{port}/" + (f"?token={token}" if token else "")


def _security_env(host: str, token: str, read_only: bool) -> dict[str, str]:
    """Env vars backend/app.py reads for access control."""
    return {
        "CC_MGR_TOKEN": token,
        "CC_MGR_READ_ONLY": "1" if read_only else "",
        # Host-header allowlist (DNS-rebinding guard) only makes sense when the
        # server is reachable solely through loopback names.
        "CC_MGR_ALLOWED_HOSTS": "localhost,127.0.0.1,::1" if host in LOOPBACK_NAMES else "",
    }


# ---------------------------------------------------------------------------
# PID-file helpers
# ---------------------------------------------------------------------------

def _secure_state() -> None:
    """Make cc_mgr's own state private before serving: the data dir (pidfiles,
    logs, the search index — a copy of every conversation) and the exports.
    Fresh files are created that way; this also tightens what an older
    version left world-readable. Shared sticky dirs (e.g. /tmp) are left alone.
    """
    exports = Path(os.environ.get("CC_MGR_EXPORT_DIR") or HERE / "exports").expanduser()
    for root, pattern in ((DATA_DIR, "cc_mgr*"), (exports, "**/*")):
        if not root.is_dir():
            continue
        try:
            st = root.stat()
            if IS_WINDOWS or st.st_uid != os.getuid() or st.st_mode & stat.S_ISVTX:
                continue
            os.chmod(root, 0o700)
            for p in root.glob(pattern):
                if p.is_symlink():
                    continue
                os.chmod(p, 0o700 if p.is_dir() else 0o600)
        except OSError:
            continue


def _write_pidfile(pid: int, host: str, port: int, token: str = "") -> None:
    path = _pid_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The pidfile holds the access token: create it owner-only, never via a symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pid": pid, "host": host, "port": port, "token": token,
                   "hostname": _host_tag(), "start": _proc_start(pid)}, fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_pidfile(path: Path | None = None) -> dict | None:
    path = path or _pid_file()
    if not path.is_file():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return info if isinstance(info, dict) and isinstance(info.get("pid"), int) else None


def _clear_pidfile(path: Path | None = None) -> None:
    try:
        (path or _pid_file()).unlink()
    except FileNotFoundError:
        pass


def _other_pidfiles() -> list[tuple[str, dict]]:
    """Pidfiles this host can't check: other hosts' and the legacy host-less one."""
    out: list[tuple[str, dict]] = []
    if not DATA_DIR.is_dir():
        return out
    mine = _pid_file().name
    for p in sorted(DATA_DIR.glob("cc_mgr*.pid")):
        info = _read_pidfile(p)
        if p.name == mine or not info:
            continue
        host = "an unknown host (old cc_mgr)" if p.name == LEGACY_PID_NAME \
            else p.name[len("cc_mgr."):-len(".pid")]
        out.append((host, info))
    return out


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. Must NOT kill it as a side effect.

    Note: on Windows, os.kill(pid, 0) would call TerminateProcess(exit 0) and
    actually kill the process — so we use tasklist there instead.
    """
    if IS_WINDOWS:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _proc_start(pid: int) -> int | None:
    """Process start time in clock ticks since boot (Linux), else None.

    Recorded in the pidfile: a PID that was reused since has a different start.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        # field 22 (starttime); fields after "(comm)" start at field 3
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _looks_like_server(argv: list[str]) -> bool:
    """argv of `python …/cc-mgr.py run …` (not merely any mention of the file)."""
    return any(a.replace("\\", "/").rsplit("/", 1)[-1] == "cc-mgr.py" and argv[i + 1] == "run"
               for i, a in enumerate(argv[:-1]))


def _has_proc() -> bool:
    return Path("/proc/self/cmdline").exists()


def _is_cc_mgr_process(pid: int, start: int | None = None) -> bool:
    """True only if pid is alive here AND is a cc_mgr server we started.

    A pidfile can outlive its server and the PID can be reused by anything,
    so a bare "is the PID alive" check could make `stop` signal an unrelated
    process. The command line must be `… cc-mgr.py run …` and, when the
    pidfile recorded one, the process start time must match. If identity
    can't be established, the answer is no.
    """
    if not _pid_alive(pid):
        return False
    if _has_proc():  # Linux
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not _looks_like_server(argv):
            return False
        return start is None or _proc_start(pid) == start
    if not IS_WINDOWS:  # macOS / BSD: ps shows the command line
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return _looks_like_server(out.split())
    # Windows: tasklist can't show command lines; ask CIM. Unverifiable -> no.
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return _looks_like_server([a.strip('"') for a in out.split()])


def _terminate(pid: int) -> None:
    """Terminate a process (and its children, e.g. uvicorn --reload workers)."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
        )
        return
    # POSIX: the server is started in its own session, so kill the whole group.
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _port_in_use(port: int) -> bool:
    """True if anything is already listening on either loopback address."""
    for addr in ("127.0.0.1", "::1"):
        try:
            family = socket.AF_INET if ":" not in addr else socket.AF_INET6
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex((addr, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def _find_port_holder(port: int) -> str | None:
    """Best-effort identification of the process holding `port`. Windows only
    (uses `netstat -ano`); returns None on POSIX or if lookup fails."""
    if not IS_WINDOWS:
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    needle = f":{port} "
    for line in out.splitlines():
        if "LISTENING" in line and needle in line:
            parts = line.split()
            pid = parts[-1] if parts else ""
            name = ""
            try:
                ti = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                if ti:
                    name = ti.split(",")[0].strip('"')
            except (OSError, subprocess.SubprocessError):
                pass
            return f"pid {pid}" + (f" ({name})" if name else "")
    return None


def _pick_free_port(start: int, tries: int = 20) -> int | None:
    for p in range(start, start + tries):
        if not _port_in_use(p):
            return p
    return None


def _loopback_sockets(port: int) -> list[socket.socket]:
    """Bind both IPv4 (127.0.0.1) and IPv6 (::1) loopback on `port`.

    Two separate sockets are needed because a single uvicorn host binds only one
    address family; this is what lets `localhost` work whether the OS resolves it
    to 127.0.0.1 or ::1. Stays loopback-only (never exposed to other interfaces).

    NOTE: SO_REUSEADDR is deliberately NOT set on Windows — there its semantics
    are nearly the opposite of POSIX and silently allow two processes to bind
    the same port, with confusing routing of incoming connections.
    """
    socks: list[socket.socket] = []
    specs = [(socket.AF_INET, "127.0.0.1")]
    if socket.has_ipv6:
        specs.append((socket.AF_INET6, "::1"))
    for family, addr in specs:
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            if not IS_WINDOWS:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((addr, port))
            s.listen(128)  # uvicorn expects sockets already listening
            socks.append(s)
        except OSError:
            # IPv6 may be unavailable on this host — skip it, keep IPv4.
            # An IPv4 failure here is fatal; the caller turns it into a
            # friendly error message that names the port holder.
            if family == socket.AF_INET:
                for prev in socks:
                    prev.close()
                raise
    return socks


def _serve(host: str, port: int, reload: bool, auto_port: bool = False,
           no_token: bool = False, read_only: bool = False) -> None:
    """Run uvicorn in the foreground (blocking). Manages the pidfile."""
    # Ensure imports + data paths resolve regardless of the caller's cwd.
    os.chdir(HERE)
    sys.path.insert(0, str(HERE))
    import uvicorn  # lazy: `stop`/`status` don't need it

    # Pre-flight: if the port is already taken, fail (or roll forward) with a
    # message that actually names the holder. Windows otherwise reports the
    # confusing WSAEACCES (10013) "forbidden by access permissions".
    if _port_in_use(port):
        holder = _find_port_holder(port)
        if auto_port:
            alt = _pick_free_port(port + 1)
            if alt is None:
                _die_port_in_use(port, holder, no_alt=True)
            print(f"port {port} is in use" + (f" by {holder}" if holder else "")
                  + f"; using {alt} instead.")
            port = alt
        else:
            _die_port_in_use(port, holder)

    token = "" if no_token else (os.environ.get("CC_MGR_TOKEN") or secrets.token_urlsafe(24))
    os.environ.update(_security_env(host, token, read_only))
    _secure_state()
    if not token:
        exposure = ("anyone who can reach it over the network" if host not in LOOPBACK_NAMES
                    else "every user logged into this machine")
        print(f"WARNING: --no-token: the API is open to {exposure}.", file=sys.stderr)
    elif host not in LOOPBACK_NAMES:
        print("WARNING: serving on a non-loopback address over plain HTTP; the token "
              "travels unencrypted. Prefer the default bind plus `ssh -L`.", file=sys.stderr)

    _write_pidfile(os.getpid(), host, port, token)
    print(_banner(host, port, token, read_only, sys.stdout.isatty()))
    print("Press Ctrl+C to stop." if sys.stdout.isatty() else "", flush=True)
    try:
        # The dual-loopback default needs two listening sockets, which the
        # uvicorn.run() convenience can't express — drive a Server directly.
        # (--reload spawns a subprocess and can't inherit our sockets, so it
        # falls back to a single IPv4 bind.)
        if host == LOOPBACK and not reload:
            config = uvicorn.Config("backend.app:app")
            server = uvicorn.Server(config)
            server.run(sockets=_loopback_sockets(port))
        else:
            bind = "127.0.0.1" if host == LOOPBACK else host
            uvicorn.run("backend.app:app", host=bind, port=port, reload=reload)
    finally:
        _clear_pidfile()


def _banner(host: str, port: int, token: str, read_only: bool, tty: bool) -> str:
    """Startup line. The token stays out of logs: a background server's stdout
    is a log file (`run --background` / `status` print the full URL from the
    0600 pidfile instead)."""
    shown = _url(host, port, token) if tty else (
        _url(host, port) + ("?token=<see `cc-mgr.py status`>" if token else ""))
    return (f"cc_mgr serving on {shown}  (pid {os.getpid()})"
            + ("  [read-only]" if read_only else ""))


def _open_log() -> int:
    """Append-only fd for the background log, owner-only even if an older run
    left a world-readable log behind."""
    log_file = _log_file()
    log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    try:
        os.chmod(log_file, 0o600)
    except OSError:
        pass
    return fd


def _die_port_in_use(port: int, holder: str | None, no_alt: bool = False) -> None:
    msg = [f"Port {port} is already in use"]
    if holder:
        msg.append(f" by {holder}")
    msg.append(".\n")
    if no_alt:
        msg.append("No free port found in the next 20 either.\n")
    msg.append("Either stop the other process, choose a different port with "
               "`--port`, or pass `--auto-port` to use the next free one.")
    print("".join(msg), file=sys.stderr)
    raise SystemExit(2)


def cmd_run(args: argparse.Namespace) -> int:
    existing = _read_pidfile()
    if existing and _is_cc_mgr_process(existing["pid"], existing.get("start")):
        print(f"cc_mgr already running on {_host_tag()} (pid {existing['pid']} on "
              f"{_url(existing['host'], existing['port'], existing.get('token', ''))}). "
              f"Use `stop` first.")
        return 1
    if existing:
        _clear_pidfile()  # stale

    if not args.background:
        _serve(args.host, args.port, args.reload, auto_port=args.auto_port,
               no_token=args.no_token, read_only=args.read_only)
        return 0

    # --- background: spawn a detached copy running in the foreground ---
    cmd = [sys.executable, str(Path(__file__).resolve()), "run",
           "--host", args.host, "--port", str(args.port)]
    if args.reload:
        cmd.append("--reload")
    if args.auto_port:
        cmd.append("--auto-port")
    if args.no_token:
        cmd.append("--no-token")
    if args.read_only:
        cmd.append("--read-only")
    log_file = _log_file()
    with os.fdopen(_open_log(), "ab") as logf:  # the child keeps its own copy of the fd
        if IS_WINDOWS:
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(cmd, stdout=logf, stderr=logf,
                             stdin=subprocess.DEVNULL, creationflags=flags,
                             close_fds=True, cwd=str(HERE))
        else:
            subprocess.Popen(cmd, stdout=logf, stderr=logf,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True, cwd=str(HERE))
    # The child writes its own pidfile at startup; give it a moment, then verify.
    time.sleep(1.5)
    info = _read_pidfile()
    if info and _is_cc_mgr_process(info["pid"], info.get("start")):
        print(f"cc_mgr started in background (pid {info['pid']}) on "
              f"{_url(info['host'], info['port'], info.get('token', ''))}")
        print(f"Logs: {log_file}")
        print("Stop it with:  python cc-mgr.py stop")
        return 0
    print(f"Failed to start in background. Check {log_file}.")
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    host = _host_tag()
    path = _pid_file()
    info = _read_pidfile(path)
    if not info:
        # An older cc_mgr wrote a host-less pidfile; only trust it if its PID
        # is provably a cc_mgr process on *this* host.
        legacy = DATA_DIR / LEGACY_PID_NAME
        linfo = _read_pidfile(legacy)
        if linfo and _is_cc_mgr_process(linfo["pid"], linfo.get("start")):
            info, path = linfo, legacy
        else:
            print(f"No cc_mgr pidfile for this host ({host}) — nothing to stop "
                  "(was it started by this script?).")
            for other, oinfo in _other_pidfiles():
                print(f"  A pidfile from {other} lists pid {oinfo['pid']}; "
                      "run `stop` on that machine.")
            return 1
    pid, start = info["pid"], info.get("start")
    if not _is_cc_mgr_process(pid, start):
        print(f"cc_mgr not running on {host} (stale pidfile for pid {pid}); cleaning up.")
        _clear_pidfile(path)
        return 0
    print(f"Stopping cc_mgr (pid {pid})…")
    _terminate(pid)
    # wait briefly for it to die
    for _ in range(20):
        if not _is_cc_mgr_process(pid, start):
            break
        time.sleep(0.1)
    if _is_cc_mgr_process(pid, start):
        print(f"Process {pid} did not exit; you may need to kill it manually.")
        return 1
    _clear_pidfile(path)
    print("Stopped.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    host = _host_tag()
    info = _read_pidfile()
    if info and _is_cc_mgr_process(info["pid"], info.get("start")):
        print(f"cc_mgr running on {host} (pid {info['pid']}) on "
              f"{_url(info['host'], info['port'], info.get('token', ''))}")
        rc = 0
    else:
        print(f"cc_mgr not running on {host}" + (" (stale pidfile present)." if info else "."))
        rc = 1
    for other, oinfo in _other_pidfiles():
        print(f"  pidfile from {other}: pid {oinfo['pid']}, port {oinfo.get('port')} "
              "(not checked from this host)")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="cc-mgr.py", description="cc_mgr — local Claude project viewer")
    sub = ap.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="start the server")
    p_run.add_argument(
        "--host", default=LOOPBACK,
        help="bind address; default binds both 127.0.0.1 and ::1 so localhost "
             "and 127.0.0.1 both work. Pass 0.0.0.0 to expose on the network.")
    p_run.add_argument("--port", type=int, default=8765,
                       help="port to serve on (default 8765)")
    p_run.add_argument("--reload", action="store_true", help="dev autoreload")
    p_run.add_argument("--background", "-b", action="store_true",
                       help="detach and run in the background")
    p_run.add_argument("--auto-port", action="store_true",
                       help="if --port is taken, try the next free one")
    p_run.add_argument("--read-only", action="store_true",
                       help="disable every endpoint that writes or deletes data")
    p_run.add_argument("--no-token", action="store_true",
                       help="serve the API without an access token (unsafe on "
                            "shared machines: loopback is shared by all users)")
    p_run.set_defaults(func=cmd_run)

    p_stop = sub.add_parser("stop", help="stop a server started by this script")
    p_stop.add_argument("--port", type=int, default=8765,
                        help="(informational; pidfile is authoritative)")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="show whether the server is running")
    p_status.add_argument("--port", type=int, default=8765)
    p_status.set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
