from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr_providers.openai_audio import OpenAIAudioASRProvider  # noqa: E402
from history import HistoryStore  # noqa: E402
from selection_translate.local_server import LocalAPIServer  # noqa: E402
from selection_translate.service import SelectionTranslateService  # noqa: E402
from translator import Translator  # noqa: E402


class FakeOpenAIServer:
    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)

    @staticmethod
    def _handler():
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b""
                if self.path == "/v1/chat/completions":
                    payload = json.loads(body.decode("utf-8"))
                    user_text = ""
                    for msg in payload.get("messages", []):
                        if msg.get("role") == "user":
                            user_text = msg.get("content", "")
                    self._send_json(
                        {
                            "id": "chatcmpl-runtime-acceptance",
                            "object": "chat.completion",
                            "model": payload.get("model", "fake-model"),
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": (
                                            "自然翻译：本地验收通过\n\n"
                                            "解释：fake-openai received "
                                            f"{user_text[:20]}\n\n"
                                            "更自然/更适合复制的表达：验收通过"
                                        ),
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 3,
                                "completion_tokens": 5,
                                "total_tokens": 8,
                            },
                        }
                    )
                    return
                if self.path == "/v1/audio/transcriptions":
                    self._send_json({"text": "runtime audio accepted", "language": "en"})
                    return
                self._send_json({"error": "not found"}, status=404)

            def log_message(self, _fmt, *_args):
                return

            def _send_json(self, payload: dict, status: int = 200):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def check_openai_compatible_llm(base_url: str):
    translator = Translator(
        api_base=base_url,
        api_key="test-key",
        model="fake-chat",
        streaming=False,
        mode="explain",
    )
    result = translator.translate("The proposal is still up in the air.", "en")
    assert "本地验收通过" in result
    assert translator.last_usage == (3, 5)


def check_openai_audio_asr(base_url: str):
    provider = OpenAIAudioASRProvider(
        base_url=base_url,
        api_key="test-key",
        model="fake-audio",
        language="en",
        timeout=5,
    )
    result = provider.transcribe(np.zeros(1600, dtype=np.float32))
    assert result is not None
    assert result["text"] == "runtime audio accepted"
    assert result["language"] == "en"


def check_selection_local_api(base_url: str):
    with tempfile.TemporaryDirectory() as tmp:
        history = HistoryStore(Path(tmp) / "history.db")
        service = SelectionTranslateService(
            get_model_config=lambda: {
                "name": "fake-chat",
                "provider": "openai-compatible",
                "api_base": base_url,
                "api_key": "test-key",
                "model": "fake-chat",
            },
            get_target_language=lambda: "zh",
            get_timeout=lambda: 5,
            history_store=history,
            get_history_enabled=lambda: True,
        )
        local_api = LocalAPIServer(
            translate_callback=lambda payload: service.translate(
                text=payload["text"],
                source=payload.get("source", "runtime-acceptance"),
                mode=payload.get("mode", "explain"),
            ),
            port=0,
            token="runtime-token",
        )
        local_api.start()
        try:
            port = local_api._server.server_address[1]
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/translate-selection",
                data=json.dumps(
                    {
                        "text": "The proposal is still up in the air.",
                        "source": "chrome-extension",
                        "mode": "explain",
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-LiveTranslate-Token": "runtime-token",
                },
                method="POST",
            )
            response = urllib.request.urlopen(request, timeout=10)
            payload = json.loads(response.read().decode("utf-8"))
            assert payload["translation"] == "本地验收通过"
            rows = history.list_history("selection")
            assert len(rows) == 1
            assert rows[0]["translated_text"] == "本地验收通过"
        finally:
            local_api.stop()


def main() -> int:
    server = FakeOpenAIServer()
    server.start()
    try:
        check_openai_compatible_llm(server.base_url)
        print("OK check_openai_compatible_llm")
        check_openai_audio_asr(server.base_url)
        print("OK check_openai_audio_asr")
        check_selection_local_api(server.base_url)
        print("OK check_selection_local_api")
    finally:
        server.stop()
    print("runtime acceptance passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
