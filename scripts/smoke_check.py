from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audio_providers.macos_placeholder import (  # noqa: E402
    MACOS_AUDIO_MESSAGE,
    MacOSAudioCaptureProviderPlaceholder,
)
from asr_providers import ASRProviderConfig, resolve_device  # noqa: E402
from asr_providers.assemblyai import AssemblyAIASRProvider  # noqa: E402
from asr_providers.assemblyai_streaming import AssemblyAIStreamingASRProvider  # noqa: E402
from asr_providers.factory import create_asr_provider  # noqa: E402
from asr_providers.openai_audio import OpenAIAudioASRProvider  # noqa: E402
from diagnostics import create_diagnostic_bundle  # noqa: E402
from exporters import export_subtitles, subtitle_rows_to_export_items  # noqa: E402
from history import HistoryStore  # noqa: E402
from logging_utils import redact_secret_data, redact_secret_text  # noqa: E402
import model_manager  # noqa: E402
import runtime_paths  # noqa: E402
from scripts import package_preflight  # noqa: E402
from scripts.verify_build_output import verify_build_output  # noqa: E402
from providers.base import TranslateInput, TranslateResult  # noqa: E402
from providers.ollama import OllamaProvider  # noqa: E402
from providers.openai_compatible import readable_provider_error  # noqa: E402
from selection_translate.hotkey_parser import (  # noqa: E402
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    parse_hotkey,
)
from selection_translate.local_server import LocalAPIServer  # noqa: E402
from selection_translate.service import SelectionTranslateService  # noqa: E402
from settings_utils import should_persist_model_config  # noqa: E402
from transcript_writer import TranscriptWriter  # noqa: E402
from translator import DEFAULT_PROMPT, PROMPT_PRESETS, Translator  # noqa: E402


def check_manifest():
    manifest = json.loads((ROOT / "browser-extension" / "manifest.json").read_text("utf-8"))
    assert manifest["manifest_version"] == 3
    assert "contextMenus" in manifest["permissions"]
    assert "storage" in manifest["permissions"]
    assert "http://127.0.0.1:17891/*" in manifest["host_permissions"]

    background = (ROOT / "browser-extension" / "background.js").read_text("utf-8")
    popup = (ROOT / "browser-extension" / "popup.js").read_text("utf-8")
    assert "X-LiveTranslate-Token" in background
    assert "formatApiError" in background
    assert "localApiToken" in background
    assert "localApiToken" in popup
    assert "/api/health" in popup


def check_package_preflight():
    package_preflight.check_required_paths()
    package_preflight.check_default_config_has_no_secrets()
    package_preflight.check_build_script_resources()
    package_preflight.check_docs_have_no_obvious_secrets()
    package_preflight.check_manifest_json()


def check_build_output_verifier():
    with tempfile.TemporaryDirectory() as tmp:
        dist = Path(tmp)
        for path in (
            "LiveTranslate.exe",
            "_internal/config.yaml",
            "_internal/assets",
            "_internal/i18n",
            "_internal/prompts",
            "_internal/funasr_nano",
            "_internal/browser-extension",
            "_internal/docs",
            "_internal/screenshot",
        ):
            target = dist / path
            if "." in target.name:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x", encoding="utf-8")
            else:
                target.mkdir(parents=True, exist_ok=True)
        verify_build_output(dist)
        (dist / "_internal" / "user_settings.json").write_text("{}", encoding="utf-8")
        try:
            verify_build_output(dist)
        except AssertionError as exc:
            assert "user data" in str(exc)
        else:
            raise AssertionError("Build verifier should reject user settings")


def check_log_redaction():
    redacted = redact_secret_text(
        "api_key=sk-1234567890abcdef token: local-secret "
        "Authorization: Bearer sk-live-secret-123456"
    )
    assert "sk-1234567890abcdef" not in redacted
    assert "local-secret" not in redacted
    assert "sk-live-secret-123456" not in redacted
    assert "sk-1****cdef" in redacted

    redacted_data = redact_secret_data(
        {
            "asr_api": {"api_key": "sk-asr-secret-123456", "model": "whisper-1"},
            "local_api": {"token": "local-token"},
            "nested": [{"authorization": "Bearer abcdefghijklmnop"}],
        }
    )
    assert redacted_data["asr_api"]["api_key"] == "****"
    assert redacted_data["local_api"]["token"] == "****"
    assert redacted_data["nested"][0]["authorization"] == "****"
    assert redacted_data["asr_api"]["model"] == "whisper-1"


