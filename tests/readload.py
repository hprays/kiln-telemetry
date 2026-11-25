"""A bounded closed-loop capacity run and an open-loop user-facing SLO test."""
import argparse
import collections
import concurrent.futures
import math
import time
from pathlib import Path

from measure import percentile, request, save_result, utc_now
from process_stats import sample_processes


def sample(port, due):
    start = time.perf_counter()
    status, node, body, http_ms = request(port, "GET", "/api/summary")
    finish = time.perf_counter()
    kilns = body.get("kilns", [])
    ok = (status == 200 and isinstance(kilns, list) and len(kilns) == 6
          and all(isinstance(k, dict) for k in kilns)
          and {k.get("kiln_id") for k in kilns} == {f"K{i:02d}" for i in range(1, 7)}
          and body.get("reading_count") == sum(k.get("reading_count", 0) for k in kilns))
    return ok, node, http_ms, (finish - due) * 1000, finish, start


def metrics(ports):
    result = {}
    for port in ports:
        code, node, body, _ = request(port, "GET", "/metrics")
        if code != 200 or not node or not {"origin_reads", "requests", "cache_hits"} <= body.keys():
            raise RuntimeError("An expected API metrics endpoint is unavailable.")
        result[node] = body
    return result


def measure(args):
    target = Path(args.out)
    target.mkdir(parents=True, exist_ok=True)
    if (target / "summary.json").exists():
        raise FileExistsError("use a fresh evidence directory")
    ports = [args.port + i for i in range(1, args.replicas + 1)]
    for _ in ports:
        if not sample(args.port, time.perf_counter())[0]:
            raise RuntimeError("gateway did not pass a readiness request")
    before = metrics(ports)
    nodes = ["kiln-gateway", *sorted(before)]
    cpu_before = sample_processes(nodes)
    cpu_start = time.perf_counter()
    records = []
    dropped = 0
    utc = utc_now()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        start = time.perf_counter()
        end = start + args.seconds
        if args.rate == 0:
            def worker():
                own = []
                while (now := time.perf_counter()) < end:
                    own.append(sample(args.port, now))
                return own
            jobs = [pool.submit(worker) for _ in range(args.workers)]
            for job in jobs:
                records.extend(job.result())
        else:
            pending = set()
            for i in range(math.ceil(args.seconds * args.rate)):
                due = start + i / args.rate
                time.sleep(max(0, due - time.perf_counter()))
                finished = {job for job in pending if job.done()}
                records.extend(job.result() for job in finished)
                pending -= finished
                if len(pending) >= 3 * args.workers:
                    dropped += 1
                else:
                    pending.add(pool.submit(sample, args.port, due))
            records.extend(job.result() for job in pending)
            time.sleep(max(0, end - time.perf_counter()))
    elapsed = time.perf_counter() - start
    cpu_after = sample_processes(nodes)
    cpu_elapsed = time.perf_counter() - cpu_start
    after = metrics(ports)
    count = len(records) + dropped
    ok_rows = [row for row in records if row[0]]
    within = sum(row[3] <= 200 for row in ok_rows)
    hits = sum(after[n]["cache_hits"] - b["cache_hits"] for n,b in before.items())
    source_queries = sum(after[n]["origin_reads"] - b["origin_reads"] for n,b in before.items())
    origin_ms = round(1000 * sum(after[n]["origin_seconds"] - b["origin_seconds"] for n,b in before.items()), 2)
    result = {
        "started_utc": utc, "ended_utc": utc_now(), "mode": "closed" if not args.rate else "open",
        "duration_s": args.seconds, "workers": args.workers, "offered_rate_per_s": args.rate,
        "cache_seconds_by_replica": {node: d["cache_seconds"] for node,d in before.items()},
        "offered": count, "successful": len(ok_rows), "failures": count-len(ok_rows),
        "client_shed": dropped, "elapsed_with_drain_s": round(elapsed, 4),
        "successful_rps": round(len(ok_rows)/elapsed, 3), "http_p95_ms": percentile([r[2] for r in records], 95),
        "scheduled_p95_ms": percentile([r[3] for r in records], 95), "slo_threshold_ms": 200,
        "slo_target_pct": 99.0, "slo_good": within, "slo_good_pct": round(100*within/count, 3),
        "slo_met": 100*within/count >= 99.0, "node_counts": dict(collections.Counter(r[1] for r in ok_rows)),
        "origin_summary_queries": source_queries, "origin_query_total_ms": origin_ms, "cache_hits": hits,
        "cache_hit_pct": round(100*hits/(hits+source_queries), 3) if hits+source_queries else None,
        "process_usage": {node: {
            "mean_cpu_pct_one_logical_core": round(100 * (cpu_after[node]["cpu_s"] - cpu_before[node]["cpu_s"]) / cpu_elapsed, 2),
            "start_rss_mib": cpu_before[node]["rss_mib"], "end_rss_mib": cpu_after[node]["rss_mib"]
        } for node in nodes},
    }
    save_result(target, result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18340)
    parser.add_argument("--replicas", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--seconds", type=int, default=8)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--rate", type=int, default=20, help="0 = closed-loop capacity")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if not 2 <= args.seconds <= 120 or not 1 <= args.workers <= 32 or not 0 <= args.rate <= 500:
        parser.error("seconds must be 2..120, workers 1..32 and rate 0..500/s")
    measure(args)
