"""Check owned Windows runtime absence or verify the freshly seeded deployment."""
import argparse
import http.client
import json
import socket
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "terraform" / ".kiln-local"


def read(port, route, method="GET", data=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    headers = {"Connection": "close"}
    if data is not None:
        headers.update({"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        conn.request(method, route, None if data is None else json.dumps(data), headers)
        reply = conn.getresponse()
        return reply.status, json.loads(reply.read()), reply.getheader("X-Node")
    finally:
        conn.close()


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def stopped(ports):
    checks = {"runtime_removed": not RUNTIME.exists(),
              "listeners_closed": all(not listening(port) for port in ports)}
    if not all(checks.values()):
        raise RuntimeError(f"teardown verification failed: {checks}")
    return checks


def fresh(ports):
    gateway = ports[0]
    status, summary, _ = read(gateway, "/api/summary")
    if status != 200 or len(summary.get("kilns", [])) != 6 or summary.get("reading_count") != 2160:
        raise RuntimeError("expected six seeded kilns with 2160 observations")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    observation = {"kiln_id": "K03", "firing_id": "recreate-smoke", "sequence": 1,
                   "observed_at": now, "phase": "heating", "temperature_c": 149.5}
    token = (RUNTIME / "credential" / "sensor.token").read_text(encoding="ascii").strip()
    sent = {"readings": [observation]}
    first, new, node_a = read(gateway, "/api/readings", "POST", sent, token)
    again, replay, node_b = read(gateway, "/api/readings", "POST", sent, token)
    observation["temperature_c"] = 150
    conflict, _, _ = read(gateway, "/api/readings", "POST", sent, token)
    series_status, series, _ = read(gateway, "/api/series/K03/recreate-smoke")
    points = series.get("points", [])
    if (first != 201 or new.get("accepted") != 1 or again != 200 or replay.get("duplicates") != 1
            or conflict != 409 or series_status != 200 or len(points) != 1
            or points[0]["temperature_c"] != 149.5 or node_a == node_b):
        raise RuntimeError("recreated service failed write, cross-replica retry or conflict checks")
    return {"seeded_kilns": len(summary["kilns"]), "seeded_observations": summary["reading_count"],
            "write": first, "replay": again, "conflict": conflict, "replicas": [node_a, node_b],
            "persisted_points_for_smoke_run": len(points)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["stopped", "fresh"])
    parser.add_argument("--port", type=int, default=18340)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    ports = range(args.port, args.port + 3)
    result = stopped(ports) if args.mode == "stopped" else fresh(ports)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
