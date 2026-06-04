from __future__ import annotations

import queue
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402


class _FailingAudio:
    def __init__(self):
        self.stopped = False

    def start(self):
        raise RuntimeError("audio open failed")

    def stop(self):
        self.stopped = True


def test_start_failure_leaves_pipeline_stopped_and_retryable():
    app = main.LiveTranslateApp.__new__(main.LiveTranslateApp)
    old_executor = ThreadPoolExecutor(max_workers=1)
    old_queue = queue.Queue(maxsize=1)
    app._running = False
    app._paused = False
    app._subwin = None
    app._audio = _FailingAudio()
    app._capture_thread = None
    app._asr_thread = None
    app._tl_executor = old_executor
    app._asr_queue = old_queue
    app._mem_periodic_timer = None

    try:
        with pytest.raises(RuntimeError, match="audio open failed"):
            main.LiveTranslateApp.start(app)

        assert app._running is False
        assert app._paused is False
        assert app._capture_thread is None
        assert app._asr_thread is None
        assert app._tl_executor is old_executor
        assert app._asr_queue is old_queue
    finally:
        old_executor.shutdown(wait=False)
