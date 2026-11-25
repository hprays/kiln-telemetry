"""Loopback gateway. Public routes are explicitly enumerated."""

import argparse
import http.client
import threading

from httpbase import Handler, Listener, PUBLIC, SERIES_ROUTE


def fetch(port, path, timeout, payload=None, authorization=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
    try:
        connection.connect()
        connection.sock.settimeout(timeout)
        headers = {"Connection": "close"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if authorization:
                headers["Authorization"] = authorization
        connection.request("GET" if payload is None else "POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        forwarded = {key: response.getheader(key) for key in ("X-Cache", "X-Node")
                     if response.getheader(key)}
        return response.status, response.read(), response.getheader("Content-Type"), forwarded
    finally:
        connection.close()


class Backends:
    def __init__(self, ports):
        self.ports = ports
        self.healthy = set()
        self.lock = threading.Lock()
        self.offset = 0
        self.stop = threading.Event()

    def update(self, port, live):
        with self.lock:
            if live:
                self.healthy.add(port)
            else:
                self.healthy.discard(port)

    def candidates(self):
        with self.lock:
            ports = [p for p in self.ports if p in self.healthy]
            if not ports:
                return []
            offset = self.offset % len(ports)
            self.offset = offset + 1
            return ports[offset:] + ports[:offset]

    def probe(self):
        for port in self.ports:
            try:
                code, *_ = fetch(port, "/health", .75)
                self.update(port, code == 200)
            except (OSError, http.client.HTTPException):
                self.update(port, False)

    def watch(self):
        while not self.stop.wait(.5):
            self.probe()


class Gateway(Handler):
    def forward(self, path, body=None, authorization=None):
        pool = self.server.backends
        for port in pool.candidates():
            try:
                # Even if the previous receiver committed before disconnecting,
                # retrying the same immutable observations is safe.
                code, data, mime, headers = fetch(port, path, 5, body, authorization)
                if code < 500:
                    self.reply(code, data, mime, headers)
                    return
            except (OSError, http.client.HTTPException):
                pass
            pool.update(port, False)
        self.reject(503, "no_ready_backend")

    def do_GET(self):
        path = self.path.partition("?")[0]
        if path not in PUBLIC and not SERIES_ROUTE.fullmatch(path):
            self.reject(403, "path_denied")
            return
        self.forward(path)

    def do_POST(self):
        if self.path != "/api/readings":
            self.reject(403, "path_denied")
            return
        supplied = self.headers.get_all("Authorization") or []
        if len(supplied) != 1 or not supplied[0].startswith("Bearer "):
            self.reject(401, "producer_auth_required")
            return
        body = self.read_body()
        if body is not None:
            self.forward(self.path, body, supplied[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--backends", required=True)
    args = parser.parse_args()
    try:
        ports = [int(value) for value in args.backends.split(",")]
    except ValueError:
        parser.error("expected comma-separated backend ports")
    if any(p < 1 or p > 65535 for p in ports + [args.port]):
        parser.error("ports must be 1..65535")
    if len(set(ports)) != len(ports) or args.port in ports:
        parser.error("ports must be unique")
    pool = Backends(ports)
    with Listener(("127.0.0.1", args.port), Gateway) as httpd:
        httpd.backends = pool
        pool.probe()
        thread = threading.Thread(target=pool.watch, daemon=True)
        thread.start()
        try:
            httpd.serve_forever()
        finally:
            pool.stop.set()
            thread.join()


if __name__ == "__main__":
    main()
