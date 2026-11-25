"""Kiln telemetry receiver, dashboard and local-only maintenance commands."""

import argparse
import csv
import hmac
import json
import math
import os
import secrets
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import storage
from httpbase import Handler, Listener, SERIES_ROUTE, encode


ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/screen.css": ("screen.css", "text/css; charset=utf-8"),
          "/screen.js": ("screen.js", "text/javascript; charset=utf-8")}


def make_token(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token_bytes(path)
        return {"token_file_ready": True}
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            # Apply a user-only discretionary ACL to the empty file before storing its value.
            if os.name == "nt":
                identity = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"])
                sid = next(csv.reader([identity.decode("ascii", errors="replace").strip()]))[-1]
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", "*" + sid + ":F"],
                               check=True, stdout=subprocess.DEVNULL)
            stream.write(secrets.token_urlsafe(32))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"token_file_ready": True}


def token_bytes(path):
    value = Path(path).read_bytes().strip()
    if not value.isascii() or not 32 <= len(value) <= 256:
        raise ValueError("invalid producer token file")
    return b"Bearer " + value


class Telemetry:
    def __init__(self, db, node, ttl, token):
        self.db, self.node, self.ttl, self.token = db, node, ttl, token
        self.mutex = threading.Lock()
        self.cache_mutex = threading.Lock()
        self.cached, self.until, self.version = b"", 0, None
        self.counters = dict(requests=0, cache_hits=0, cache_misses=0, origin_reads=0,
                             origin_errors=0, accepted=0, duplicates=0, conflicts=0, denied=0)
        self.origin_seconds = 0.0

    def inc(self, **counts):
        with self.mutex:
            for key, count in counts.items():
                self.counters[key] += count

    def metrics(self):
        with self.mutex:
            return {**self.counters, "origin_seconds": round(self.origin_seconds, 6),
                    "node": self.node, "cache_seconds": self.ttl}

    def origin(self):
        self.inc(cache_misses=1, origin_reads=1)
        start = time.perf_counter()
        try:
            data = storage.summary(self.db)
            return encode(data), (data["revision"], data["updated_at"])
        except sqlite3.Error:
            self.inc(origin_errors=1)
            raise
        finally:
            with self.mutex:
                self.origin_seconds += time.perf_counter() - start

    def get_summary(self):
        self.inc(requests=1)
        if not self.ttl:
            return self.origin()[0], "MISS"
        with self.cache_mutex:
            if time.monotonic() < self.until:
                # A cheap metadata lookup invalidates stale entries across replicas and restore.
                current = storage.readiness(self.db)
                if (current["revision"], current["updated_at"]) == self.version:
                    self.inc(cache_hits=1)
                    return self.cached, "HIT"
            self.cached, self.version = self.origin()
            self.until = time.monotonic() + self.ttl
            return self.cached, "MISS"


class Receiver(Handler):
    def reply(self, code, body, mime="application/json; charset=utf-8", headers=None):
        super().reply(code, body, mime, {**(headers or {}), "X-Node": self.server.api.node})

    def do_GET(self):
        api = self.server.api
        target = self.path.partition("?")[0]
        try:
            if target == "/api/summary":
                body, cache = api.get_summary()
                self.reply(200, body, headers={"X-Cache": cache})
            elif match := SERIES_ROUTE.fullmatch(target):
                kiln, firing = match.groups()
                if firing is None:
                    self.reply(200, encode({"firings": storage.firings(api.db, kiln)}))
                else:
                    data = storage.series(api.db, kiln, firing)
                    if data["points"]:
                        self.reply(200, encode(data))
                    else:
                        self.reject(404, "unknown_firing")
            elif target == "/health":
                self.reply(200, encode({"status": "ok", "node": api.node, **storage.readiness(api.db)}))
            elif target == "/metrics":
                self.reply(200, encode(api.metrics()))
            elif target in self.server.assets:
                body, mime = self.server.assets[target]
                self.reply(200, body, mime)
            else:
                self.reject(404, "not_found")
        except sqlite3.Error:
            self.reject(503, "database_unavailable")

    def do_POST(self):
        api = self.server.api
        if self.path != "/api/readings":
            self.reject(404, "not_found")
            return
        supplied = self.headers.get_all("Authorization") or []
        if len(supplied) != 1 or not hmac.compare_digest(
                supplied[0].encode("latin-1"), api.token):
            api.inc(denied=1)
            self.reject(401, "producer_auth_required")
            return
        body = self.read_body()
        if body is None:
            return
        try:
            rows = storage.parse_batch(json.loads(body.decode("utf-8")))
            result = storage.ingest(api.db, rows)
        except (ValueError, RecursionError):
            self.reject(422, "invalid_reading_batch")
            return
        except storage.Conflict:
            api.inc(conflicts=1)
            self.reject(409, "observation_key_conflict")
            return
        except sqlite3.Error:
            self.reject(503, "database_unavailable")
            return
        api.inc(accepted=result["accepted"], duplicates=result["duplicates"])
        self.reply(201 if result["accepted"] else 200, encode(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("token").add_argument("--file", required=True)
    for action in ("seed", "status", "backup", "restore", "serve"):
        cmd = actions.add_parser(action)
        cmd.add_argument("--db", required=True)
        if action == "backup":
            cmd.add_argument("--to", required=True)
        if action == "restore":
            cmd.add_argument("--from", dest="source", required=True)
        if action == "serve":
            cmd.add_argument("--port", type=int, required=True)
            cmd.add_argument("--node", required=True)
            cmd.add_argument("--cache-seconds", type=float, default=4)
            cmd.add_argument("--token-file", required=True)
    args = parser.parse_args()
    if args.action == "serve":
        if not math.isfinite(args.cache_seconds) or args.cache_seconds < 0:
            parser.error("cache seconds must be finite and nonnegative")
        if not args.node.isascii() or not args.node.isprintable():
            parser.error("invalid node name")
        storage.readiness(args.db)
        api = Telemetry(args.db, args.node, args.cache_seconds, token_bytes(args.token_file))
        base = Path(__file__).resolve().parent
        with Listener(("127.0.0.1", args.port), Receiver) as service:
            service.api = api
            service.assets = {url: ((base / file).read_bytes(), mime)
                              for url, (file, mime) in ASSETS.items()}
            service.serve_forever()
        return
    if args.action == "token":
        output = make_token(args.file)
    elif args.action == "backup":
        output = storage.copy_snapshot(args.db, args.to, restore=False)
    elif args.action == "restore":
        output = storage.copy_snapshot(args.source, args.db, restore=True)
    elif args.action == "seed":
        output = storage.seed(args.db)
    else:
        output = storage.status(args.db)
    print(json.dumps(output))


if __name__ == "__main__":
    main()
