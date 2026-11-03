"""Self-contained loopback browser API for pump control."""

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import can

STATIC_DIR = Path(__file__).with_name("static")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    device = None
    server_version = "CirculationPump/1.0"

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            self._api(lambda: self._get(parsed.path, parsed.query))
        else:
            self._static(parsed.path)

    def do_POST(self):
        parsed = urlsplit(self.path)
        self._api(lambda: self._post(parsed.path, self._body()))

    def _api(self, operation):
        try:
            self._json(200, operation())
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
        except can.CanError as exc:
            self._json(503, {"error": f"CAN error: {exc}"})
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _json(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _get(self, path, query):
        if path == "/api/state":
            return self.device.state()
        raise ApiError("Unknown endpoint", 404)

    def _post(self, path, body):
        if path == "/api/scan":
            return self.device.scan(body.get("seconds", 3))
        elif path == "/api/heartbeat":
            self.device.heartbeat()
        elif path == "/api/arm":
            if body.get("armed") is True and body.get("confirmed") is not True:
                raise ApiError("Confirm bench setup before arming", 403)
            self.device.arm(body.get("armed"))
        elif path == "/api/stop":
            if "pump" in body:
                self.device.manual(body["pump"], 0)
            else:
                self.device.stop_all()
        elif path == "/api/manual":
            self.device.manual(body.get("pump"), body.get("duty"))
        elif path == "/api/sequence":
            legacy_dwell = body.get("dwell")
            return self.device.start_sequence(
                body.get("pump"), body.get("start", 40), body.get("end", 80), body.get("step", 5),
                body.get("low_time", legacy_dwell if legacy_dwell is not None else 5),
                body.get("high_time", legacy_dwell if legacy_dwell is not None else 5),
                body.get("step_time", legacy_dwell if legacy_dwell is not None else 5),
                body.get("repeats", 1), body.get("return_trip", False))
        else:
            raise ApiError("Unknown endpoint", 404)
        return {"ok": True}

    def _body(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ApiError("Cross-site requests are not allowed", 403)
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            raise ApiError("Cross-origin requests are not allowed", 403)
        if self.headers.get_content_type() != "application/json":
            raise ApiError("Expected application/json", 415)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("Invalid request body size") from None
        if not 0 < length <= 1 << 20:
            raise ApiError("Invalid request body size")
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError("Body is not valid JSON") from None
        if not isinstance(body, dict):
            raise ApiError("Body must be a JSON object")
        return body

    def _static(self, path):
        relative = "index.html" if path == "/" else unquote(path.lstrip("/"))
        target = (STATIC_DIR / relative).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            self._json(404, {"error": "Not found"})
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format_string, *args):
        return


def make_server(device, host="127.0.0.1", port=8326):
    handler = type("PumpHandler", (Handler,), {"device": device})
    return ThreadingHTTPServer((host, port), handler)