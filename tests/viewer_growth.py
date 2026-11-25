"""Compare one or more independent dashboard viewers polling a local gateway."""
import argparse
import concurrent.futures
import threading
import time
from pathlib import Path

from measure import percentile, save_result, utc_now
from readload import metrics, sample


def run(args):
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "summary.json").exists():
        raise FileExistsError("choose a new output directory")
    ports = [args.port + i for i in (1, 2)]
    for _ in ports:
        if not sample(args.port, time.perf_counter())[0]:
            raise RuntimeError("failed to warm up both collectors")
    before = metrics(ports)
    ready = threading.Barrier(args.viewers + 1, timeout=10)
    go = threading.Event()
    requests_per_viewer = args.seconds * 2

    def viewer(index):
        # Each viewer keeps its own schedule. Stagger starts within one poll interval.
        rows = []
        ready.wait()
        go.wait()
        for tick in range(requests_per_viewer):
            due = start + (tick + index / args.viewers) / 2
            time.sleep(max(0, due - time.perf_counter()))
            rows.append(sample(args.port, due))
        return rows

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.viewers) as pool:
        jobs = [pool.submit(viewer, i) for i in range(args.viewers)]
        ready.wait()
        started = utc_now()
        start = time.perf_counter()
        go.set()
        by_viewer = [job.result() for job in jobs]
    after = metrics(ports)
    rows = [row for group in by_viewer for row in group]
    ok = [row for row in rows if row[0]]
    within_target = sum(row[3] <= 200 for row in ok)
    origin = sum(after[n]["origin_reads"] - b["origin_reads"] for n, b in before.items())
    hits = sum(after[n]["cache_hits"] - b["cache_hits"] for n, b in before.items())
    if len(rows) != args.viewers * requests_per_viewer:
        raise RuntimeError("viewer schedule was incomplete")
    save_result(args.out, {
        "started_utc": started, "ended_utc": utc_now(), "poll_window_s": args.seconds,
        "viewers": args.viewers, "polls_per_viewer_per_s": 2, "offered_rps": 2 * args.viewers,
        "offered": len(rows), "successful": len(ok), "failed": len(rows) - len(ok),
        "valid_responses_by_viewer": [sum(r[0] for r in group) for group in by_viewer],
        "cache_seconds_by_replica": {n: b["cache_seconds"] for n,b in before.items()},
        "cache_hits": hits, "origin_summary_queries": origin,
        "valid_scheduled_p95_ms": percentile([r[3] for r in ok],95),
        "slo_threshold_ms": 200, "slo_good": within_target,
        "slo_good_pct": round(100 * within_target / len(rows), 2),
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18340)
    parser.add_argument("--viewers", type=int, choices=[1, 10], required=True)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.seconds <= 120 or not 1024 <= args.port <= 65533:
        parser.error("seconds must be 2..120 and port must be 1024..65533")
    run(args)
