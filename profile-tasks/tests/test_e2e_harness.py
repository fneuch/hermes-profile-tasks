"""The e2e harness helpers:

- the probe plugin never leaves a partly written response where the trial reads it;
- ``stop_gateway.alive`` says not alive, without raising, for a gateway that exits while it is read;
- two ``run.sh`` invocations started in the same second do not share a home;
- the remote MCP fixture starts off Linux (no ``prctl`` there);
- the keyed trial does not count Hermes's background review of an earlier session as a manager turn.

Run: ``PT_SCRIPT=tests/test_e2e_harness.py e2e/run.sh`` (isolated HOME/HERMES_HOME, the installed Hermes
interpreter). ``e2e/run.sh <path to an e2e folder>`` with that script runs it against another copy.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

E2E = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "e2e")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    failures = 0

    def check(ok: bool, what: str, **evidence) -> None:
        nonlocal failures
        failures += not ok
        print("PASS" if ok else "FAIL", what, json.dumps(evidence, default=str)[:800], flush=True)

    # 1. Probe responses: each write is split in two; between the halves the reader looks at the response.
    probe = load("pt_probe_under_test", E2E / "probe-plugin" / "__init__.py")
    sys.modules[probe.MODULE] = type(sys)("fake_profile_tasks")
    sys.modules[probe.MODULE].send = sys.modules[probe.MODULE].result = lambda **args: {"echo": args}
    torn: list[str] = []
    real_write = pathlib.Path.write_text

    def write_in_two(self, data, *a, **k):
        with open(self, "w") as handle:
            handle.write(data[: len(data) // 2])
            handle.flush()
            for response in self.parent.glob("response-*.json"):
                try:
                    json.loads(response.read_text())
                except ValueError:
                    torn.append(response.name)
            handle.write(data[len(data) // 2:])
        return len(data)

    with tempfile.TemporaryDirectory() as box_dir:
        box = Path(box_dir)
        pathlib.Path.write_text = write_in_two
        try:
            threading.Thread(target=probe._serve, args=(box,), daemon=True).start()
            # Renamed into place, as the trial does, so the probe never reads a half-written request.
            real_write(box / "request.tmp", json.dumps({"op": "send", "args": {"key": "k"}}))
            (box / "request.tmp").replace(box / "request-1.json")
            deadline = time.monotonic() + 10
            while not (box / "response-1.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)
        finally:
            pathlib.Path.write_text = real_write
        response = box / "response-1.json"
        body = json.loads(response.read_text()) if response.exists() else None
    check(not torn and bool(body and body.get("ok")),
          "the probe's response exists only once fully written", torn=torn, body=body)

    # 2. stop_gateway.alive on a gateway that exits between the existence check and the read.
    stop = load("pt_stop_under_test", E2E / "stop_gateway.py")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    real_exists = pathlib.Path.exists
    pathlib.Path.exists = lambda self, *a, **k: True if str(self) == f"/proc/{gone.pid}" else real_exists(self, *a, **k)
    try:
        result = stop.alive(gone.pid)
        error = None
    except Exception as exc:  # noqa: BLE001 - the failure this check is about
        result, error = None, f"{exc.__class__.__name__}: {exc}"
    finally:
        pathlib.Path.exists = real_exists
    check(result is False and error is None, "a gateway that exits while it is read is not alive, no error",
          result=result, error=error)
    zombie = subprocess.Popen([sys.executable, "-c", "pass"])
    os.waitid(os.P_PID, zombie.pid, os.WEXITED | os.WNOWAIT)
    check(stop.alive(os.getpid()) and not stop.alive(zombie.pid), "a live process is alive, a zombie is not")
    zombie.wait()

    # 3. Two run.sh invocations started in the same second (date pinned) each get their own home.
    with tempfile.TemporaryDirectory() as box_dir:
        box = Path(box_dir)
        (box / "bin").mkdir()
        (box / "bin" / "date").write_text("#!/bin/sh\necho 20260101-000000\n")
        (box / "bin" / "date").chmod(0o755)
        script = box / "print_home.py"
        script.write_text("import os, time\ntime.sleep(0.5)\nprint(os.environ['HOME'])\n")
        env = {"PATH": f"{box / 'bin'}:/usr/bin:/bin", "HOME": str(box), "PT_E2E_BASE": str(box / "runs"),
               "HERMES_PYTHON": sys.executable, "HERMES_AGENT_DIR": os.environ.get("PT_AGENT", ""),
               "PT_SCRIPT": os.path.relpath(script, E2E.parent)}
        runs = [subprocess.Popen(["bash", str(E2E / "run.sh")], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True) for _ in range(2)]
        homes = [proc.communicate(timeout=60)[0].strip().splitlines()[-1:] for proc in runs]
        codes = [proc.returncode for proc in runs]
    check(codes == [0, 0] and len({tuple(h) for h in homes}) == 2,
          "two harness runs started in the same second get separate homes", homes=homes, codes=codes)

    # 4. The remote MCP fixture's preexec hook off Linux (macOS has no libc.so.6 and no prctl), and on Linux.
    import ctypes

    harness = load("pt_e2e_under_test", E2E / "run_e2e.py")
    loaded: list[str] = []

    def no_libc(name, *a, **k):
        loaded.append(name)
        raise OSError(f"{name}: cannot open shared object file")

    real_cdll, real_platform = ctypes.CDLL, sys.platform
    ctypes.CDLL = no_libc
    errors: dict[str, str | None] = {}
    try:
        for platform in ("darwin", "linux"):
            sys.platform = platform
            try:
                harness._die_with_parent()
                errors[platform] = None
            except OSError as exc:
                errors[platform] = str(exc)
    finally:
        ctypes.CDLL, sys.platform = real_cdll, real_platform
    check(errors["darwin"] is None and errors["linux"] is not None and loaded == ["libc.so.6"],
          "the fixture's die-with-parent hook is a no-op off Linux and still sets PR_SET_PDEATHSIG on Linux",
          errors=errors, loaded=loaded)

    # 5. A background memory/skill review of an earlier manager session is not counted as a manager turn.
    from agent import background_review

    prompts = [background_review._MEMORY_REVIEW_PROMPT, background_review._SKILL_REVIEW_PROMPT,
               background_review._COMBINED_REVIEW_PROMPT]

    class Server:
        def main_requests(self):
            turn = {"messages": [{"role": "user", "content": "SCENARIO:X a task"}]}
            reviews = [{"messages": [{"role": "user", "content": "SCENARIO:X a task"},
                                     {"role": "assistant", "content": "done"},
                                     {"role": "user", "content": prompt}]} for prompt in prompts]
            return [turn, *reviews]

    counted = len(harness.conversation_turns(Server()))
    check(counted == 1 and all(p.startswith(harness.REVIEW_PROMPT_START) for p in prompts),
          "the keyed check's manager-turn count leaves out Hermes's background review requests", counted=counted)

    print(f"{failures} failure(s)", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
