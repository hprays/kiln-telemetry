"""Recalculate sensor and outage metrics from the archived client-side event logs."""
import csv
import json
import math
from collections import Counter
from pathlib import Path


EVIDENCE = Path(__file__).resolve().parents[1] / "evidence"


def rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def require(actual, expected, metric):
    if actual != expected:
        raise ValueError(f"{metric}: recorded {expected}, recalculated {actual}")


def verify(folder):
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    acks = rows(folder / "acks.csv")
    attempts = rows(folder / "attempts.csv")
    probes = rows(folder / "probes.csv")
    events = rows(folder / "events.csv")
    keys = {(r["kiln"], r["sequence"]) for r in acks}
    require(len(keys), len(acks), "unique confirmed keys")
    require(len(acks), summary["acknowledged"], "confirmed records")
    delays = sorted(round((int(r["ack_unix_ns"]) - int(r["produced_unix_ns"])) / 1e6, 3) for r in acks)
    require(delays[math.ceil(0.95 * len(delays)) - 1], summary["client_ack_p95_ms"], "ack p95")
    require(len(attempts), summary["attempts"], "POST attempts")
    require(dict(Counter(r["status"] for r in attempts)), summary["status_counts"], "POST statuses")
    require(len(probes), summary["probe_total"], "read attempts")
    require(sum(int(r["ok"]) for r in probes), summary["probe_ok"], "valid reads")
    if summary["fault"]:
        clock = {r["event"]: float(r["t_s"]) for r in events}
        down, restart = clock["target_stopped"], clock["restart_requested"]
        during = [p for p in probes if down <= float(p["t_s"]) < restart]
        first = next(p for p in probes if float(p["t_s"]) >= down and int(p["ok"]))
        fault = summary["fault"]
        require(len(during), fault["probes_while_stopped"], "outage reads")
        require(sum(int(r["ok"]) for r in during), fault["success_while_stopped"], "outage successes")
        require(round(float(first["end_s"]) - down, 4), fault["first_success_after_confirmed_stop_s"], "failover")
    return {"trial": folder.name, "unique_confirmed": len(keys), "posts": len(attempts),
            "read_probes": len(probes), "ack_p95_ms": summary["client_ack_p95_ms"]}


if __name__ == "__main__":
    trials = ["s1-single", "s1-dual", "s2-normal", "s2-peak-single", "s2-peak-batch",
              "s2-drain-35s-limit", "s2-drain-single", "s2-drain-batch"]
    results = [verify(EVIDENCE / name) for name in trials]
    print(json.dumps({"verified_trials": len(results), "results": results}, indent=2))
