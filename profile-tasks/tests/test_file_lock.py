"""The lock on ``tasks.lock`` between processes (flock on Linux and macOS, a locked byte on Windows):

- a second process waits while the first holds the lock, then takes it when the block ends;
- a process that is killed while it holds the lock releases it;
- a reader's lock (``shared=True``) waits for a writer; two readers do not wait for each other where the
  platform has shared locks (everywhere but Windows, where a reader takes the exclusive lock);
- several processes changing one task through ``TaskStore.modify`` lose no update, and the same run with the
  lock turned off does lose some (so the check does see what the lock is for);
- the lock file is not left locked once its holder closed it.

Run: ``PT_SCRIPT=tests/test_file_lock.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes
interpreter). ``e2e/run.sh <path to __init__.py>`` with that script runs it against another copy of the plugin.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The worker processes read the plugin path from PT_PLUGIN too.
PLUGIN = Path(os.environ.get("PT_PLUGIN") or Path(__file__).resolve().parents[1] / "__init__.py")
WORKERS, ROUNDS = 4, 40


def load():
    spec = importlib.util.spec_from_file_location("profile_tasks_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FileState:
    """The plugin state's surface TaskStore uses, kept in a JSON file the way PluginState keeps it."""

    def __init__(self, folder: str) -> None:
        self.data_dir = Path(folder)
        self.path = self.data_dir / "state.json"

    def get(self, key, default=None):
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get(key, default)
        except (FileNotFoundError, ValueError):  # ValueError: read while another process was writing
            return default

    def set(self, key, value) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        data[key] = value
        # Slow enough that two unlocked writers overlap.
        time.sleep(0.002)
        self.path.write_text(json.dumps(data), encoding="utf-8")


def child_main(args: list[str]) -> int:
    mod = load()
    mode, path = args[0], args[1]
    if mode in ("hold", "wait", "wait-shared", "hold-shared"):
        shared = mode.endswith("shared")
        opener = open(path, "rb") if shared else open(path, "a+b")
        with opener as handle:
            started = time.monotonic()
            with mod._file_lock(handle, shared=shared):
                print(f"LOCKED waited={time.monotonic() - started:.3f}", flush=True)
                if mode.startswith("hold"):
                    time.sleep(float(args[2]))
        print("RELEASED", flush=True)
        return 0
    if mode == "increment":
        if args[3] == "nolock":
            mod._file_lock = lambda handle, shared=False: contextlib.nullcontext()
        store = mod.TaskStore(FileState(args[2]))
        for _ in range(ROUNDS):
            store.modify("t", lambda task: {"count": int(task.get("count", 0)) + 1})
        return 0
    return 2


def spawn(*args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, __file__, "--child", *args], stdout=subprocess.PIPE, text=True)


def waited(line: str) -> float:
    return float(line.split("waited=")[1])


def main() -> int:
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:800], flush=True)

    mod = load()
    with tempfile.TemporaryDirectory() as box_dir:
        lock = str(Path(box_dir) / "tasks.lock")

        # 1. A holder makes the next process wait, then lets it in.
        holder = spawn("hold", lock, "1.5")
        assert holder.stdout.readline().startswith("LOCKED")
        started = time.monotonic()
        waiter = spawn("wait", lock)
        line = waiter.stdout.readline().strip()
        waiter.wait(30)
        holder.wait(30)
        check(line.startswith("LOCKED") and waited(line) >= 0.8,
              "a second process waits for the holder, then takes the lock", line=line,
              seconds=round(time.monotonic() - started, 2))

        # 2. A killed holder releases the lock.
        holder = spawn("hold", lock, "60")
        assert holder.stdout.readline().startswith("LOCKED")
        waiter = spawn("wait", lock)
        time.sleep(0.5)
        still_waiting = waiter.poll() is None
        holder.kill()
        holder.wait(30)
        line = waiter.stdout.readline().strip()
        waiter.wait(30)
        check(still_waiting and line.startswith("LOCKED"), "a holder killed while it holds the lock releases it",
              still_waiting=still_waiting, line=line)

        # 3. A reader waits for a writer; two readers wait for each other only where there is no shared lock.
        holder = spawn("hold", lock, "1.5")
        assert holder.stdout.readline().startswith("LOCKED")
        reader = spawn("wait-shared", lock)
        line = reader.stdout.readline().strip()
        reader.wait(30)
        holder.wait(30)
        check(line.startswith("LOCKED") and waited(line) >= 0.8, "a reader waits for a writer", line=line)

        first = spawn("hold-shared", lock, "1.5")
        assert first.stdout.readline().startswith("LOCKED")
        second = spawn("wait-shared", lock)
        line = second.stdout.readline().strip()
        second.wait(30)
        first.wait(30)
        shares = os.name != "nt"
        check(line.startswith("LOCKED") and (waited(line) < 0.8 if shares else waited(line) >= 0.8),
              "two readers share the lock" if shares else "Windows: a reader takes the exclusive lock, readers queue",
              line=line)

        # 4. The lock file is free once everyone has left.
        after = spawn("wait", lock)
        line = after.stdout.readline().strip()
        after.wait(30)
        check(line.startswith("LOCKED") and waited(line) < 0.5, "the lock file is free after its holders ended", line=line)

    # 4b. The read of another profile's tasks (ProfileTasks._read_profile_tasks) waits for that profile's writer.
    from hermes_cli.plugins_state import PluginState
    with tempfile.TemporaryDirectory() as box_dir:
        home = Path(box_dir)
        folder = home / "plugin-data" / PluginState(mod.PLUGIN_ID).data_dir.name
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({mod.STATE_KEY: {"t-1": {"status": "running"}}}), encoding="utf-8")
        mod.ProfileTasks._home = staticmethod(lambda profile: home)
        before = mod.ProfileTasks._read_profile_tasks("other")
        holder = spawn("hold", str(folder / "tasks.lock"), "1.5")
        assert holder.stdout.readline().startswith("LOCKED")
        started = time.monotonic()
        during = mod.ProfileTasks._read_profile_tasks("other")
        seconds = time.monotonic() - started
        holder.wait(30)
        check(before == during == {"t-1": {"status": "running"}} and seconds >= 0.8,
              "the read of another profile's tasks waits for the writer that holds its lock, then returns them",
              before=before, during=during, seconds=round(seconds, 2))

    # 5. No lost update through TaskStore.modify, and lost updates with the lock off.
    for mode in ("lock", "nolock"):
        with tempfile.TemporaryDirectory() as box_dir:
            store = mod.TaskStore(FileState(box_dir))
            store.modify("t", lambda task: {"count": 0})
            procs = [subprocess.Popen([sys.executable, __file__, "--child", "increment", "-", box_dir, mode])
                     for _ in range(WORKERS)]
            codes = [proc.wait(180) for proc in procs]
            # Without the lock a torn write can even lose the task.
            count = (store.get("t") or {}).get("count", 0)
        if mode == "lock":
            check(codes == [0] * WORKERS and count == WORKERS * ROUNDS,
                  f"{WORKERS} processes x {ROUNDS} changes lose no update", count=count, expected=WORKERS * ROUNDS,
                  codes=codes)
        else:
            check(count < WORKERS * ROUNDS, "the same run without the lock loses updates (the check can fail)",
                  count=count, expected=WORKERS * ROUNDS)

    print(f"{failures} failure(s)", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        raise SystemExit(child_main(sys.argv[2:]))
    raise SystemExit(main())