def check_diagnostic_bundle():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        resources = root / "resources"
        data_root = root / "runtime-data"
        resources.mkdir()
        data_root.mkdir()
        (resources / "browser-extension").mkdir()
        (resources / "prompts").mkdir()
        (data_root / "logs").mkdir()
        (data_root / "data").mkdir()
        (resources / "config.yaml").write_text(
            "translation:\n  api_key: sk-config-secret\nlocal_api:\n  token: local-token\n",
            encoding="utf-8",
        )
        (data_root / "user_settings.json").write_text(
            json.dumps(
                {
                    "models": [{"api_key": "sk-user-secret", "name": "demo"}],
                    "local_api": {"token": "user-token"},
                }
            ),
            encoding="utf-8",
        )
        (data_root / "logs" / "livetrans.log").write_text(
            "api_key=sk-log-secret token: log-token",
            encoding="utf-8",
        )
        (data_root / "data" / "history.db").write_text("private history", encoding="utf-8")
        (resources / "browser-extension" / "manifest.json").write_text("{}", encoding="utf-8")

        bundle = create_diagnostic_bundle(
            root / "diag.zip",
            project_root=data_root,
            resource_base=resources,
        )
        with zipfile.ZipFile(bundle) as zf:
            names = set(zf.namelist())
            summary = json.loads(zf.read("diagnostics/summary.json").decode("utf-8"))
            combined = "\n".join(
                zf.read(name).decode("utf-8", errors="replace")
                for name in names
            )
        assert "diagnostics/summary.json" in names
        assert "diagnostics/config.redacted.yaml" in names
        assert "diagnostics/user_settings.redacted.json" in names
        assert "diagnostics/logs/livetrans.log" in names
        assert summary["data_root"] == str(data_root)
        assert summary["resource_root"] == str(resources)
        assert summary["paths"]["config"]["path"] == str(resources / "config.yaml")
        assert summary["paths"]["user_settings"]["path"] == str(data_root / "user_settings.json")
        assert "audio_provider" in summary
        assert "packaged" in summary
        assert "models" in summary["paths"]
        assert "dist" in summary["paths"]
        assert "history.db" not in names
        assert "sk-config-secret" not in combined
        assert "sk-user-secret" not in combined
        assert "sk-log-secret" not in combined
        assert "local-token" not in combined
        assert "user-token" not in combined
        assert "log-token" not in combined


def check_diagnostic_bundle_legacy_project_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "logs").mkdir()
        (root / "data").mkdir()
        (root / "browser-extension").mkdir()
        (root / "prompts").mkdir()
        (root / "config.yaml").write_text(
            "translation:\n  api_key: sk-config-secret\nlocal_api:\n  token: local-token\n",
            encoding="utf-8",
        )
        (root / "user_settings.json").write_text(
            json.dumps(
                {
                    "models": [{"api_key": "sk-user-secret", "name": "demo"}],
                    "local_api": {"token": "user-token"},
                }
            ),
            encoding="utf-8",
        )
        (root / "logs" / "livetrans.log").write_text(
            "api_key=sk-log-secret token: log-token",
            encoding="utf-8",
        )
        (root / "data" / "history.db").write_text("private history", encoding="utf-8")
        (root / "browser-extension" / "manifest.json").write_text("{}", encoding="utf-8")

        bundle = create_diagnostic_bundle(root / "diag.zip", project_root=root)
        with zipfile.ZipFile(bundle) as zf:
            names = set(zf.namelist())
            summary = json.loads(zf.read("diagnostics/summary.json").decode("utf-8"))
            combined = "\n".join(
                zf.read(name).decode("utf-8", errors="replace")
                for name in names
            )
        assert "diagnostics/summary.json" in names
        assert "diagnostics/config.redacted.yaml" in names
        assert "diagnostics/user_settings.redacted.json" in names
        assert "diagnostics/logs/livetrans.log" in names
        assert "audio_provider" in summary
        assert "packaged" in summary
        assert "models" in summary["paths"]
        assert "dist" in summary["paths"]
        assert "history.db" not in names
        assert "sk-config-secret" not in combined
        assert "sk-user-secret" not in combined
        assert "sk-log-secret" not in combined
        assert "local-token" not in combined
        assert "user-token" not in combined
        assert "log-token" not in combined


