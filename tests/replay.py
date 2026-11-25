"""Finish a previous interrupted measurement using its original durable sensor queue."""
import argparse
import json
import re
import threading
import time
from contextlib import closing
from pathlib import Path

from measure import ROOT, STATE, readback, utc_now
from sensor import Outbox, sender


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("summary", type=Path)
    p.add_argument("--port", type=int, default=18340)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    old = json.loads(args.summary.read_text(encoding="utf-8"))
    firing = old["firing_id"]
    if not re.fullmatch(r"run-[0-9a-f]{14}", firing):
        p.error("expected an experiment firing ID")
    spool = ROOT / ".runs" / "sensors" / (firing + ".sqlite3")
    if not spool.is_file():
        p.error("original sensor queue is unavailable")
    if args.out.exists():
        p.error("choose a new evidence filename")
    expected = {(f"K{i:02d}", n) for i in range(1, old["sensors"] + 1)
                for n in range(1, old["ticks_per_sensor"] + 1)}
    with closing(Outbox(spool)) as q:
        pending = q.count()
    token = (STATE / "credential" / "sensor.token").read_text(encoding="ascii").strip()
    started = time.perf_counter()
    stop = threading.Event()
    stop.deadline = started + 150
    stop.set()
    attempts, errors = [], []
    sender(spool, args.port, token, 12, 0, threading.Event(), stop, attempts, errors, started)
    with closing(Outbox(spool)) as q:
        remaining = q.count()
    check = readback(STATE / "data.sqlite3", firing, expected)
    result = {"utc": utc_now(), "firing_id": firing, "pending_before": pending,
              "pending_after": remaining, "post_attempts": len(attempts),
              "elapsed_s": round(time.perf_counter()-started, 3), "db_check": check, "errors": errors}
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    if errors or remaining or any(check[k] for k in ["missing", "unexpected", "duplicate_rows"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
