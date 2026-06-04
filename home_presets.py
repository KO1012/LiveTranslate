"""
Home-tab provider presets.

Used by the simplified "Home" tab in the control panel so that non-technical
users only need to pick a brand from a list, paste their API key, and pick a
model. Each preset only fills in the OpenAI-compatible base URL plus a small
list of recommended model names. Users can still type a custom model name in
the editable combo box and they can switch the base URL freely afterwards.

All endpoints below were verified against vendor documentation. Keep entries
short; we do not advertise pricing or model capabilities here.
"""

from __future__ import annotations

# --------- Speech-to-text (ASR) ----------

# Each entry: (display_name, engine_type, api_base, [model_names])
#   engine_type:  "openai-audio"  -> OpenAI-compatible /v1/audio/transcriptions
#                 "assemblyai"    -> AssemblyAI /v2 transcript flow
#                 "assemblyai-streaming" -> AssemblyAI v3 Streaming WebSocket
#
# When users pick a preset we copy api_base + the first model name into the
# settings and select the matching engine_type so the rest of the pipeline
# (provider factory, tests, runtime) stays unchanged.

ASR_PRESETS: list[dict] = [
    {
        "name": "OpenAI Whisper",
        "engine": "openai-audio",
        "api_base": "https://api.openai.com/v1",
        "models": ["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"],
        "hint": "OpenAI 官方 /v1/audio/transcriptions 接口。",
    },
    {
        "name": "Groq Whisper",
        "engine": "openai-audio",
        "api_base": "https://api.groq.com/openai/v1",
        "models": [
            "whisper-large-v3-turbo",
            "whisper-large-v3",
            "distil-whisper-large-v3-en",
        ],
        "hint": "Groq 上的 Whisper,速度极快、按量计费。",
    },
    {
        "name": "SiliconFlow",
        "engine": "openai-audio",
        "api_base": "https://api.siliconflow.cn/v1",
        "models": [
            "FunAudioLLM/SenseVoiceSmall",
            "TeleAI/TeleSpeechASR",
        ],
        "hint": "硅基流动,国内访问稳定,支持中英日韩等多语种。",
    },
    {
        "name": "AssemblyAI",
        "engine": "assemblyai",
        "api_base": "https://api.assemblyai.com",
        "models": ["universal", "best", "nano"],
        "hint": "AssemblyAI Universal-2,英文识别质量高。",
    },
    {
        "name": "AssemblyAI Streaming",
        "engine": "assemblyai-streaming",
        "api_base": "wss://streaming.assemblyai.com/v3/ws",
        "models": [
            "u3-rt-pro",
            "universal-streaming-english",
            "universal-streaming-multilingual",
            "whisper-rt",
        ],
        "hint": "AssemblyAI v3 Streaming WebSocket,用于实时语音转文本。",
    },
    {
        "name": "自定义 (OpenAI 兼容)",
        "engine": "openai-audio",
        "api_base": "https://api.openai.com/v1",
        "models": ["whisper-1"],
        "hint": "自托管或其它兼容服务,自己填地址和模型名。",
    },
]


# Local ASR engines run model weights on-device (no cloud API). The home tab's
# ASR card only has presets for cloud engines, so when one of these is the
# active engine the card shows a read-only "local model" sentinel instead and
# never overwrites asr_engine. Engine switching for these stays in the
# Advanced (ASR & VAD) page.
LOCAL_ASR_ENGINES = frozenset(
    {
        "whisper",
        "sensevoice",
        "funasr-nano",
        "funasr-mlt-nano",
        "anime-whisper",
    }
)


def is_local_asr_engine(engine: str) -> bool:
    """Return True if ``engine`` is an on-device (local) ASR engine."""
    return (engine or "").strip().lower() in LOCAL_ASR_ENGINES


# --------- LLM (translation) ----------

# Each entry: (display_name, provider, api_base, [recommended_models], hint)
#   provider:  "openai-compatible" or "ollama" — matches translator/factory.

LLM_PRESETS: list[dict] = [
    {
        "name": "DeepSeek",
        "provider": "openai-compatible",
        "api_base": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "hint": "DeepSeek 官方 API,翻译性价比高。",
    },
    {
        "name": "OpenAI",
        "provider": "openai-compatible",
        "api_base": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "hint": "OpenAI 官方接口,按量计费。",
    },
    {
        "name": "硅基流动 SiliconFlow",
        "provider": "openai-compatible",
        "api_base": "https://api.siliconflow.cn/v1",
        "models": [
            "Qwen/Qwen2.5-7B-Instruct",
            "deepseek-ai/DeepSeek-V3",
            "tencent/Hunyuan-MT-7B",
        ],
        "hint": "汇聚多家开源模型,国内直连。",
    },
    {
        "name": "阿里百炼 (Qwen)",
        "provider": "openai-compatible",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-mt-turbo", "qwen-plus", "qwen-turbo"],
        "hint": "阿里百炼 OpenAI 兼容入口,推荐 qwen-mt-* 翻译模型。",
    },
    {
        "name": "火山方舟 (豆包)",
        "provider": "openai-compatible",
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-1-6", "doubao-1-5-pro-32k", "doubao-pro-32k"],
        "hint": "字节跳动豆包系列。模型名是控制台分配的 endpoint id。",
    },
    {
        "name": "智谱 GLM",
        "provider": "openai-compatible",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-flash", "glm-4-plus", "glm-4-air"],
        "hint": "智谱 BigModel,支持长上下文。",
    },
    {
        "name": "月之暗面 Kimi",
        "provider": "openai-compatible",
        "api_base": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
        "hint": "Moonshot Kimi,中文强、上下文长。",
    },
    {
        "name": "OpenRouter",
        "provider": "openai-compatible",
        "api_base": "https://openrouter.ai/api/v1",
        "models": [
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
            "google/gemini-2.0-flash-001",
        ],
        "hint": "聚合 400+ 模型,海外节点。",
    },
    {
        "name": "本地 LM Studio",
        "provider": "openai-compatible",
        "api_base": "http://127.0.0.1:1234/v1",
        "models": ["local-model"],
        "hint": "LM Studio 默认监听 1234 端口,本机运行,不需要 API Key。",
    },
    {
        "name": "本地 Ollama",
        "provider": "ollama",
        "api_base": "http://127.0.0.1:11434",
        "models": ["qwen2.5:7b", "llama3.1:8b", "gemma2:9b"],
        "hint": "需要先在终端运行 `ollama pull <模型名>`。",
    },
    {
        "name": "自定义 (OpenAI 兼容)",
        "provider": "openai-compatible",
        "api_base": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini"],
        "hint": "自托管或其它兼容服务,自己填地址和模型名。",
    },
]


def find_asr_preset(api_base: str, engine: str) -> int:
    """Return index of the preset whose api_base+engine match, else -1."""
    api_base = (api_base or "").rstrip("/").lower()
    for i, p in enumerate(ASR_PRESETS):
        if p["engine"] == engine and p["api_base"].rstrip("/").lower() == api_base:
            return i
    return -1


def find_llm_preset(api_base: str, provider: str) -> int:
    api_base = (api_base or "").rstrip("/").lower()
    for i, p in enumerate(LLM_PRESETS):
        if (
            p["provider"] == provider
            and p["api_base"].rstrip("/").lower() == api_base
        ):
            return i
    return -1
