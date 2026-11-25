"""Persistence for a synthetic firing log. The database has no network listener."""

import math
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


KINDS = ("heating", "holding", "cooling")
KILNS = tuple(f"K{number:02d}" for number in range(1, 7))
FIELDS = ("kiln_id", "firing_id", "sequence", "observed_at", "phase", "temperature_c")
UTC_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)")


class Conflict(Exception):
    """An existing observation key has a different published value."""


def stamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def connect(path, mode):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=" + mode,
                                 timeout=4, uri=True)
    connection.row_factory = sqlite3.Row
    if mode != "ro":
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def metadata(connection):
    return dict(connection.execute(
        "SELECT revision, updated_at FROM metadata WHERE id=1"
    ).fetchone())


def readiness(path):
    with closing(connect(path, "ro")) as connection:
        return metadata(connection)


def status_conn(connection):
    values = dict(connection.execute(
        "SELECT count(*) AS reading_count, max(observed_at) AS latest_observed_at FROM readings"
    ).fetchone())
    return {**metadata(connection), **values}


def status(path):
    with closing(connect(path, "ro")) as connection, connection:
        connection.execute("BEGIN")
        return status_conn(connection)


def seed_rows(now):
    for index, kiln in enumerate(KILNS):
        end = (0.30, 0.58, 0.91)[index % 3]
        peak = 1060 + 10 * index
        for sequence in range(1, 361):
            progress = end * (sequence - 1) / 359
            if progress < 0.47:
                phase, temp = "heating", 20 + (peak - 20) * progress / 0.47
            elif progress < 0.65:
                phase, temp = "holding", peak + 2 * math.sin(sequence / 6)
            else:
                phase, temp = "cooling", peak - (peak - 60) * (progress - 0.65) / 0.35
            yield (kiln, "demo-" + kiln, sequence, stamp(now - timedelta(minutes=360-sequence)),
                   phase, round(temp, 1))


def seed(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path, "rwc")) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), "
            "revision INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        if connection.execute("SELECT 1 FROM metadata WHERE id=1").fetchone():
            return status_conn(connection)
        connection.execute(
            "CREATE TABLE readings (kiln_id TEXT NOT NULL, firing_id TEXT NOT NULL, "
            "sequence INTEGER NOT NULL CHECK(sequence>=1), observed_at TEXT NOT NULL, "
            "phase TEXT NOT NULL, temperature_c REAL NOT NULL, "
            "PRIMARY KEY(kiln_id,firing_id,sequence)) WITHOUT ROWID"
        )
        now = datetime.now(timezone.utc)
        connection.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?)", seed_rows(now))
        connection.execute("INSERT INTO metadata VALUES (1,1,?)", (stamp(now),))
        return status_conn(connection)


def parse_batch(data):
    if not isinstance(data, dict) or set(data) != {"readings"}:
        raise ValueError("expected_readings_array")
    rows = data["readings"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        raise ValueError("batch_size_1_to_128")
    observations = []
    for item in rows:
        if not isinstance(item, dict) or set(item) != set(FIELDS):
            raise ValueError("invalid_reading_fields")
        kiln, firing, seq, observed, phase, temp = (item[key] for key in FIELDS)
        if kiln not in KILNS or phase not in KINDS:
            raise ValueError("unknown_kiln_or_phase")
        if not isinstance(firing, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", firing):
            raise ValueError("invalid_firing_id")
        if type(seq) is not int or not 1 <= seq <= 2**31-1:
            raise ValueError("invalid_sequence")
        if not isinstance(observed, str) or not UTC_FORMAT.fullmatch(observed):
            raise ValueError("invalid_utc_timestamp")
        try:
            observed = stamp(datetime.fromisoformat(observed.replace("Z", "+00:00")))
        except ValueError:
            raise ValueError("invalid_utc_timestamp") from None
        if type(temp) not in (int, float) or not -50 <= temp <= 1600 or not math.isfinite(temp):
            raise ValueError("invalid_temperature_c")
        observations.append((kiln, firing, seq, observed, phase, float(temp)))
    return observations


def ingest(path, rows):
    accepted = duplicates = 0
    with closing(connect(path, "rw")) as connection, connection:
        # Serialize lookup+insert across processes; a Conflict rolls back the whole batch.
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            known = connection.execute(
                "SELECT observed_at, phase, temperature_c FROM readings "
                "WHERE kiln_id=? AND firing_id=? AND sequence=?", row[:3]
            ).fetchone()
            if known is None:
                connection.execute("INSERT INTO readings VALUES (?,?,?,?,?,?)", row)
                accepted += 1
            elif tuple(known) == row[3:]:
                duplicates += 1
            else:
                raise Conflict()
        if accepted:
            connection.execute("UPDATE metadata SET revision=revision+1, updated_at=? WHERE id=1",
                               (stamp(),))
        response = {**metadata(connection), "accepted": accepted, "duplicates": duplicates}
    # The transaction context has committed (synchronous=FULL) before HTTP acknowledgment.
    return response


def summary(path):
    with closing(connect(path, "ro")) as connection, connection:
        connection.execute("BEGIN")
        info = metadata(connection)
        kilns = [dict(row) for row in connection.execute("""
            WITH ranked AS (
                SELECT kiln_id, firing_id, sequence, observed_at, phase, temperature_c,
                       ROW_NUMBER() OVER (
                           PARTITION BY kiln_id ORDER BY observed_at DESC, firing_id DESC, sequence DESC
                       ) AS position,
                       MAX(temperature_c) OVER (PARTITION BY kiln_id) AS peak_c,
                       COUNT(*) OVER (PARTITION BY kiln_id) AS reading_count
                  FROM readings
            )
            SELECT kiln_id, firing_id, sequence, observed_at, phase, temperature_c, peak_c, reading_count
              FROM ranked WHERE position = 1 ORDER BY kiln_id
        """)]
        return {**info, "simulated": True, "reading_count": sum(k["reading_count"] for k in kilns),
                "kilns": kilns}


def firings(path, kiln):
    with closing(connect(path, "ro")) as connection:
        return [row[0] for row in connection.execute(
            "SELECT firing_id FROM readings WHERE kiln_id=? GROUP BY firing_id "
            "ORDER BY max(observed_at) DESC, firing_id LIMIT 100", (kiln,)
        )]


def series(path, kiln, firing):
    # Keep the last 720 measurements by original observation time, including late backfill.
    with closing(connect(path, "ro")) as connection:
        rows = connection.execute(
            "SELECT sequence, observed_at, temperature_c FROM readings "
            "WHERE kiln_id=? AND firing_id=? ORDER BY observed_at DESC, sequence DESC LIMIT 720",
            (kiln, firing)
        ).fetchall()
    return {"kiln_id": kiln, "firing_id": firing, "points": [dict(r) for r in reversed(rows)]}


def copy_snapshot(source_path, destination_path, restore):
    target = Path(destination_path).resolve()
    if Path(source_path).resolve() == target:
        raise ValueError("source and destination must differ")
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with closing(connect(source_path, "ro")) as source:
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("source failed integrity check")
            status_conn(source)
            if restore:
                dest = target
            else:
                fd, name = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
                os.close(fd)
                dest = staged = Path(name)
            with closing(connect(dest, "rwc")) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("destination failed integrity check")
                info = status_conn(destination)
        if staged:
            os.replace(staged, target)
        return info
    finally:
        if staged:
            staged.unlink(missing_ok=True)
