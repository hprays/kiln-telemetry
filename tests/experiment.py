"""Run against the Terraform-created localhost gateway. Writes CSV traces and a JSON summary."""
import argparse
import collections
import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from measure import ROOT, STATE, percentile, readback, request, save_result, sensor_readings, utc_now, write_csv
from sensor import Outbox, sender


def control(action):
    proc = subprocess.run([sys.executable, str(ROOT / "ops/lifecycle.py"), action, "kiln-1"],
                          cwd=ROOT, text=True, capture_output=True, timeout=30)
    if proc.returncode:
        raise RuntimeError(f"{action} kiln-1 failed ({proc.returncode}): {proc.stderr[-300:]}")


def probe(port, done, active, log):
    while not done.is_set():
        start = time.perf_counter()
        status, node, body, elapsed = request(port, "GET", "/api/summary", timeout=2)
        log.append({"t_s": round(start - active, 4), "end_s": round(time.perf_counter() - active, 4),
                    "ok": int(status == 200 and "kilns" in body), "status": status,
                    "node": node, "http_ms": elapsed})
        done.wait(max(0, 0.1 - (time.perf_counter() - start)))


def fault(active, stop, queue_path, events, errors):
    def mark(name):
        events.append({"event": name, "t_s": round(time.perf_counter() - active, 4), "utc": utc_now()})

    if stop.wait(1.5):
        return
    mark("stop_requested")
    try:
        control("stop-api")
        mark("target_stopped")
        stop.wait(3.0)
        connection = Outbox(queue_path)
        events.append({"event": "outbox_checked_from_independent_connection",
                       "t_s": round(time.perf_counter() - active, 4), "utc": utc_now(),
                       "pending": connection.count()})
        connection.close()
    except Exception as exc:
        errors.append(str(exc))
    finally:
        mark("restart_requested")
        try:
            control("start-api")
            mark("target_restarted")
        except Exception as exc:
            errors.append(str(exc))


