"""Common local instrumentation for the kiln telemetry experiments (Python 3.10+)."""
import csv
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "terraform" / ".kiln-local"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[math.ceil(len(ordered) * percent / 100) - 1], 3)


def request(port, method, path, payload=None, token=None, timeout=2.0):
    headers = {"Accept": "application/json", "Connection": "close"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout)
    start = time.perf_counter()
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            data = {}
        status, node = response.status, response.getheader("X-Node", "")
    except (OSError, EOFError, HTTPException) as exc:
        status, node, data = 0, "", {"error": type(exc).__name__}
    finally:
        conn.close()
    return status, node, data, round((time.perf_counter() - start) * 1000, 3)


def sensor_readings(firing, sequence, observed):
    # Accelerated synthetic firing. All comparisons use the same deterministic values.
    step = min(sequence - 1, 59)
    readings = []
    for i in range(1, 7):
        peak = 1040 + 8 * i
        if step < 28:
            phase, temperature = "heating", 25 + (peak - 25) * step / 28
        elif step < 39:
            phase, temperature = "holding", peak + 3 * math.sin(step - 28)
        else:
            phase, temperature = "cooling", peak - (peak - 75) * (step - 39) / 20
        readings.append({"kiln_id": f"K{i:02}", "firing_id": firing, "sequence": sequence,
                         "observed_at": observed, "phase": phase, "temperature_c": round(temperature, 1)})
    return readings


def readback(database, firing, expected):
    # Separately enumerate the persisted keys, without trusting the ACK response.
    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        rows = conn.execute("SELECT kiln_id, sequence FROM readings WHERE firing_id = ?", (firing,)).fetchall()
    unique = set(rows)
    return {"expected": len(expected), "stored_rows": len(rows), "stored_keys": len(unique),
            "missing": len(expected - unique), "unexpected": len(unique - expected),
            "duplicate_rows": len(rows) - len(unique)}


def write_csv(target, rows, fields):
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_result(folder, result):
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    (folder / "summary.json").write_text(rendered + "\n", encoding="utf-8")
    print(rendered, flush=True)