def check_hotkey_parser():
    mods, vk, label = parse_hotkey("Ctrl+Shift+T")
    assert mods == (MOD_CONTROL | MOD_SHIFT)
    assert vk == ord("T")
    assert label == "CTRL+SHIFT+T"
    mods, vk, label = parse_hotkey("Alt+F2")
    assert mods == MOD_ALT
    assert vk == 0x71
    assert label == "ALT+F2"


def check_audio_placeholder():
    provider = MacOSAudioCaptureProviderPlaceholder()
    assert provider.get_audio() is None
    try:
        provider.start()
    except NotImplementedError as exc:
        assert MACOS_AUDIO_MESSAGE in str(exc)
    else:
        raise AssertionError("macOS placeholder should raise NotImplementedError")


def check_cache_path_env():
    with tempfile.TemporaryDirectory() as tmp:
        model_manager.apply_cache_env(tmp)
        assert model_manager.MODELS_DIR == Path(tmp)
        assert model_manager.get_models_dir() == Path(tmp)
        assert model_manager.os.environ["MODELSCOPE_CACHE"] == str(Path(tmp) / "modelscope")
        assert model_manager.os.environ["HF_HOME"] == str(Path(tmp) / "huggingface")
        assert model_manager.os.environ["TORCH_HOME"] == str(Path(tmp) / "torch")
        with tempfile.TemporaryDirectory() as tmp2:
            model_manager.set_models_dir(tmp2)
            assert model_manager.get_models_dir() == Path(tmp2)
            assert model_manager.os.environ["TORCH_HOME"] == str(Path(tmp2) / "torch")


def check_runtime_paths():
    assert runtime_paths.resource_root() == ROOT
    assert runtime_paths.data_root() == ROOT
    assert runtime_paths.resource_path("config.yaml") == ROOT / "config.yaml"
    assert runtime_paths.data_path("data", "history.db") == ROOT / "data" / "history.db"
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("LIVETRANSLATE_DATA_DIR")
        os.environ["LIVETRANSLATE_DATA_DIR"] = tmp
        try:
            assert runtime_paths.data_root() == Path(tmp).resolve()
            assert runtime_paths.data_path("data", "history.db") == Path(tmp).resolve() / "data" / "history.db"
        finally:
            if old is None:
                os.environ.pop("LIVETRANSLATE_DATA_DIR", None)
            else:
                os.environ["LIVETRANSLATE_DATA_DIR"] = old

    with tempfile.TemporaryDirectory() as tmp:
        old_appdata = os.environ.get("APPDATA")
        old_override = os.environ.get("LIVETRANSLATE_DATA_DIR")
        old_frozen = getattr(sys, "frozen", None)
        old_executable = sys.executable
        os.environ.pop("LIVETRANSLATE_DATA_DIR", None)
        os.environ["APPDATA"] = str(Path(tmp) / "Roaming")
        try:
            sys.frozen = True
            sys.executable = str(Path(tmp) / "dist" / "LiveTranslate" / "LiveTranslate.exe")
            assert runtime_paths.data_root() == Path(tmp, "Roaming", "LiveTranslate").resolve()
        finally:
            if old_frozen is None:
                try:
                    delattr(sys, "frozen")
                except AttributeError:
                    pass
            else:
                sys.frozen = old_frozen
            sys.executable = old_executable
            if old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old_appdata
            if old_override is None:
                os.environ.pop("LIVETRANSLATE_DATA_DIR", None)
            else:
                os.environ["LIVETRANSLATE_DATA_DIR"] = old_override


