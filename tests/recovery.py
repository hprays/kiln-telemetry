"""Recover a real online backup into a new local database and verify its HTTP read path."""
import argparse
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from measure import ROOT, STATE, readback, request, save_result, sensor_readings, utc_now


def operator(action, database, *arguments):
    output = subprocess.check_output(
        [sys.executable, "-B", str(ROOT / "app/server.py"), action, "--db", str(database),
         *map(str, arguments)], timeout=40)
    return json.loads(output)


def fingerprint(db):
    digest = hashlib.sha256()
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        checked = connection.execute("PRAGMA integrity_check").fetchall()
        if checked != [("ok",)]:
            raise RuntimeError("restored SQLite integrity check failed")
        for row in connection.execute(
            "SELECT kiln_id, firing_id, sequence, observed_at, phase, temperature_c FROM readings "
            "ORDER BY kiln_id, firing_id, sequence"):
            digest.update(json.dumps(row, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def recovery(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "summary.json").exists():
        raise FileExistsError("use a fresh evidence directory")
    scratch = ROOT / ".runs"
    scratch.mkdir(exist_ok=True)
    primary = STATE / "data.sqlite3"
    credential = STATE / "credential/sensor.token"
    token = credential.read_text(encoding="ascii").strip()
    key = "dr-" + uuid.uuid4().hex[:12]
    timeline = []
    def mark(event):
        timeline.append({"event": event, "utc": utc_now()})

    def write(sequence):
        rows = sensor_readings(key, sequence, utc_now())
        code, _, receipt, _ = request(args.port, "POST", "/api/readings", {"readings": rows}, token)
        if code != 201 or receipt.get("accepted") != len(rows):
            raise RuntimeError("could not commit the drill markers")
        return receipt

    with tempfile.TemporaryDirectory(prefix="recovery-", dir=scratch) as temporary:
        backup = STATE / "backups/checkpoint.sqlite3"
        if backup.exists():
            # Preserve a pre-existing operator checkpoint on later independent runs.
            backup = Path(temporary) / "checkpoint.sqlite3"
        restore_to = Path(temporary) / "recovered.sqlite3"
        mark("pre_checkpoint_marker_committed_after_event")
        before = write(1)
        mark("online_backup_started")
        snapshot = operator("backup", primary, "--to", backup)
        if snapshot["revision"] != before["revision"]:
            raise RuntimeError("run the drill without concurrent sensor writers")
        expected_hash = fingerprint(backup)
        mark("backup_verified")
        time.sleep(1)
        after = write(2)
        mark("after_checkpoint_marker_committed")
        recovery_clock = time.perf_counter()
        mark("primary_declared_unavailable_for_recovery_exercise")
        incident = timeline[-1]["utc"]
        restored = operator("restore", restore_to, "--from", backup)
        mark("sqlite_restore_completed")
        with socket.socket() as vacant:
            vacant.bind(("127.0.0.1", 0))
            recovery_port = vacant.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "app/server.py"), "serve", "--db", str(restore_to),
             "--port", str(recovery_port), "--node", "recovery", "--cache-seconds", "0", "--token-file", str(credential)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and process.poll() is None:
                code, node, payload, _ = request(recovery_port, "GET", "/api/summary", timeout=.35)
                if (code == 200 and node == "recovery" and payload.get("revision") == snapshot["revision"]
                        and payload.get("reading_count") == snapshot["reading_count"]):
                    break
                time.sleep(.03)
            else:
                raise RuntimeError("fresh recovery API failed its snapshot read check")
            rto = time.perf_counter() - recovery_clock
            mark("verified_correct_recovered_api_response")
            recovered_hash = fingerprint(restore_to)
            expected = {(f"K{i:02d}", 1) for i in range(1, 7)}
            check = readback(restore_to, key, expected)
            primary_check = readback(primary, key, expected | {(f"K{i:02d}", 2) for i in range(1, 7)})
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        rpo = (datetime.fromisoformat(incident.replace("Z", "+00:00")) -
               datetime.fromisoformat(before["updated_at"].replace("Z", "+00:00"))).total_seconds()
        result = {
            "kind": "isolated_restore_from_live_primary", "start_utc": timeline[0]["utc"], "end_utc": utc_now(),
            "scope": "RTO ends at validated recovery API response; DNS/client cutover and detection are excluded.",
            "checkpoint_stored_in_terraform_backup_dir": backup.parent == STATE / "backups",
            "snapshot_count": snapshot["reading_count"], "restored_count": restored["reading_count"],
            "checkpoint_revision": snapshot["revision"], "last_committed_primary_revision": after["revision"],
            "recovered_revision": restored["revision"], "checkpoint_bytes": backup.stat().st_size,
            "rpo_s": round(rpo, 4), "rpo_definition": "declared failure UTC minus newest committed checkpoint UTC",
            "rto_s": round(rto, 4), "post_checkpoint_rows_lost_in_recovery_copy": 12 - check["stored_rows"],
            "checkpoint_logical_sha256": expected_hash, "recovered_logical_sha256": recovered_hash,
            "sqlite_integrity_check": "ok", "marker_check": check, "primary_marker_check": primary_check,
            "timeline": timeline,
        }
    save_result(out, result)
    if (recovered_hash != expected_hash or check["missing"] or check["unexpected"] or check["duplicate_rows"]
            or primary_check["missing"] or primary_check["unexpected"]):
        raise RuntimeError("restored data differs from the checkpoint")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18340)
    parser.add_argument("--out", required=True)
    recovery(parser.parse_args())
