#!/usr/bin/env python3
"""cc_mgr — single-file launcher for the local Claude project viewer.

Usage:
    python cc-mgr.py run [--host H] [--port P] [--reload] [--background]
    python cc-mgr.py stop [--port P]
    python cc-mgr.py status [--port P]

`run` serves the app (foreground; Ctrl+C to stop). Add --background to detach
the server and return to the shell. `stop` terminates a server previously
started by this script. Cross-platform (Windows + Linux).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PID_FILE = HERE / "data" / "cc_mgr.pid"
LOG_FILE = HERE / "data" / "cc_mgr.log"
IS_WINDOWS = os.name == "nt"

# Sentinel default for --host: bind both loopback addresses (IPv4 127.0.0.1 and
# IPv6 ::1) so both `localhost` and `127.0.0.1` work regardless of how the OS
# resolves `localhost`, while staying loopback-only (not exposed to the network).
LOOPBACK = "loopback"


def _display_host(host: str) -> str:
    return "localhost" if host == LOOPBACK else host


# ---------------------------------------------------------------------------
# PID-file helpers
# ---------------------------------------------------------------------------

def _write_pidfile(pid: int, host: str, port: int) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(
        json.dumps({"pid": pid, "host": host, "port": port}), encoding="utf-8"
    )


def _read_pidfile() -> dict | None:
    if not PID_FILE.is_file():
        return None
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _clear_pidfile() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


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
        except ProcessLookupError:
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


def _serve(host: str, port: int, reload: bool, auto_port: bool = False) -> None:
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

    _write_pidfile(os.getpid(), host, port)
    print(f"cc_mgr serving on http://{_display_host(host)}:{port}  "
          f"(pid {os.getpid()})")
    print("Press Ctrl+C to stop." if sys.stdout.isatty() else "")
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
    if existing and _pid_alive(existing["pid"]):
        print(f"cc_mgr already running (pid {existing['pid']} on "
              f"http://{_display_host(existing['host'])}:{existing['port']}). "
              f"Use `stop` first.")
        return 1
    if existing:
        _clear_pidfile()  # stale

    if not args.background:
        _serve(args.host, args.port, args.reload, auto_port=args.auto_port)
        return 0

    # --- background: spawn a detached copy running in the foreground ---
    cmd = [sys.executable, str(Path(__file__).resolve()), "run",
           "--host", args.host, "--port", str(args.port)]
    if args.reload:
        cmd.append("--reload")
    if args.auto_port:
        cmd.append("--auto-port")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_FILE, "ab")
    if IS_WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, stdout=logf, stderr=logf,
                                stdin=subprocess.DEVNULL, creationflags=flags,
                                close_fds=True, cwd=str(HERE))
    else:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=logf,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                close_fds=True, cwd=str(HERE))
    # The child writes its own pidfile at startup; give it a moment, then verify.
    time.sleep(1.5)
    info = _read_pidfile()
    if info and _pid_alive(info["pid"]):
        print(f"cc_mgr started in background (pid {info['pid']}) on "
              f"http://{_display_host(args.host)}:{args.port}")
        print(f"Logs: {LOG_FILE}")
        print("Stop it with:  python cc-mgr.py stop")
        return 0
    print(f"Failed to start in background. Check {LOG_FILE}.")
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    info = _read_pidfile()
    if not info:
        print("No cc_mgr pidfile found — nothing to stop "
              "(was it started by this script?).")
        return 1
    pid = info["pid"]
    if not _pid_alive(pid):
        print(f"cc_mgr not running (stale pidfile for pid {pid}); cleaning up.")
        _clear_pidfile()
        return 0
    print(f"Stopping cc_mgr (pid {pid})…")
    _terminate(pid)
    # wait briefly for it to die
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    if _pid_alive(pid):
        print(f"Process {pid} did not exit; you may need to kill it manually.")
        return 1
    _clear_pidfile()
    print("Stopped.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    info = _read_pidfile()
    if info and _pid_alive(info["pid"]):
        print(f"cc_mgr running (pid {info['pid']}) on "
              f"http://{_display_host(info['host'])}:{info['port']}")
        return 0
    if info:
        print("cc_mgr not running (stale pidfile present).")
    else:
        print("cc_mgr not running.")
    return 1


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