def check_control_panel_constructs():
    with tempfile.TemporaryDirectory() as tmp:
        old_data = os.environ.get("LIVETRANSLATE_DATA_DIR")
        old_qt = os.environ.get("QT_QPA_PLATFORM")
        os.environ["LIVETRANSLATE_DATA_DIR"] = tmp
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            import yaml
            from PyQt6.QtWidgets import QApplication
            from control_panel import ControlPanel

            app = QApplication.instance() or QApplication([])
            config = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
            panel = ControlPanel(
                config,
                saved_settings={
                    "vad_mode": "silero",
                    "vad_threshold": config["asr"]["vad_threshold"],
                    "energy_threshold": 0.02,
                    "min_speech_duration": config["asr"]["min_speech_duration"],
                    "max_speech_duration": config["asr"]["max_speech_duration"],
                    "silence_mode": "auto",
                    "silence_duration": 0.8,
                    "asr_language": "auto",
                    "asr_engine": "openai-audio",
                    "asr_device": "cpu",
                    "asr_api": config["asr_api"],
                    "models": [
                        {
                            "name": "demo",
                            "provider": "openai-compatible",
                            "api_base": "http://127.0.0.1:1234/v1",
                            "api_key": "",
                            "model": "demo",
                        }
                    ],
                    "active_model": 0,
                    "hub": "ms",
                    "translation_mode": "natural",
                },
            )
            panel._apply_settings()
            settings = panel.get_settings()
            assert settings["asr_engine"] == "openai-audio"
            assert settings["asr_api"]["model"] == "whisper-1"
            assert (Path(tmp) / "data" / "history.db").exists()
            panel.deleteLater()
            app.processEvents()
        finally:
            if old_data is None:
                os.environ.pop("LIVETRANSLATE_DATA_DIR", None)
            else:
                os.environ["LIVETRANSLATE_DATA_DIR"] = old_data
            if old_qt is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = old_qt


def check_resource_templates():
    assert {"literal", "natural", "explain", "polish"}.issubset(PROMPT_PRESETS)
    for name, template in PROMPT_PRESETS.items():
        rendered = template.format(
            source_lang="English",
            target_lang="Chinese",
            context="Previous subtitle context.",
        )
        assert rendered.strip(), name
    assert DEFAULT_PROMPT.strip()
    assert (runtime_paths.resource_path("i18n") / "en.yaml").exists()
    assert (runtime_paths.resource_path("i18n") / "zh.yaml").exists()


def check_asr_provider_config():
    cfg = ASRProviderConfig(
        engine_type="whisper",
        device="cuda:1 (Demo GPU)",
        model_size="small",
        compute_type="float16",
        language="en",
        hub="hf",
    )
    assert cfg.engine_type == "whisper"

    cuda = resolve_device("cuda:1 (Demo GPU)", "float16")
    assert cuda.device == "cuda"
    assert cuda.device_index == 1
    assert cuda.pipeline_device == "cuda:1"
    assert cuda.compute_type == "float16"

    cpu = resolve_device("cpu", "float16")
    assert cpu.device == "cpu"
    assert cpu.pipeline_device == "cpu"
    assert cpu.compute_type == "int8"


def check_openai_audio_asr_provider():
    class FakeTranscriptions:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return {"text": "hello world"}

    class FakeAudio:
        def __init__(self):
            self.transcriptions = FakeTranscriptions()

    class FakeClient:
        def __init__(self):
            self.audio = FakeAudio()

    provider = OpenAIAudioASRProvider(
        base_url="http://127.0.0.1:1234/v1",
        api_key="",
        model="whisper-1",
        language="en",
    )
    provider._client = FakeClient()
    audio = np.zeros(1600, dtype=np.float32)
    result = provider.transcribe(audio)
    assert result["text"] == "hello world"
    assert result["language"] == "en"
    kwargs = provider._client.audio.transcriptions.kwargs
    assert kwargs["model"] == "whisper-1"
    assert kwargs["language"] == "en"
    assert kwargs["file"].name == "audio.wav"

    created = create_asr_provider(
        ASRProviderConfig(
            engine_type="openai-audio",
            api_base="http://127.0.0.1:1234/v1",
            api_key="",
            api_model="whisper-1",
        )
    )
    assert isinstance(created, OpenAIAudioASRProvider)


