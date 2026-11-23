"""Stop isolated test gateways through their own control socket (``pause-for-update``, force).

Usage: stop_gateway.py HERMES_HOME [HERMES_HOME ...]
Each home must sit under the e2e base (``PT_E2E_ROOT``, default ``~/.cache/profile-tasks-e2e`` of the
real user); the real ``~/.hermes`` is refused. The gateway answers with its pid, drains and exits;
this script waits for that pid to disappear and prints one line per home. Exit 1 if one is still alive.
"""

from __future__ import annotations

import os
import pwd
import sys
import time
from pathlib import Path

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
ROOT = Path(os.environ.get("PT_E2E_ROOT") or REAL_HOME / ".cache/profile-tasks-e2e").resolve()
REAL_HERMES = (REAL_HOME / ".hermes").resolve()


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
    if home == REAL_HERMES or REAL_HERMES in home.parents or ROOT not in home.parents:
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


def main(argv: list[str]) -> int:
    ok = True
    for raw in argv:
        ok = stop(Path(raw)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
