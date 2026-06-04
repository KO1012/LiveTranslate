# LiveTranslate 代码审计记录

审计日期：2026-05-27

## 项目启动

推荐环境：

```powershell
cd E:\translate\LiveTranslate
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

如果使用 CUDA，需要按显卡和 CUDA 版本单独安装 `torch`、`torchaudio`，再安装其余依赖。

## 现有模块入口

| 功能 | 文件 | 说明 |
|---|---|---|
| 主程序编排 | `main.py` | 创建音频、VAD、ASR、Translator、字幕 overlay，并管理线程 |
| 音频捕获抽象 | `audio_providers/` | 根据平台创建音频捕获 Provider |
| Windows 系统音频捕获 | `audio_capture.py`、`audio_providers/windows_wasapi.py` | Windows WASAPI loopback 捕获，支持设备选择和麦克风混音 |
| macOS 音频占位 | `audio_providers/macos_placeholder.py` | 暂不实现捕获，提示后续 ScreenCaptureKit/CoreAudio 支持 |
| VAD/切片 | `vad_processor.py` | 根据静音、最小时长、最大时长切分语音片段 |
| ASR 统一入口 | `asr_engine.py` | faster-whisper 入口 |
| OpenAI Audio ASR | `asr_providers/openai_audio.py` | OpenAI 兼容音频转写 API，适合不想本地加载 ASR 模型的场景 |
| SenseVoice ASR | `asr_sensevoice.py` | SenseVoice 引擎适配 |
| FunASR Nano | `asr_funasr_nano.py` | FunASR Nano 引擎适配 |
| Anime Whisper | `asr_anime_whisper.py` | Anime Whisper 引擎适配 |
| LLM 翻译外观 | `translator.py` | 保留原调用接口，内部改为 Provider 层 |
| OpenAI Compatible Provider | `providers/openai_compatible.py` | OpenAI 兼容 API 请求、流式输出、超时和 JSON 输出 |
| Ollama Provider | `providers/ollama.py` | 本地 Ollama `/api/chat` 调用、流式输出、连接失败提示 |
| 字幕悬浮窗 | `subtitle_overlay.py` | 桌面悬浮字幕、流式更新、复制、导出 |
| 多语言字幕窗口 | `subtitle_window.py` | 多目标语言字幕显示 |
| 控制面板 | `control_panel.py` | 模型配置、prompt、VAD/ASR、样式设置 |
| 历史记录 UI | `control_panel.py` | 历史页支持筛选、详情查看、删除选中记录 |
| 手动文本翻译 | `control_panel.py`、`selection_translate/service.py` | 翻译页可输入文本并选择直译、自然表达、解释、润色模式，结果写入 `manual` 历史 |
| 启动/配置弹窗 | `dialogs.py` | 首次启动、模型下载、模型配置编辑 |
| 字幕文本落盘 | `transcript_writer.py` | transcript 文件写入 |
| SQLite 历史记录 | `history/db.py` | 创建配置、翻译历史、字幕历史、会话表，并保存字幕记录 |
| 选中文字翻译服务 | `selection_translate/service.py` | 用解释模式调用当前模型，并写入翻译历史 |
| 本地 HTTP API | `selection_translate/local_server.py` | 监听 `127.0.0.1:17891`，提供插件调用接口 |
| 全局快捷键划词翻译 | `selection_translate/hotkey.py`、`clipboard.py`、`panel.py` | `Ctrl+Shift+T` 复制选中文本、翻译并显示结果窗口 |
| Chrome 插件 | `browser-extension/` | Manifest V3 右键菜单，调用本地划词翻译 API |
| 字幕导出 | `exporters/subtitle_exporter.py`、`subtitle_overlay.py` | 支持 `.txt`、`.srt`、`.md` 导出 |

## 第一阶段改动

已新增统一 LLM Provider 层：

- `providers/base.py`
- `providers/openai_compatible.py`
- `providers/ollama.py`
- `providers/__init__.py`

`translator.py` 现在只作为应用层外观，业务代码不再直接创建模型客户端。模型配置通过 `provider` 字段选择 `openai-compatible` 或 `ollama`。

Ollama 默认地址：

```text
http://localhost:11434/api/chat
```

已新增 Prompt 模板：

- `prompts/subtitle_prompt.txt`
- `prompts/explain_prompt.txt`
- `prompts/polish_prompt.txt`

已新增翻译模式：

- `literal`
- `natural`
- `explain`
- `polish`

控制面板的 Prompt 预设已加入以上四种模式；字幕默认使用 `natural`。

已新增 SQLite 历史记录：

- 数据库路径：`data/history.db`
- 表：`config`、`translation_history`、`subtitle_history`、`sessions`
- ASR 出原文时写入 `subtitle_history`
- 翻译完成、同语言跳过或翻译失败时更新同一条字幕记录
- 控制面板新增历史记录页，可按全部、字幕、快捷键划词、浏览器插件筛选
- 历史筛选已补充开发文档要求的 `selection` 聚合筛选和 `manual` 手动翻译筛选
- 支持查看详情和删除选中记录
- 选中字幕历史后可导出该字幕所属的完整会话为 `.txt`、`.srt`、`.md`
- 支持关闭本地历史保存，支持一键清空全部历史记录

已新增本地划词翻译 API：

- 健康检查：`GET http://127.0.0.1:17891/api/health`
- 划词翻译：`POST http://127.0.0.1:17891/api/translate-selection`
- 健康检查会返回 `token_required` 和可用 endpoints；划词接口会在 API 层校验空文本和不支持的模式。
- 请求体示例：

