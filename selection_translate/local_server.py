from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger("LiveTranslate.LocalAPI")


class LocalAPIServer:
    def __init__(
        self,
        translate_callback: Callable[[dict], dict],
        host: str = "127.0.0.1",
        port: int = 17891,
        token: str | None = None,
    ):
        self._translate_callback = translate_callback
        self._host = host
        self._port = int(port)
        self._token = token or ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self):
        if self._server is not None:
            return

        callback = self._translate_callback
        api_server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LiveTranslateLocalAPI/0.1"

            def do_OPTIONS(self):
                self._send_empty(204)

            def do_GET(self):
                if self.path == "/api/health":
                    self._send_json(
                        {
                            "ok": True,
                            "service": "LiveTranslate",
                            "token_required": bool(api_server._token),
                            "endpoints": ["/api/translate-selection"],
                        }
                    )
                    return
                self._send_json({"error": "not found"}, status=404)

            def do_POST(self):
                if self.path != "/api/translate-selection":
                    self._send_json({"error": "not found"}, status=404)
                    return
                if (
                    api_server._token
                    and self.headers.get("X-LiveTranslate-Token") != api_server._token
                ):
                    self._discard_request_body()
                    self._send_json({"error": "unauthorized"}, status=401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 200_000:
                        self._send_json({"error": "invalid request body"}, status=400)
                        return
                    raw = self.rfile.read(length)
                    payload = json.loads(raw.decode("utf-8"))
                    if not isinstance(payload, dict):
                        self._send_json({"error": "request body must be a JSON object"}, status=400)
                        return
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        self._send_json({"error": "text is required"}, status=400)
                        return
                    payload["text"] = text
                    mode = payload.get("mode")
                    if mode is not None and mode not in {
                        "literal",
                        "natural",
                        "explain",
                        "polish",
                    }:
                        self._send_json({"error": "unsupported mode"}, status=400)
                        return
                    result = callback(payload)
                    self._send_json(result)
                except json.JSONDecodeError:
                    self._send_json({"error": "invalid JSON"}, status=400)
                except ValueError as e:
                    self._send_json({"error": str(e)}, status=400)
                except Exception as e:
                    log.warning("Local API translate failed: %s", e)
                    self._send_json({"error": str(e)}, status=500)

            def log_message(self, fmt, *args):
                log.debug("%s - %s", self.address_string(), fmt % args)

            def _send_empty(self, status: int):
                self.send_response(status)
                self._send_common_headers()
                self.end_headers()

            def _send_json(self, data: dict, status: int = 200):
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self._send_common_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_common_headers(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Content-Type, X-LiveTranslate-Token",
                )

            def _discard_request_body(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length > 0:
                    self.rfile.read(min(length, 200_000))

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="LiveTranslateLocalAPI",
            daemon=True,
        )
        self._thread.start()
        log.info("Local API listening on %s", self.url)

    def set_token(self, token: str | None):
        self._token = token or ""
        log.info("Local API token %s", "enabled" if self._token else "disabled")

    def stop(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)
        log.info("Local API stopped")
        self._server = None
        self._thread = None
