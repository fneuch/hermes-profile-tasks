"""usage-governor cron entry: one tick, or `--health`. Run by ~/.hermes/scripts/usage-governor-tick.py.

Prints nothing on a normal tick (a silent cron run), one REVIEW line at the daily review; `--health` prints
one line and exits 0 when healthy, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import governor  # noqa: E402 - the plugin dir goes on sys.path first


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--health" in argv:
        ok, reasons = governor.health()
        print("usage-governor: ok" if ok else "usage-governor: not ok: " + "; ".join(reasons))
        return 0 if ok else 1
    governor.tick()
    return 0


if __name__ == "__main__":
    sys.exit(main())
