"""Disk-backed sensor simulator. Safe to restart with the same outbox file."""
import json
import sqlite3
import time

from measure import request


class Outbox:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY, data TEXT NOT NULL, created_ns INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS receipts(id INTEGER, kiln TEXT, seq INTEGER, created_ns INTEGER, ack_ns INTEGER);"
        )

    def enqueue(self, records):
        created = time.time_ns()
        with self.db:
            self.db.executemany("INSERT INTO pending(data,created_ns) VALUES(?,?)",
                                [(json.dumps(item, separators=(",", ":")), created) for item in records])

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]

    def next_batch(self, maximum):
        return self.db.execute("SELECT id,data,created_ns FROM pending ORDER BY id LIMIT ?", (maximum,)).fetchall()

    def acknowledge(self, rows, records):
        # One SQLite transaction confirms delivery and removes the retryable copy.
        now = time.time_ns()
        with self.db:
            self.db.executemany("INSERT INTO receipts VALUES(?,?,?,?,?)",
                                [(row[0], r["kiln_id"], r["sequence"], row[2], now)
                                 for row, r in zip(rows, records)])
            self.db.executemany("DELETE FROM pending WHERE id=?", [(row[0],) for row in rows])

    def receipts(self):
        return self.db.execute("SELECT kiln,seq,created_ns,ack_ns FROM receipts ORDER BY ack_ns, kiln, seq").fetchall()

    def close(self):
        self.db.close()


def sender(spool, port, token, batch_size, flush_seconds, hold_ack, stop, log, errors, active):
    queue = Outbox(spool)
    try:
        while not stop.is_set() or queue.count():
            rows = queue.next_batch(batch_size)
            if not rows:
                time.sleep(0.006)
                continue
            age = (time.time_ns() - rows[0][2]) / 1e9
            if len(rows) < batch_size and age < flush_seconds and not stop.is_set():
                time.sleep(min(0.006, flush_seconds - age))
                continue
            records = [json.loads(row[1]) for row in rows]
            status, node, reply, elapsed = request(port, "POST", "/api/readings", {"readings": records}, token)
            accepted, duplicates = reply.get("accepted"), reply.get("duplicates")
            valid = (status in (200, 201) and isinstance(accepted, int) and isinstance(duplicates, int)
                     and accepted + duplicates == len(rows))
            lost_ack = valid and hold_ack.is_set()
            if lost_ack:
                hold_ack.clear()
            if valid and not lost_ack:
                queue.acknowledge(rows, records)
            log.append({"t_s": round(time.perf_counter() - active, 4), "status": status, "node": node,
                        "records": len(rows), "http_ms": elapsed, "accepted": accepted, "duplicates": duplicates,
                        "ack_saved": int(valid and not lost_ack), "pending": queue.count()})
            if status in (400, 401, 403, 409, 413, 422) or (200 <= status < 300 and not valid):
                raise RuntimeError(f"non-retryable response: HTTP {status}, accepted={accepted}, duplicates={duplicates}")
            if not valid or lost_ack:
                time.sleep(0.1)
            if stop.is_set() and time.perf_counter() > stop.deadline:
                raise TimeoutError("outbox did not drain; its durable contents were retained")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        queue.close()