def check_assemblyai_asr_provider():
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self):
            self.posts = []
            self.gets = []

        def post(self, path, **kwargs):
            self.posts.append((path, kwargs))
            if path == "/v2/upload":
                return FakeResponse({"upload_url": "https://cdn.example/audio.wav"})
            if path == "/v2/transcript":
                return FakeResponse({"id": "tx-1"})
            raise AssertionError(path)

        def get(self, path):
            self.gets.append(path)
            return FakeResponse(
                {"status": "completed", "text": "hello from assembly", "language_code": "en"}
            )

        def close(self):
            return None

    provider = AssemblyAIASRProvider(
        base_url="https://api.assemblyai.com",
        api_key="test-key",
        model="universal",
        language="en",
    )
    provider._client = FakeClient()
    result = provider.transcribe(np.zeros(1600, dtype=np.float32))
    assert result["text"] == "hello from assembly"
    assert result["language"] == "en"
    assert provider._client.posts[1][1]["json"]["speech_model"] == "universal"
    assert provider._client.posts[1][1]["json"]["language_code"] == "en"

    created = create_asr_provider(
        ASRProviderConfig(
            engine_type="assemblyai",
            api_base="https://api.assemblyai.com",
            api_key="test-key",
            api_model="universal",
        )
    )
    assert isinstance(created, AssemblyAIASRProvider)

    streaming = AssemblyAIStreamingASRProvider(
        base_url="wss://streaming.assemblyai.com/v3/ws",
        api_key="test-key",
        model="u3-rt-pro",
        language="en",
    )
    stream_url = streaming._stream_url()
    assert stream_url.startswith("wss://streaming.assemblyai.com/v3/ws?")
    assert "speech_model=u3-rt-pro" in stream_url
    assert "sample_rate=16000" in stream_url
    assert streaming._float32_to_pcm16(np.zeros(1600, dtype=np.float32))

    created_streaming = create_asr_provider(
        ASRProviderConfig(
            engine_type="assemblyai-streaming",
            api_base="wss://streaming.assemblyai.com/v3/ws",
            api_key="test-key",
            api_model="u3-rt-pro",
        )
    )
    assert isinstance(created_streaming, AssemblyAIStreamingASRProvider)


def check_translator_facade():
    class FakeProvider:
        name = "fake"
        model = "fake-model"

        @property
        def last_usage(self):
            return 3, 4

        def translate(self, input_data):
            assert "CURRENT SUBTITLE:" in input_data.text
            assert "hello" in input_data.text
            return TranslateResult(original=input_data.text, translation="你好")

        def stream_translate(self, input_data):
            yield "你"
            yield "你好"

    translator = Translator("http://127.0.0.1:1234/v1", "dummy", "fake", provider=FakeProvider())
    assert translator.translate("hello", "en") == "你好"
    assert list(translator.translate_iter("hello", "en")) == ["你", "你好"]
    assert translator.last_usage == (3, 4)


def check_ollama_payload():
    provider = OllamaProvider(base_url="http://localhost:11434/api/chat", model="demo")
    payload = provider._payload(
        TranslateInput(text="hello", system_prompt="translate", context=[("a", "b")]),
        stream=True,
    )
    assert payload["model"] == "demo"
    assert payload["stream"] is True
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][-1]["content"] == "hello"


def check_initial_model_persist_rules():
    assert should_persist_model_config(
        {
            "provider": "openai-compatible",
            "api_base": "http://127.0.0.1:1234/v1",
            "api_key": "",
            "model": "local-model",
        }
    )
    assert should_persist_model_config(
        {
            "provider": "ollama",
            "api_base": "http://localhost:11434/api/chat",
            "api_key": "",
            "model": "qwen2.5:7b",
        }
    )
    assert not should_persist_model_config(
        {"provider": "ollama", "api_base": "", "model": "qwen2.5:7b"}
    )


def check_provider_error_messages():
    AuthError = type("AuthenticationError", (Exception,), {})
    TimeoutErr = type("APITimeoutError", (Exception,), {})
    NotFoundErr = type("NotFoundError", (Exception,), {"status_code": 404})
    RateLimitErr = type("RateLimitError", (Exception,), {"status_code": 429})

    assert "API Key" in str(readable_provider_error(AuthError("bad key")))
    assert "超时" in str(readable_provider_error(TimeoutErr("timeout")))
    assert "模型不存在" in str(readable_provider_error(NotFoundErr("missing")))
    assert "额度不足" in str(readable_provider_error(RateLimitErr("limited")))


