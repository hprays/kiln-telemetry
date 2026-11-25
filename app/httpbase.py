import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAX_BODY = 65536
PUBLIC = {"/", "/screen.css", "/screen.js", "/api/summary", "/health"}
SERIES_ROUTE = re.compile(r"/api/series/(K0[1-6])(?:/([a-zA-Z0-9_-]{1,64}))?")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Listener(ThreadingHTTPServer):
    request_queue_size = 128


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KilnLab/1"
    sys_version = ""

    def reply(self, code, body, mime="application/json; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (ConnectionError, TimeoutError):
                pass

    def reject(self, status, reason):
        self.close_connection = True
        self.reply(status, encode({"error": reason}))

    def read_body(self):
        if self.headers.get("Transfer-Encoding"):
            self.reject(400, "unsupported_transfer_encoding")
            return None
        fields = self.headers.get_all("Content-Length") or []
        if len(fields) != 1 or not fields[0].isascii() or not fields[0].isdigit() or len(fields[0]) > 12:
            self.reject(411, "require_content_length")
            return None
        length = int(fields[0])
        if length > MAX_BODY:
            self.reject(413, "body_limit_exceeded")
            return None
        if self.headers.get_content_type() != "application/json":
            self.reject(415, "require_json")
            return None
        self.connection.settimeout(4)
        try:
            value = self.rfile.read(length)
        except OSError:
            self.reject(408, "request_timeout")
            return None
        if len(value) != length:
            self.reject(400, "incomplete_body")
            return None
        return value

    def do_HEAD(self):
        self.reject(405, "method_denied")

    do_PUT = do_HEAD
    do_PATCH = do_HEAD
    do_DELETE = do_HEAD
    do_OPTIONS = do_HEAD
    do_TRACE = do_HEAD
    do_CONNECT = do_HEAD

    def log_message(self, fmt, *args):
        pass