def run(args):
    status, _, precheck, _ = request(args.port, "GET", "/api/summary")
    if status != 200 or "kilns" not in precheck:
        raise RuntimeError("apply the kiln lab and wait for a healthy summary first")
    token = (STATE / "credential/sensor.token").read_text(encoding="utf-8-sig").strip()
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "summary.json").exists():
        raise FileExistsError("use a new evidence directory to retain the previous measurement")
    firing = "run-" + uuid.uuid4().hex[:14]
    # Sensor storage survives destruction/recreation of the server infrastructure.
    spool_dir = ROOT / ".runs" / "sensors"
    spool_dir.mkdir(parents=True, exist_ok=True)
    spool = spool_dir / (firing + ".sqlite3")
    producer = Outbox(spool)
    stop, done, hold_ack = threading.Event(), threading.Event(), threading.Event()
    if args.scenario == "outage":
        hold_ack.set()
    attempts, probes, events, errors = [], [], [], []
    started = utc_now()
    active = time.perf_counter()
    load = threading.Thread(target=sender, args=(spool, args.port, token, args.batch, args.flush_ms / 1000,
                                                 hold_ack, stop, attempts, errors, active))
    watcher = threading.Thread(target=probe, args=(args.port, done, active, probes))
    injector = threading.Thread(target=fault, args=(active, done, spool, events, errors))
    load.start()
    watcher.start()
    if args.scenario == "outage":
        injector.start()
    max_pending = 0
    expected = set()
    chart_source = []
    try:
        for sequence in range(1, args.ticks + 1):
            target = active + (sequence - 1) / args.hz if args.hz else active
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            records = sensor_readings(firing, sequence, utc_now())
            producer.enqueue(records)
            expected.update((r["kiln_id"], sequence) for r in records)
            chart_source.append(next(r for r in records if r["kiln_id"] == "K03"))
            max_pending = max(max_pending, producer.count())
        if injector.is_alive():
            injector.join(timeout=68)
    finally:
        stop.deadline = time.perf_counter() + 35
        stop.set()
        load.join(timeout=40)
        done.set()
        watcher.join(timeout=3)
        if injector.is_alive():
            injector.join(timeout=32)
    duration = time.perf_counter() - active
    receipts = producer.receipts()
    remaining = producer.count()
    producer.close()
    write_csv(folder / "attempts.csv", attempts,
              ["t_s","status","node","records","http_ms","accepted","duplicates","ack_saved","pending"])
    write_csv(folder / "probes.csv", probes, ["t_s","end_s","ok","status","node","http_ms"])
    receipt_rows = [{"kiln": k, "sequence": n, "produced_unix_ns": c, "ack_unix_ns": a,
                     "ack_ms": round((a-c)/1e6, 3)} for k,n,c,a in receipts]
    write_csv(folder / "acks.csv", receipt_rows, ["kiln","sequence","produced_unix_ns","ack_unix_ns","ack_ms"])
    write_csv(folder / "events.csv", events, ["event","t_s","utc","pending"])
    check = readback(STATE / "data.sqlite3", firing, expected)
    status, _, chart, _ = request(args.port, "GET", f"/api/series/K03/{firing}")
    def point_tuple(row):
        stamp = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
        return stamp, row["sequence"], row["temperature_c"]
    expected_chart = sorted(point_tuple(r) for r in chart_source)[-720:]
    observed_chart = [point_tuple(r) for r in chart.get("points", [])]
    chart_ok = status == 200 and expected_chart == observed_chart
    good = [p for p in probes if p["ok"]]
    ack = [r["ack_ms"] for r in receipt_rows]
    ok_attempts = [a for a in attempts if a["ack_saved"]]
    ack_window_s = (max(r[3] for r in receipts) - min(r[2] for r in receipts)) / 1e9 if receipts else None
    marked = {e["event"]: e["t_s"] for e in events}
    fault_summary = None
    if "target_stopped" in marked:
        down = marked["target_stopped"]
        end = marked["restart_requested"]
        next_good = next((p for p in good if p["t_s"] >= down), None)
        window = [p for p in probes if down <= p["t_s"] < end]
        fault_summary = {"confirmed_stop_s": down, "restart_requested_s": end,
                         "poll_period_ms": 100, "first_success_after_confirmed_stop_s":
                         round(next_good["end_s"] - down, 4) if next_good else None,
                         "first_success_node": next_good["node"] if next_good else None,
                         "success_while_stopped": sum(p["ok"] for p in window), "probes_while_stopped": len(window),
                         "nodes_while_stopped": sorted({p["node"] for p in window if p["ok"]})}
    result = {"scenario": args.scenario, "label": args.label, "start_utc": started, "end_utc": utc_now(),
              "firing_id": firing, "sensors": 6, "ticks_per_sensor": args.ticks,
              "offered_hz_per_sensor": args.hz, "batch_max": args.batch, "flush_ms": args.flush_ms,
              "duration_s": round(duration, 3), "client_ack_window_s": round(ack_window_s, 4) if ack_window_s else None,
              "acknowledged": len(receipts), "records_per_s": round(len(receipts)/ack_window_s, 3) if ack_window_s else None,
              "attempts": len(attempts), "ack_saved_requests": len(ok_attempts),
              "status_counts": dict(collections.Counter(str(a["status"]) for a in attempts)),
              "deduplicated_on_server": sum(a["duplicates"] or 0 for a in attempts),
              "client_ack_p50_ms": percentile(ack, 50), "client_ack_p95_ms": percentile(ack, 95),
              "client_ack_max_ms": max(ack, default=None),
              "ok_post_p95_ms": percentile([a["http_ms"] for a in ok_attempts], 95),
              "max_outbox_observed": max([max_pending] + [a["pending"] for a in attempts]),
              "outbox_remaining": remaining, "db_check": check,
              "chart_check": {"kiln": "K03", "points": len(observed_chart), "times_sequences_temperatures_match": chart_ok},
              "probe_total": len(probes), "probe_ok": len(good),
              "probe_availability_pct": round(100 * len(good)/len(probes), 3) if probes else None,
              "probe_p95_ms": percentile([p["http_ms"] for p in good], 95), "fault": fault_summary, "events": events,
              "errors": errors}
    save_result(folder, result)
    if (errors or load.is_alive() or remaining or check["missing"] or check["unexpected"] or
            check["duplicate_rows"] or not chart_ok or len(receipts) != len(expected)):
        raise RuntimeError("experiment did not satisfy delivery invariants; see raw evidence")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scenario", choices=["load","outage"])
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=18340)
    ap.add_argument("--ticks", type=int, default=60)
    ap.add_argument("--hz", type=float, default=10, help="samples/s per sensor; 0 enqueues immediately")
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--flush-ms", type=float, default=200)
    args = ap.parse_args()
    if not (1 <= args.ticks <= 5000 and 0 <= args.hz <= 1000 and 1 <= args.batch <= 128
            and 0 <= args.flush_ms <= 5000 and 1024 <= args.port <= 65535):
        ap.error("parameter outside the bounded local test range")
    run(args)


if __name__ == "__main__":
    main()