```json
{
  "text": "The proposal is still up in the air.",
  "source": "chrome-extension",
  "url": "https://example.com",
  "mode": "explain"
}
```

- 本地服务只监听 `127.0.0.1`
- 如果 `local_api.token` 有值，客户端必须发送 `X-LiveTranslate-Token`
- 控制面板翻译页可设置本地 API Token，运行中修改会立即应用到本地 HTTP 服务

已新增 Chrome 插件：

- 目录：`browser-extension/`
- 安装说明：`docs/chrome_extension_install.md`
- 右键菜单：
  - `AI 翻译解释`
  - `自然翻译`
  - `学术润色`
- 插件只把选中文本发送到本地 API，不保存 API Key
- 本地客户端未启动或请求失败时，会在页面浮窗和 popup 中显示错误

已新增 Windows 打包说明：

- 文档：`docs/packaging.md`
- 脚本：`scripts/build_windows.ps1`
- 快捷入口：`build_windows.bat`
- 默认使用 PyInstaller 输出 `dist/LiveTranslate/`
- 打包资源显式包含 `funasr_nano/`，避免 FunASR Nano 运行时动态导入 `model.py` 丢失
- 新增 `scripts/verify_build_output.py`，校验 `dist/LiveTranslate` 中 exe、运行资源存在，并确认未带 `user_settings.json`、`data/`、`logs/`、`transcripts/`

已新增离线自检：

- 脚本：`scripts/smoke_check.py`
- 快捷入口：`smoke_check.bat`
- 覆盖插件 manifest、热键解析、macOS 音频占位、Provider 外观、Ollama payload、首次模型配置保存规则、Provider 错误提示、SQLite config/历史/导出、本地 HTTP API、本地 API Token 运行时切换

已新增全局快捷键划词翻译：

- 默认快捷键：`Ctrl+Shift+T`
- 流程：保存剪贴板、模拟 `Ctrl+C`、读取选中文本、恢复剪贴板、调用解释式翻译、弹出结果窗口
- 结果窗口支持复制译文、复制全部、关闭
- `config.yaml` 中可通过 `hotkey.enabled` 开关，通过 `hotkey.shortcut` 配置组合键
- 控制面板翻译页可启用/禁用并修改快捷键

已新增手动文本翻译：

- 控制面板翻译页可直接输入文本，选择 `literal`、`natural`、`explain`、`polish` 模式。
- 翻译任务在后台线程执行，完成后可复制译文。
- 手动翻译结果以 `manual` 类型写入 SQLite 历史，可在历史页按“手动翻译”筛选。

已新增字幕导出格式：

- `.txt`：保留原有文本导出能力
- `.srt`：输出标准 SRT 时间戳
- `.md`：输出原文/译文对照 Markdown
- overlay 导出当前内存字幕；历史记录页可从 SQLite 导出完整字幕会话

已补充启动配置与错误处理：

- 首次启动向导可选择模型缓存目录，保存到 `user_settings.json` 的 `cache_path`；后续启动会在导入 torch 前应用 `MODELSCOPE_CACHE`、`HF_HOME`、`TORCH_HOME`。
- 缓存目录读取改为动态 `get_models_dir()`；首次向导修改缓存目录后，下载、缓存检测和 ASR 加载会使用同一路径。
- 新增 `runtime_paths.py` 区分只读资源目录和运行时数据目录；打包后配置、日志、历史、转写、模型缓存写到 exe 同级目录，不写入 `_internal/`。
- 诊断包按资源目录读取只读配置，按运行时数据目录读取 `user_settings.json` 和日志；发布版 `--self-check` 会验证诊断包包含脱敏后的默认配置。
- 首次启动后的模型配置不再强制要求 API Key；Ollama、LM Studio 等可空 Key 的本地/兼容服务只要填写 `api_base` 和 `model` 就会保存。
- OpenAI Compatible Provider 会把认证失败、连接失败、超时、模型不存在、限流/额度不足、服务端错误转换为普通用户可读提示。
- 控制面板缺失的 `QCheckBox` 导入已修复，避免历史记录和快捷键设置页打开时报错。

## 当前运行路径

## ASR 抽象补充

- 新增 `asr_providers/` 作为 ASR Provider 入口。
- `main.py` 不再直接按字符串 import 各个 ASR engine，而是通过 `create_asr_provider()` 创建。
- 当前已接入 `whisper`、`sensevoice`、`funasr-nano`、`funasr-mlt-nano`、`anime-whisper`。
- 新增 `openai-audio` ASR Provider，可在 VAD / ASR 页选择 “OpenAI Audio API”，配置 `asr_api.api_base`、`asr_api.api_key`、`asr_api.model`、`asr_api.timeout` 后通过 OpenAI 兼容音频转写接口识别。
- 打包脚本已显式加入这些 ASR engine 的 hidden import，避免动态导入在 PyInstaller 产物中丢失。

实时字幕主流程：

```text
AudioCaptureProvider
  -> VADProcessor
  -> ASREngine / SenseVoice / FunASR
  -> Translator
  -> OpenAICompatibleProvider
  -> SubtitleOverlay / SubtitleWindow
```

音频捕获平台策略：

- Windows：`WindowsWasapiCaptureProvider`
- macOS：`MacOSAudioCaptureProviderPlaceholder`
- macOS 当前提示：`macOS 音频捕获功能暂未开放，后续将通过 ScreenCaptureKit/CoreAudio 支持。`

## 已知问题

1. macOS 已有音频 Provider 占位，但真实 ScreenCaptureKit/CoreAudio 捕获尚未实现。
2. 本地未配置实际 API Key / Ollama 模型和 ASR 模型时，不能完成端到端字幕实测。