def check_history_and_export():
    with tempfile.TemporaryDirectory() as tmp:
        store = HistoryStore(Path(tmp) / "history.db")
        store.set_config("history_enabled", False)
        assert store.get_config("history_enabled") is False
        assert store.get_config("missing", "fallback") == "fallback"
        session_id = store.start_session("demo")
        first_id = store.add_subtitle_original(1, "Hello", "en", "zh", start_time=100.0)
        store.update_subtitle_original(1, "Hello world", "en")
        store.update_subtitle_translation(1, "你好", end_time=102.0)
        store.add_translation_history("global-hotkey", "World", "世界", "zh", "explain")
        store.add_translation_history("chrome-extension", "Browser", "浏览器", "zh", "explain")
        store.add_translation_history("manual", "Manual", "手动", "zh", "natural")

        all_rows = store.list_history("all")
        assert len(all_rows) == 4
        assert store.list_history("subtitle")[0]["id"] == first_id
        assert store.list_history("global-hotkey")[0]["translated_text"] == "世界"
        selection_rows = store.list_history("selection")
        assert len(selection_rows) == 2
        assert {row["source_type"] for row in selection_rows} == {
            "global-hotkey",
            "chrome-extension",
        }
        assert store.list_history("manual")[0]["translated_text"] == "手动"

        session_rows = store.get_session_subtitles(session_id)
        assert session_rows[0]["original_text"] == "Hello world"
        items = subtitle_rows_to_export_items(session_rows)
        srt = export_subtitles(items, "both", "srt")
        md = export_subtitles(items, "both", "md")
        assert "00:00:00,000 --> 00:00:02,000" in srt
        assert "Hello world" in srt and "你好" in srt
        assert "# 字幕记录" in md

        conn = sqlite3.connect(Path(tmp) / "history.db")
        try:
            assert conn.execute("SELECT COUNT(*) FROM subtitle_history").fetchone()[0] == 1
        finally:
            conn.close()


def check_transcript_original_revision():
    with tempfile.TemporaryDirectory() as tmp:
        writer = TranscriptWriter(Path(tmp))
        writer.write_original(1, "12:00:00", "This is because.")
        writer.update_original(1, "This is because. we changed the cache settings")
        writer.write_translation(1, "这是因为我们改了缓存设置")
        paths = writer.session_paths()
        writer.close()

        all_text = Path(paths["all"]).read_text("utf-8")
        assert "This is because. we changed the cache settings" in all_text
        assert "This is because.\n  ->" not in all_text


