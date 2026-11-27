"""Stop isolated test gateways through their own control socket (``pause-for-update``, force).

Usage: stop_gateway.py HERMES_HOME [HERMES_HOME ...]
Each home must sit under the e2e base (``PT_E2E_ROOT``, default ``~/.cache/profile-tasks-e2e`` of the
real user); the real ``~/.hermes`` is refused. The gateway answers with its pid, drains and exits;
this script waits for that pid to disappear and prints one line per home. Exit 1 if one is still alive.
Then any process still running with that ``HERMES_HOME`` (a gateway the socket missed or that outlived
its run) is terminated, and killed if it does not exit.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def real_home() -> Path:
    """The user's real home: from the password database, or ``PT_REAL_HOME`` where there is none (Windows)."""
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except ImportError:
        return Path(os.environ.get("PT_REAL_HOME") or Path.home())


def real_hermes_homes() -> list[Path]:
    """The live Hermes homes no run may touch: ``~/.hermes``, and on Windows the install under %LOCALAPPDATA%."""
    homes = [real_home() / ".hermes"]
    if os.environ.get("PT_REAL_HERMES"):
        homes.append(Path(os.environ["PT_REAL_HERMES"]))
    return [home.resolve() for home in homes]


def is_real_home(home: Path) -> bool:
    """Whether *home* is, or lies inside, a live Hermes home."""
    home = home.resolve()
    return any(home == real or real in home.parents for real in real_hermes_homes())


REAL_HOME = real_home()
ROOT = Path(os.environ.get("PT_E2E_ROOT") or REAL_HOME / ".cache/profile-tasks-e2e").resolve()


def alive(pid: int) -> bool:
    import psutil  # a Hermes dependency; reads the process on Linux and macOS
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        # The gateway exited, possibly while it was being read.
        return False


def stop(home: Path, wait: float = 90.0) -> bool:
    from gateway.control_socket import identify_gateway, pause_gateway_for_update

    home = home.resolve()
    if is_real_home(home) or ROOT not in home.parents:
        print(f"REFUSED {home}: not an isolated e2e home under {ROOT}")
        return False
    ident = identify_gateway(home, timeout=5.0)
    if not ident:
        print(f"NONE {home}: no gateway answers on its control socket")
        return True
    pid = int(ident.get("pid") or 0)
    ack = pause_gateway_for_update(home, timeout=10.0, force=True)
    print(f"STOPPING {home}: pid={pid} ack={ack}")
    deadline = time.monotonic() + wait
    while pid and alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    gone = not (pid and alive(pid))
    print(f"{'STOPPED' if gone else 'STILL-ALIVE'} {home}: pid={pid}")
    return gone


def reap(home: Path, wait: float = 10.0) -> int:
    """Terminate, then kill, every other process whose HERMES_HOME is ``home``; returns how many."""
    import psutil

    home = home.resolve()
    # Not this process nor the ones that started it: on Windows `env -i` stays alive as its parent,
    # with the run's HERMES_HOME.
    mine = {os.getpid(), *(parent.pid for parent in psutil.Process().parents())}
    left = []
    for proc in psutil.process_iter():
        try:
            value = proc.environ().get("HERMES_HOME")
        except (psutil.Error, OSError):
            continue
        if proc.pid not in mine and value and Path(value).resolve() == home:
            left.append(proc)
    for proc in left:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, still = psutil.wait_procs(left, timeout=wait)
    for proc in still:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    if left:
        print(f"REAPED {home}: pids={[proc.pid for proc in left]}")
    return len(left)


def main(argv: list[str]) -> int:
    ok = True
    for raw in argv:
        ok = stop(Path(raw)) and ok
        home = Path(raw).resolve()
        if ROOT in home.parents and not is_real_home(home):
            reap(home)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