def check_local_api():
    server = LocalAPIServer(lambda payload: {"ok": True, "text": payload["text"]}, port=17993)
    server.start()
    try:
        health = urllib.request.urlopen(
            "http://127.0.0.1:17993/api/health", timeout=3
        ).read().decode("utf-8")
        health_data = json.loads(health)
        assert health_data["ok"] is True
        assert health_data["token_required"] is False
        assert "/api/translate-selection" in health_data["endpoints"]
        request = urllib.request.Request(
            "http://127.0.0.1:17993/api/translate-selection",
            data=json.dumps({"text": "  hello  "}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        body = urllib.request.urlopen(request, timeout=3).read().decode("utf-8")
        assert json.loads(body) == {"ok": True, "text": "hello"}

        bad_request = urllib.request.Request(
            "http://127.0.0.1:17993/api/translate-selection",
            data=json.dumps({"text": ""}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(bad_request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            assert "text is required" in exc.read().decode("utf-8")
        else:
            raise AssertionError("Local API should reject empty text")

        bad_mode = urllib.request.Request(
            "http://127.0.0.1:17993/api/translate-selection",
            data=json.dumps({"text": "hello", "mode": "bad"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(bad_mode, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            assert "unsupported mode" in exc.read().decode("utf-8")
        else:
            raise AssertionError("Local API should reject unsupported mode")
    finally:
        server.stop()


def check_local_api_token():
    server = LocalAPIServer(
        lambda payload: {"ok": True, "text": payload["text"]},
        port=17994,
        token="local-secret",
    )
    server.start()
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:17994/api/translate-selection",
            data=json.dumps({"text": "hello"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("Local API should reject missing token")

        request = urllib.request.Request(
            "http://127.0.0.1:17994/api/translate-selection",
            data=json.dumps({"text": "hello"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-LiveTranslate-Token": "local-secret",
            },
            method="POST",
        )
        body = urllib.request.urlopen(request, timeout=3).read().decode("utf-8")
        assert json.loads(body) == {"ok": True, "text": "hello"}
    finally:
        server.stop()


def check_local_api_token_runtime_update():
    server = LocalAPIServer(
        lambda payload: {"ok": True, "text": payload["text"]},
        port=17995,
    )
    server.start()
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:17995/api/translate-selection",
            data=json.dumps({"text": "hello"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        body = urllib.request.urlopen(request, timeout=3).read().decode("utf-8")
        assert json.loads(body) == {"ok": True, "text": "hello"}

        server.set_token("runtime-secret")
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("Local API should apply token updates at runtime")

        request = urllib.request.Request(
            "http://127.0.0.1:17995/api/translate-selection",
            data=json.dumps({"text": "hello"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-LiveTranslate-Token": "runtime-secret",
            },
            method="POST",
        )
        body = urllib.request.urlopen(request, timeout=3).read().decode("utf-8")
        assert json.loads(body) == {"ok": True, "text": "hello"}
    finally:
        server.stop()


def check_history_disable():
    class FakeHistory:
        def __init__(self):
            self.count = 0

        def add_translation_history(self, **_kwargs):
            self.count += 1

    class FakeTranslator:
        provider_name = "fake"
        model = "fake-model"

        def __init__(self, *_args, **_kwargs):
            pass

        def translate(self, *_args, **_kwargs):
            return "自然翻译：\n你好"

    import selection_translate.service as service_mod

    original_translator = service_mod.Translator
    fake_history = FakeHistory()
    try:
        service_mod.Translator = FakeTranslator
        service = SelectionTranslateService(
            get_model_config=lambda: {"api_base": "x", "api_key": "", "model": "m"},
            get_target_language=lambda: "zh",
            get_timeout=lambda: 1,
            history_store=fake_history,
            get_history_enabled=lambda: False,
        )
        result = service.translate("hello")
        assert result["translation"] == "你好"
        assert fake_history.count == 0
    finally:
        service_mod.Translator = original_translator


def check_manual_translation_history():
    class FakeTranslator:
        provider_name = "fake"
        model = "fake-model"

        def __init__(self, *_args, **_kwargs):
            pass

        def translate(self, *_args, **_kwargs):
            return "自然翻译：\n你好\n\n解释：\n问候语"

    import selection_translate.service as service_mod

    original_translator = service_mod.Translator
    try:
        service_mod.Translator = FakeTranslator
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "history.db")
            service = SelectionTranslateService(
                get_model_config=lambda: {"api_base": "x", "api_key": "", "model": "m"},
                get_target_language=lambda: "zh",
                get_timeout=lambda: 1,
                history_store=store,
                get_history_enabled=lambda: True,
            )
            result = service.translate("hello", source="manual", mode="explain")
            assert result["translation"] == "你好"
            rows = store.list_history("manual")
            assert len(rows) == 1
            assert rows[0]["source_type"] == "manual"
            assert rows[0]["translated_text"] == "你好"
    finally:
        service_mod.Translator = original_translator


def main():
    checks = [
        check_manifest,
        check_package_preflight,
        check_build_output_verifier,
        check_log_redaction,
        check_diagnostic_bundle,
        check_diagnostic_bundle_legacy_project_root,
        check_hotkey_parser,
        check_audio_placeholder,
        check_cache_path_env,
        check_runtime_paths,
        check_control_panel_constructs,
        check_resource_templates,
        check_asr_provider_config,
        check_openai_audio_asr_provider,
        check_assemblyai_asr_provider,
        check_translator_facade,
        check_ollama_payload,
        check_initial_model_persist_rules,
        check_provider_error_messages,
        check_history_and_export,
        check_transcript_original_revision,
        check_local_api,
        check_local_api_token,
        check_local_api_token_runtime_update,
        check_history_disable,
        check_manual_translation_history,
    ]
    for check in checks:
        check()
        print(f"OK {check.__name__}")
    print("smoke check passed")


if __name__ == "__main__":
    main()
