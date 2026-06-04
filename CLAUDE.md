# CLAUDE.md — LiveTranslate 开发文档与施工说明书

> 本文件是本仓库的**唯一开发文档**，同时充当给 AI agent 和人类开发者的**导航 + 约束说明书**。
> 它的目标：让任何人/agent 在改动前先知道"该动哪个文件、不该动哪里、改完要做什么"，
> 避免在 2000+ 行的大文件里到处瞎改。

> **本文档如何被自动注意到（防腐机制）**：
> - `.kiro/steering/agent-rules.md` 标记为 `inclusion: always`，会在**每次 Kiro 对话自动注入**，
>   把"先读本文档 → 再改 → 改完更新本文档"的规则强制摆到每个 agent 面前，无需用户手动提醒。
> - `agentStop` Hook（`Maintain CLAUDE.md after changes`）在**每轮工作结束后**自动追问是否需要
>   同步更新本文档，做流程兜底。
> - 若你用的不是 Kiro（如 Claude Code/命令行），`CLAUDE.md` 本身就是约定文件——你正在读它，请遵守。

---

## ⚠️ 给后续 Agent 的强制规则（必读，最高优先级）

1. **改动前先查"功能 → 文件导航表"**（见下文），按表定位文件，不要凭猜测全局搜索后乱改。
2. **改完代码必须维护本文档**：如果你新增/删除/移动了模块、改了功能入口位置、调整了菜单结构、
   新增了配置项或信号，**必须同步更新本文档对应章节**（导航表、模块地图、配置、信号）。
   文档与代码脱节会直接导致后续 agent 误判——保持同步是硬性要求。
3. **遵守"硬约束"章节**（颜色走 theme、文案走 i18n 双语、改完跑校验三件套等）。
4. **改完必须跑校验三件套**（见"验证"），全绿才算完成：
   - `python -m ruff check .`
   - `.venv\Scripts\python.exe -m pytest tests -q`
   - `.venv\Scripts\python.exe main.py --self-check`
5. **不确定职责边界时**，先读本文档的"模块地图"和"关键踩坑记录"，仍不清楚就向用户确认，不要擅自大改架构。

---

## 项目概览

LiveTranslate 是一个 Windows 实时音频翻译软件：通过 WASAPI 环回捕获系统声音 → 语音识别(ASR)
→ 调用 LLM API 翻译 → 在透明悬浮窗 / 独立字幕窗显示，并可选语音朗读(TTS 同声传译)。

**当前阶段**：Phase 0 Python 原型（Phase 1 计划为 C++ DirectShow Audio Tap Filter）。

附带能力：划词翻译 + 全局快捷键、本地 HTTP API（供浏览器扩展调用）、音频文件离线翻译、
历史记录、字幕导出、转写文件落盘、翻译基准测试、诊断打包。

## 运行 / 校验

```bash
# 必须用项目自带 venv（系统 Python 缺依赖）
.venv\Scripts\python.exe main.py            # 开发模式（有控制台，看实时日志）
# 或双击 start_silent.vbs                    # 无终端黑框静默启动（用 pythonw.exe）

# 校验三件套（改完代码必跑，全绿才算完成）
python -m ruff check .                               # Lint（配置见 pyproject.toml）
.venv\Scripts\python.exe -m pytest tests -q          # 单元测试
.venv\Scripts\python.exe main.py --self-check        # 启动自检（导入+核心流程冒烟）
```

> 打包后的 exe（`LiveTranslate.spec`，`console=False`）本就无终端窗口；终端黑框只是开发模式现象。

- **Lint 配置已固化在 `pyproject.toml`**（`[tool.ruff]`：select F,E,W；ignore E501,E402；
  排除 vendored 的 `funasr_nano/`）。不要再用文档里手写的 ruff 命令——直接 `ruff check .`。
- `E402` 被忽略是有意为之：`main.py` 必须在 `import PyQt6` 之前 `import torch`（Windows DLL 冲突）。

---

## 🧭 功能 → 文件导航表（改动前先查这里）

> "我想改 X" → 去动这些文件。带 ⚠️ 的是容易漏改、必须连带改的地方。

### UI / 外观

| 我想改… | 主要文件 | ⚠️ 连带 |
|---|---|---|
| 应用界面配色 / 间距 / 圆角 / 按钮样式 | `theme.py`（唯一来源） | 禁止在别处写死 hex/QSS，见硬约束 |
| 纸感拼贴美术资源（首页 Hero / 三步插画 / 空状态 / 侧边栏图标 / 装饰） | `assets/paper_collage/`（PNG + SVG） | 经 `theme.nav_svg_icon()` 着色加载侧边栏图标；空状态走 `control_panel._make_empty_state`；资源清单见 `../LiveTranslate_PaperCollage_ArtPack/manifest.json` |
| 悬浮窗底栏/顶栏图标 + 纸感纹理 | `subtitle_overlay.py` + `assets/paper_collage/overlay/`（`textures/*.png`） | 顶栏/底栏按钮优先用 `_line_icon()` 的 QPainter 自绘线性图标，避免离屏渲染缺字并贴近参考稿；`_overlay_icon()` 仍保留给可用 PNG 图标兜底；撕纸分隔线纹理 `textures/torn_strip.png`；来源 `../subtitle_overlay_assets_pack/` |
| 字幕外观预设（Dracula/Nord/Paper Dark 等 15 套） | `subtitle_presets.py` | 控制面板"样式"tab 会读它（`_preset_keys` + i18n `preset_*`） |
| 悬浮窗工具栏（顶栏标题+置顶+关闭 / 底栏 播放·不透明度滑块·Aa·设置） | `subtitle_overlay.py`（`DragHandle`：顶栏在 `__init__`，底栏在 `build_bottom_bar()`；`_OverlayContainer` 绘制圆角暗纸面） | 托盘菜单常有重复项需同步、i18n；「朗读」按钮（`_tts_btn`/`tts_toggled`）默认隐藏但仍与托盘「同声传译朗读」+ 设置开关三处同步（`set_tts_active`）；清空走气泡右键菜单；透明度滑块（标签 `overlay_opacity`=「不透明度」）经 `opacity_changed` 持久化；`_OverlayContainer` 用 `overlay/textures/paper_tile.png` 低透明度叠加暗纸纹；**悬浮窗默认显示在任务栏**（`_setup_ui` 不再带 `Qt.Tool`），「任务栏」复选框（`_taskbar_check`，默认勾选）+ 托盘 `taskbar_action`（默认勾选）经 `taskbar_toggled`→`_set_taskbar` 增删 `Qt.Tool` 切换 tray-only 模式，三处默认值要一致 |
| 悬浮窗消息气泡（原文/译文纸签 + 撕纸分隔线 + 植物） | `subtitle_overlay.py`（`ChatMessage` + `_TornDivider` + `_chip_stylesheet`） | i18n（`subtitle_chip_*`）；译文纸签显示目标语言「译文（中文）」需 `ChatMessage._target_language`（由 `SubtitleOverlay.set_target_language` 同步）；纸签文字用暖墨色 `#5a5347`（非强调橙）；撕纸分隔线右端由 `_TornDivider._paint_ornament()` 绘制纸片+植物，避免整块图片背景压住字幕；**`_TornDivider` 的纸条纹理 `torn_strip.png` 是近不透明奶油色，必须在 `paintEvent` 里裁取下半段毛边区域再绘制（当前 opacity 0.78），否则全图拉伸会变成挡视野的白条** |
| 悬浮窗右键菜单 | `subtitle_overlay.py`（`ChatMessage.contextMenuEvent`） | i18n |
| 悬浮窗系统监控条（CPU/RAM/Token） | `subtitle_overlay.py`（`MonitorBar`） | — |
| 独立字幕窗（OBS 捕获用）渲染 | `subtitle_window.py` | — |
| 独立字幕窗的设置项 | `subtitle_settings.py` | i18n |
| 对话框（向导/模型编辑/下载）外观 | `dialogs.py` + `theme.dialog_stylesheet()` | — |
| 模型加载/下载对话框观感（状态行+忙碌进度条+可折叠详情） | `dialogs.py`（`_build_loading_body` 共享 helper、`_ModelLoadDialog`、`ModelDownloadDialog`） | i18n（`loading_*`/`show_details`/`hide_details`）、`theme.busy_progress_stylesheet`/`link_button_stylesheet` |
| 启动 splash 画面 | `splash.py`（`StartupSplash`），在 `main()` 重型初始化前 show、`overlay.show()` 后 finish | `theme.py` 配色、`assets/app-icon.png` |
| 日志窗口 | `log_window.py` | — |
| 应用图标 | `main.py`（`create_app_icon()`） | — |

### 菜单 / 信息架构

| 我想改… | 主要文件 | ⚠️ 连带 |
|---|---|---|
| 系统托盘菜单 | `main.py`（`main()` 内 tray 段，约 1700+ 行） | 与悬浮窗/面板多处重复，靠信号同步 |
| 设置中心导航分组 / 加减页面 | `control_panel.py`（`__init__` 的 `nav_spec` 列表） | i18n（`nav_*` 文案）、对应 `_create_*_tab` |
| 设置中心某个页面的内容 | `control_panel.py`（对应 `_create_*_tab`，内部逻辑独立） | i18n |
| 首页（快速开始）布局：Hero 横幅 + 三步并排卡 + 小贴士卡 | `control_panel.py`（`_create_home_tab` + `_make_step_card`） | i18n（`home_hero_*`/`home_step_*`/`home_tips_*`/`home_open_*`）、Hero 用 `home_hero_*.png`、badge 走 `theme.step_badge_stylesheet`；**首页 ASR 卡只服务云端引擎**，本地引擎走 `home_presets.is_local_asr_engine` 判定后显示只读哨兵条目（见踩坑记录） |
| 首次启动向导 | `dialogs.py`（`SetupWizardDialog`） | `home_presets.py` 预设 |

### 翻译

| 我想改… | 主要文件 | ⚠️ 连带 |
|---|---|---|
| 翻译请求行为 / 流式 / JSON 模式 / 参数 | `providers/openai_compatible.py`、`providers/ollama.py` | `translator.py` 是上层封装 |
| 翻译高层逻辑（上下文/切句/重复检测/提示词） | `translator.py` | — |
| 提示词预设 / 模板 | `prompts/*.txt` + `translator.py`（`PROMPT_PRESETS`、`DEFAULT_PROMPT`） | — |
| 翻译错误的用户提示文案 | `translator.py`（`subtitle_error_label`）+ `providers/openai_compatible.py`（`readable_provider_error`） | i18n（`error_tl_*`） |
| 新增一个 LLM provider | 新建 `providers/xxx.py` + 注册到 `providers/__init__.py` | `translator.py`、ModelEditDialog |

### 语音识别(ASR) / 音频 / VAD

| 我想改… | 主要文件 | ⚠️ 连带 |
|---|---|---|
| 新增/改 ASR 引擎 | `asr_providers/`（`base.py`+`factory.py`+具体 provider） | `control_panel` 的 VAD-ASR tab、`model_manager.py` |
| 本地 ASR 模型实现（Whisper/SenseVoice/FunASR/Anime） | `asr_engine.py` / `asr_sensevoice.py` / `asr_funasr_nano.py` / `asr_anime_whisper.py` | — |
| 模型下载 / 缓存 / 检测 | `model_manager.py` | — |
| 硬件检测 + 模型推荐（按 GPU/显存/内存推荐引擎·设备·Whisper 尺寸） | `model_manager.py`（`detect_hardware()`/`recommend_asr()`/`WHISPER_VRAM_NEED_GB`） | 向导 `dialogs.SetupWizardDialog`（硬件横幅 + 低端机预选云端）、设置中心 `control_panel._create_*_tab` 的 ASR 组（「应用推荐配置」按钮 `_apply_asr_recommendation` + Whisper 显存不足警告 `_update_whisper_vram_warning`）、i18n（`hw_*`） |
| 音频捕获（WASAPI 环回） | `audio_providers/windows_wasapi.py`（经 `audio_capture.py`/`factory.py`） | — |
| VAD 切分逻辑 | `vad_processor.py` | — |
| 实时管线（捕获→VAD→ASR→翻译→显示） | `main.py`（`LiveTranslateApp` 的 `_capture_loop`/`_asr_loop`/`_process_segment`/`_do_interim_asr`） | — |
| 音频文件离线翻译 | `audio_file_translate.py` | 入口在 control panel |

### 其它子系统

| 我想改… | 主要文件 | ⚠️ 连带 |
|---|---|---|
| TTS 语音朗读 / 同声传译 | `tts_controller.py`（控制器：启停/设置应用/流式分句策略）+ `tts_engine.py` + `tts_providers/`（edge / openai-speech / azure / elevenlabs） | `main.py` `_translate_async` 调用点（**流式边译边念**：`self._tts.begin_stream()` 取得 `TTSStream`，流式里 `feed(partial)` 切出完整句即念，收尾 `finish()` 补念剩余 remainder）、TTS tab（设置中心「语音同传」组）；一键开关在悬浮窗工具栏「朗读」按钮 + 托盘「同声传译朗读」+ 设置开关，三处经 `main()` 的 `_sync_tts_ui`/`_apply_tts_toggle` 同步 |
| 新增一个 TTS 服务商 | 新建 `tts_providers/xxx.py` + 在 `factory.ENGINE_CAPS` 加能力条目 + `create_tts_provider` 加分发 + `fetch_tts_items` 加获取分支 | 引擎下拉 `dialogs.TTSConfigEditDialog`（addItem）、i18n（`tts_engine_*`）；字段可见性/获取按钮由 `ENGINE_CAPS` 自动驱动，无需改对话框逻辑 |
| 划词翻译 + 全局快捷键 | `selection_translate/`（`service.py`/`hotkey.py`/`panel.py`/`clipboard.py`） | `main.py` 装配；结果弹窗 `panel.py` 文案走 i18n（`sel_*`/`copy_translation`/`copy_all`/`btn_close`/`history_*`） |
| 本地 HTTP API（浏览器扩展用） | `selection_translate/local_server.py` | `browser-extension/` |
| 浏览器扩展 | `browser-extension/`（JS，独立于 Python） | `docs/chrome_extension_install.md` |
| 历史记录（SQLite） | `history/db.py` | — |
| 字幕导出（srt/vtt/txt） | `exporters/subtitle_exporter.py` | 多处入口：右键/托盘 |
| 转写文件落盘 | `transcript_writer.py` | — |
| 翻译基准测试 | `benchmark.py` | 基准 tab |
| 诊断打包 | `diagnostics.py` | 诊断 tab |

### 配置 / 文案 / 工程

| 我想改… | 主要文件 | ⚠️ 连带 |
|---|---|---|
| 默认配置 | `config.yaml` | — |
| 运行时设置（用户改的） | `user_settings.json`（运行时生成，优先于 config.yaml） | 读写经 `control_panel` / `settings_utils.py` |
| 任何界面文案 | `i18n/zh.yaml` **且** `i18n/en.yaml`（必须双语同步） | — |
| 更新日志 | `i18n/CHANGELOG_zh.md` + `CHANGELOG_en.md` | — |
| 路径解析（打包/开发/数据目录） | `runtime_paths.py` | — |
| Lint / pytest 配置 | `pyproject.toml` | — |
| 开发期启动（无终端黑框） | `start_silent.vbs`（双击，用 `pythonw.exe` 静默启动）；`start.bat` 仍保留用于看实时日志 | — |
| Agent 规则 / 文档防腐机制 | **工作区根** `../.kiro/steering/agent-rules.md`（always 注入）+ `../.kiro/hooks/maintain-dev-docs.kiro.hook`（agentStop） | 注意在工作区根 `e:\translate\.kiro\`，**不在** `LiveTranslate/` 内；改了规则要同步本文档顶部说明 |
| 打包 | `LiveTranslate.spec`、`scripts/build_windows.ps1`、`build_windows.bat` | — |
| 打可交付压缩包（onedir → 单 zip） | `scripts/make_release_zip.ps1` | 输出到工作区根 `../releases/`（项目目录外，避免被 `--clean` 清掉）；用 .NET ZipFile 支持 Zip64，别用 PS5.1 的 `Compress-Archive`（2GB 上限） |
| 打源码包（给人/AI 审查用） | `scripts/make_source_zip.ps1` | 取 git 已跟踪 + 未跟踪未忽略文件（含未提交源码），排除 `data/`、`*.db`、`user_settings.json`、`logs/`、`transcripts/`、`models/`；输出 `../releases/LiveTranslate-source.zip`（含 `assets/paper_collage` 全尺寸美术源图时 ~40MB，仅剔除源图可降到 ~10MB）。⚠️ 必须用 `git -c core.quotepath=false ls-files` 并以 UTF-8 解码 git 输出——否则仓库里的 `网站/` 等非 ASCII 路径会被八进制转义，导致 `Join-Path`/`Test-Path` 报"路径中具有非法字符"而打包失败 |

---

## 🔒 硬约束（违反会被后续 agent 当成债务）

1. **颜色 / 样式只走 `theme.py`**：应用外壳（控制面板、对话框、日志窗）的颜色、圆角、间距、
   按钮/输入框样式，必须用 `theme.py` 的 token（`Color.*`）和 helper（`panel_stylesheet()` 等）。
   **禁止**在各 UI 文件里新写死 hex/rgba/QSS 字符串。字幕本身的外观例外，走 `subtitle_presets.py`。
   （`theme.py` = 应用外壳的"装修"；`subtitle_presets.py` = 观众看到的字幕外观，两者别混。）
2. **文案必须双语同步**：任何用户可见字符串都走 `t("key")`，并同时在 `i18n/zh.yaml` 和
   `i18n/en.yaml` 添加。绝不在代码里硬编码界面文案。
3. **改完跑校验三件套**（ruff + pytest + --self-check），全绿才算完成。
4. **设置读写用原子写**：`user_settings.json` 经现有的原子写路径（写 `.tmp` 再 `os.replace`），不要绕过。
5. **不泄露密钥**：日志、诊断、设置打印都要过滤 `api_key` / `token` / `models` / `system_prompt`
   （已有 `logging_utils.redact_secret_data` / `add_secret_redaction`）。
6. **`import torch` 必须在 `import PyQt6` 之前**（仅限 `main.py` 顶部，已有注释）。
7. **跨线程改 UI 必须用 Qt 信号**，不要在管线/工作线程里直接碰控件。
8. **大文件暂不强制拆分**，但新增功能优先放到合适的现有子模块或新建模块，不要继续往
   `control_panel.py` / `main.py` 这两个巨型文件里堆。
9. **改完维护本文档**（见顶部规则 2）。

---

## ✅ 改动前检查清单

- [ ] 查了导航表，确认要动哪些文件 + ⚠️ 连带项
- [ ] 涉及 UI 颜色？→ 只改 `theme.py`
- [ ] 涉及文案？→ `zh.yaml` + `en.yaml` 都加了
- [ ] 涉及菜单项？→ 检查托盘/悬浮窗/面板是否有重复入口需同步
- [ ] 改完跑了 ruff + pytest + --self-check，全绿
- [ ] 更新了本文档对应章节

---

## 模块地图（实际文件 → 职责）

### 入口 / 核心
- `main.py` — 应用入口。`LiveTranslateApp`（管线、翻译调度、切句、内存监控、历史/成本、
  设置/模型/语言切换、划词翻译装配）+ 超长 `main()`（窗口/托盘/热键/信号装配）。**最大的两个文件之一。**
- `runtime_paths.py` — `resource_path()`（打包资源）/ `data_path()`（用户数据），统一路径解析。
- `settings_utils.py` — 设置相关纯函数（如 `should_persist_model_config`）。
- `logging_utils.py` — 日志 + 密钥脱敏。
- `model_manager.py` — 模型检测/下载/缓存、缓存环境变量、设备枚举。另含**硬件检测+推荐**：
  `detect_hardware()`（best-effort 探 GPU/显存/内存/CPU，全程 try/except 不抛）、`recommend_asr()`
  （按显存分档推荐 Whisper 尺寸 / 无 GPU 走 CPU SenseVoice / 低内存走云端 API；阈值对齐
  `WHISPER_VRAM_NEED_GB` 避免推荐与显存警告自相矛盾）。

### UI 层
- `subtitle_overlay.py` — 主透明悬浮窗（标题走 i18n `overlay_title`=「字幕窗口」）。含 `SubtitleOverlay` / `DragHandle`（纸感拼贴改版的
  分体式工具栏：顶栏只剩标题 + 📌 置顶切换 + ✕ 关闭；底栏 `build_bottom_bar()` 为 ▶/⏸ 播放暂停 ·
  透明度滑块（`opacity_changed` 信号实时改窗口透明度并持久化）· 🔊 朗读 · 🧹 清空 · Aa 详细切换 ·
  ⚙ 设置；model/源/目标语言下拉与若干复选框收进 Aa 展开的折叠行）/
  `ChatMessage`（消息气泡，纸感拼贴改版：原文/译文用 `_chip_stylesheet` 渲染的圆角纸签 +
  `_TornDivider` 撕纸分隔线（用纹理贴图 `overlay/textures/torn_strip.png` 拉伸成细纸条，右端长出
  `decorative/cropped/plant_branch.png` 植物嫩枝，缺失时回退手绘波浪线）；原文/译文纸签底纹用
  `overlay/textures/chip_paper.png`（`_chip_stylesheet` 经 border-image），纸签文字为暖墨色 `#5a5347`；
  译文纸签显示目标语言「译文（中文）」（`ChatMessage._target_language` 由 `set_target_language` 同步）；
  撕纸分隔线颜色取 `style["accent_color"]`）/
  `MonitorBar`（监控条）。底栏（`build_bottom_bar()`）按纸感拼贴稿仅放 播放 · 不透明度（`overlay_opacity`）滑块 ·
  Aa（详细切换）· 设置；朗读（`_tts_btn`）仍存在但默认隐藏（`setVisible(False)`，供托盘/设置 `set_tts_active`
  同步用），清空走气泡右键菜单。底栏/顶栏控件图标优先用 `_line_icon()` 自绘线性图标，避免字体缺字。
  空闲且已配置时显示"等待声音"空状态（`_build_empty_state`/
  `_refresh_empty_state`，用 `empty_subtitle_wave_strip.png` + i18n `overlay_waiting_sound`；
  与首启 welcome 卡互斥，来消息即隐藏）。
- `subtitle_window.py` — 独立字幕窗（给 OBS 捕获），`QPainterPath` 描边文字 + 动画。
- `subtitle_settings.py` — 独立字幕窗的设置 UI。
- `control_panel.py` — 设置中心。**单窗口 + 左侧分组侧边栏导航**（`QListWidget` + `QStackedWidget`），
  分 6 组：常用 / 外观 / 引擎 / 语音同传 / 数据 / 工具，共 13 个页面。导航分组在 `__init__` 的 `nav_spec` 定义，
  每个页面仍由各自的 `_create_*_tab()` 独立生成。**最大的两个文件之一。**
  TTS（语音同传）独立成「语音同传」组下的一级页面（`_create_tts_tab`）；启停开关经 `set_tts_enabled()` /
  `tts_enabled_changed` 信号 / `_on_tts_enabled_toggled` 与悬浮窗、托盘三处同步。
- `dialogs.py` — `SetupWizardDialog`（首启动向导）/ `ModelEditDialog`（模型增改）/
  `ModelDownloadDialog`（缺模型下载）/ `_ModelLoadDialog`（GPU 加载）。
- `log_window.py` — 实时日志查看器。
- `splash.py` — 启动 splash 画面（`StartupSplash`），冷启动期间显示品牌化加载画面，避免白屏假死感。
- `home_presets.py` — 快速开始页用的 ASR / LLM 服务商预设。`ASR_PRESETS` **只含云端引擎**
  （openai-audio/assemblyai/assemblyai-streaming）；本地引擎集合 `LOCAL_ASR_ENGINES` +
  `is_local_asr_engine()` 供首页判断是否改用只读"本地模型"哨兵条目。

### 设计系统（本次重构新增）
- `theme.py` — **应用外壳**设计系统：颜色 token、间距/圆角/字号、`ui_font_family()`、
  QSS 模板（`panel_stylesheet`/`dialog_stylesheet`/`console_stylesheet`/`primary_button_stylesheet`/
  `status_text_stylesheet`/`hint_text_stylesheet`/`step_card_stylesheet` 等）。
  - **纸感拼贴扩展（Paper Collage 美术包）**：`Color` 新增橄榄绿副强调（`OLIVE`/`OLIVE_HOVER`/
    `OLIVE_PRESSED`/`OLIVE_SOFT`）+ 装饰用 `SAND`/`LAVENDER`；侧边栏导航（`nav_sidebar_stylesheet`）
    改为奶油纸面 + 橄榄绿选中纸片 + 软橄榄 hover。新增 helper：`paper_card_stylesheet`、
    `empty_state_title_stylesheet`/`empty_state_text_stylesheet`、`nav_svg_icon(svg_path, size)`
    （把美术包 SVG 的描边色按导航状态着色成 Normal=灰 / Selected=白 的两态 `QIcon`，`lru_cache`）；
    首页改版又加了 `step_badge_stylesheet(bg)`（圆形数字徽章）、`step_card_title/subtitle_stylesheet`、
    `hero_overlay_title/subtitle_stylesheet`（叠在 Hero 图上的居中标题）、`tips_card_title_stylesheet`。
    橄榄绿是暖土色副强调，仍禁止冷蓝/青绿。
  - **视觉风格 = Anthropic / Claude 暖纸感**：浅色面用奶油纸底（`BG #f0eee6`）+ 暖白卡片，
    唯一强调色是赤陶土橙（`PRIMARY #c96442` / `ACCENT #d97757`），文字用暖近黑、边框是暖灰发丝线；
    深色面（悬浮窗/字幕窗/日志）用暖炭灰（`DARK_BG #1a1916`）而非冷黑，强调色同为赤陶橙。
    禁止再引入冷蓝/青绿。所有 hex 只允许出现在 `Color` 类里。
  - 悬浮窗（`subtitle_overlay.py`）有自己的一套深色常量（`_BTN_CSS`/`_COMBO_CSS`/`_CHECK_CSS`/
    `_MENU_CSS`/`_BAR_CSS_TPL` 等），已改为引用 `theme.Color`；标题栏（`DragHandle`）背景透明、
    融进容器玻璃，不再画第二层边框（`apply_style` 里也保持标题栏透明）。按钮为扁平 ghost 样式，
    仅"运行/暂停"主操作填充强调色。
- `subtitle_presets.py` — **字幕外观**数据：`DEFAULT_STYLE`、`STYLE_PRESETS`（15 套，含纸感暗色
  `paper_dark`，现为默认外观）、`hex_to_rgba()`。新增 `accent_color` 字段（纸签/撕纸分隔线颜色，默认赤陶橙）。
  注：`subtitle_overlay.py` 仍向后兼容再导出 `DEFAULT_STYLE`/`STYLE_PRESETS`（`__all__`）。
  默认外观落地：新装由向导 `dialogs.SetupWizardDialog._finish()` 写入 `style=paper_dark`；老用户由
  `control_panel._migrate_style()`（`_load_saved_settings` 内）把"旧版纯黑 default"一次性迁移到 `paper_dark`。
- `assets/paper_collage/` — **Paper Collage 美术资源包**（从 `../LiveTranslate_PaperCollage_ArtPack`
  复制）。`illustrations/cropped/`（首页 Hero `home_hero_paper_collage_1200x400.png`、三步插画
  `step_*.png`、空状态 `empty_*.png`）、`icons/svg/`（15 个侧边栏线条图标）、`decorative/cropped/`
  （纸条/便签/植物等装饰，目前备用）。打包随 `assets` 目录进 spec。

### 翻译
- `translator.py` — `Translator` 高层封装（上下文历史、切句、提示词、`RepetitionError`、
  `subtitle_error_label`、`make_openai_client`）。
- `providers/` — LLM provider 抽象：`base.py`（`LLMProvider`/`TranslateInput`/`TranslateResult`）、
  `openai_compatible.py`（含 `readable_provider_error` 错误归一化）、`ollama.py`。
- `prompts/` — `subtitle_prompt.txt` / `explain_prompt.txt` / `polish_prompt.txt`。

### 音频 / ASR / VAD
- `audio_providers/` — 平台音频捕获抽象：`factory.create_audio_capture()` 按平台分发到
  `windows_wasapi.py`（Windows）/ `macos_placeholder.py`（占位）。
- `audio_capture.py` — 捕获封装，设备变更自动重连。
- `vad_processor.py` — Silero VAD / 能量 / 关闭三种模式，渐进式静音 + 回溯切分。
- `asr_providers/` — ASR provider 抽象：`base.py` + `factory.py`（`ASRProviderConfig`/`resolve_device`）
  + `openai_audio.py`（云端 /audio/transcriptions）+ `assemblyai.py` / `assemblyai_streaming.py`。
- `asr_engine.py`（Whisper/ctranslate2）、`asr_sensevoice.py`、`asr_funasr_nano.py`、
  `asr_anime_whisper.py` — 本地 ASR 引擎实现。
- `funasr_nano/` — **vendored 第三方代码**（FunASR Nano），不 lint、不随意改。
- `audio_file_translate.py` — 上传音频文件 → 解码 16kHz mono → 分块 ASR → 翻译（UI 无关，可单测）。

### TTS（同声传译）
- `tts_controller.py` — **TTS 高层控制器**（本次拆分新增）。`TTSController` 拥有引擎实例 + 启停状态，
  集中了原先散在 `main.py` 里的同传**策略**：`apply_settings()`（方案/音色切换、音量、设备、启停）、
  `set_enabled()`（一键开关即时启停）、`clear()`/`stop()`（暂停/切目标语言/关闭时清队列）、
  `begin_stream(source, target)`（返回 `TTSStream` 或 `None`）。`TTSStream` 持单次翻译的流式状态：
  `feed(partial)` 边收流边用模块级 `extract_tts_sentences()` 切完整句即念，`finish(translated)` 收尾补念
  remainder（源=目标语言时不分句、整段念一次）。`LiveTranslateApp._translate_async` 只调这两个方法，
  不再直接碰引擎/分句逻辑。
- `tts_engine.py` — `TTSEngine`：独立线程合成+播放，有界队列丢旧帧保持低延迟。
- `tts_providers/` — `factory.create_tts_provider()`：`edge.py`（免费免 key，默认）/
  `openai_speech.py`（/audio/speech）/ `azure.py`（Azure 认知服务 REST，`Ocp-Apim-Subscription-Key`+region）/
  `elevenlabs.py`（`/v1/text-to-speech/{voice_id}`，`xi-api-key`）；`audio_utils.py` 解码到 PCM
  并提供代理感知的 `build_httpx_client()`（API 类 provider 共用）。
  **引擎能力表 `factory.ENGINE_CAPS`** 是单一数据源：每个引擎声明 `needs_key`/`fields`（要显示哪些字段）/
  `fetch`（"models"|"voices"|None）。编辑对话框的字段可见性 + 「获取」按钮行为都读它，新增引擎只改这张表
  + `create_tts_provider` 分发 + `fetch_tts_items` 三处，UI 自动跟随。
- 模型/音色导入：编辑对话框「获取」按钮统一经 `factory.fetch_tts_items(engine,...)` 分发——返回
  `(kind, items)`（kind=models→填模型框，voices→填音色框）：openai-speech 拉 `/models`、
  azure 拉 `cognitiveservices/voices/list`、elevenlabs 拉 `/v1/voices`（显示 `"名称 | voice_id"`，
  synthesize 时 `_voice_id()` 取出 id）、edge 按目标语言地区拉音色。均走工作线程，经对话框
  `_fetch_done` 信号回填。
- 多方案配置：`tts.profiles` 是一组合成方案（`name`/`engine`/`voice`/`rate`/`api_base`/
  `api_key`/`model`/`speed`/`proxy`/`region`），`tts.active_profile` 选中当前方案；全局播放项
  （`enabled`/`volume`/`output_device`）放在顶层。控制面板像翻译模型列表一样增/改/复制/删
  （`dialogs.TTSConfigEditDialog`）。`resolve_profile()` 兼容旧的扁平结构；
  `build_provider_config()` 构建当前方案的 `TTSProviderConfig`。`region` 仅 azure 用。
- **一键同传开关**：作为核心功能，TTS 启停不再只埋在设置里。三处入口：①悬浮窗工具栏「朗读」按钮
  （`DragHandle._tts_btn` + `tts_toggled` 信号，激活时填充强调色，经 `set_tts_active()` 回显）；
  ②托盘「同声传译朗读」可勾选项（`tray_tts`）；③设置中心「语音同传」组的启用复选框
  （`tts_enabled_changed` 信号 + `set_tts_enabled()`）。三者经 `main()` 的 `_sync_tts_ui()`（用
  `_syncing_tts` 标志防信号回环）+ `_apply_tts_toggle()`（调 `LiveTranslateApp.set_tts_enabled()`
  立即 start/stop 引擎并持久化）保持同步。

### 划词翻译 / 本地 API / 扩展
- `selection_translate/` — `service.py`（划词翻译服务）、`hotkey.py`+`hotkey_parser.py`（全局快捷键）、
  `clipboard.py`、`panel.py`（结果弹窗）、`local_server.py`（本地 HTTP API）。
- `browser-extension/` — Chrome 扩展（JS），通过本地 API 与应用通信。

### 数据 / 导出 / 诊断
- `history/db.py` — SQLite 历史记录。
- `exporters/subtitle_exporter.py` — srt/vtt/txt 导出。
- `transcript_writer.py` — 转写文件落盘（original/translation/all）。
- `benchmark.py` — 翻译基准测试。
- `diagnostics.py` — 诊断包（脱敏配置 + 环境信息）。

### 工程
- `pyproject.toml` — 项目元数据 + ruff + pytest 配置。
- `tests/` — 目前仅 `test_translator_logic.py`（UI/管线缺测试覆盖）。
- `scripts/` — 打包预检 / 构建 / 冒烟 / 验收脚本 + `make_release_zip.ps1`（onedir → 单 zip，Zip64）+ `make_source_zip.ps1`（源码包，给审查用）。
- `i18n/` — `zh.yaml` / `en.yaml` 文案 + 双语 CHANGELOG。

---

## 架构与线程模型

管线（后台线程）：**音频捕获(32ms 块) → VAD → ASR → 翻译(异步) → 悬浮窗/字幕窗/TTS**

- **主线程**：Qt 事件循环（所有 UI）。
- **管线线程**：`LiveTranslateApp` 内读音频、跑 VAD/ASR/翻译。
- **ASR 加载线程**：`_switch_asr_engine` 后台加载重模型（~3-8s）；`_asr_ready` 标志在加载期间丢弃片段。
- **TTS 线程**：`TTSEngine` 独立 daemon 线程合成+播放。
- **跨线程 UI 更新一律走 Qt 信号**（`add_message_signal` / `update_translation_signal` /
  `update_streaming_signal` 等）。

### 启动流程
1. `main.py` 读 `user_settings.json`，在 `import torch` 前调 `apply_cache_env()` 设置 `TORCH_HOME`。
2. 首次启动（无 `user_settings.json`）→ `SetupWizardDialog`（顶部显示**硬件检测横幅**，按
   `recommend_asr()` 给出推荐理由；低端机自动预选「云端 API」模式；完成时把推荐的 `asr_device`
   /`whisper_model_size` 一并落盘——注意 `asr_device` 不再写死 `"cuda"`）。
3. 非首次但缺模型 → `ModelDownloadDialog` 自动下载。
4. 模型就绪 → 创建主 UI（悬浮窗 / 面板 / 管线）。
5. 运行时切 ASR 引擎：未缓存→`ModelDownloadDialog`，再 `_ModelLoadDialog` 加载到 GPU。
6. 延迟初始化：ASR 加载与设置应用经 `QTimer.singleShot(100)`，避免启动卡死。

### 配置体系
- `config.yaml` — 基础默认（audio/asr/translation/subtitle/tts/local_api/hotkey/privacy）。
- `user_settings.json` — 运行时设置，加载时**优先于** config.yaml。分类：ASR/VAD、翻译模型列表、
  目标/源语言、TTS、快捷键、本地 API、隐私历史、转写、字幕样式(`style`)、窗口几何、缓存路径。
- 原子写：写 `.tmp` 再 `os.replace`，防崩溃损坏。

### 模型配置字段（`user_settings.json` 的 `models[]`）
`name` / `provider` / `api_base` / `api_key` / `model` / `proxy`（none|system|自定义 URL），及可选：
- `no_system_role`：把 system 合并进 user 消息（Qwen-MT 等拒绝 system 角色的 API）。
- `no_think`（默认 True）：`extra_body={"enable_thinking": False}` 关思考（Qwen3 等）。**仅 Qwen 系认识此参数**；
  OpenAI/Cerebras/Groq 等会以 HTTP 400 拒绝——provider 已做自愈：首次被拒即去掉该参数重试一次并
  在该实例上记住不再发送（见踩坑记录）。
- `streaming`（默认 True）：流式，经 `translate_iter()` 生成器产出增量。
- `json_response`（默认 False）：`response_format` 用 json_schema `{"t":"string"}`，与流式 UI 互斥。
- `context_turns`：多轮上下文对数。
- `input_price`/`output_price`：每 1M token 价格，用于 MonitorBar 成本估算。
- `overrides`（dict）：覆盖 temperature/top_p/max_tokens/frequency_penalty/presence_penalty/seed，
  只发存在的键。
- `extra_body`（dict）：供应商专有参数（thinking_budget 等），与 `no_think` 合并。
- 三条代码路径（`_translate_sync`/`_translate_streaming`/`translate_iter`）统一经
  `Translator._build_request_kwargs()` 组装请求。

---

## 关键踩坑记录（Key Patterns，改这些区域前务必看）

- **torch 必须在 PyQt6 之前 import**（Windows DLL 冲突，PyTorch 2.9.0+，见 pytorch#166628）。
- **缓存环境变量在 `main.py` 模块级、`import torch` 前设置**，否则 `TORCH_HOME` 不生效。
- **延迟初始化**：ASR 加载与设置应用经 `QTimer.singleShot(100)`，防启动冻结。
- `make_openai_client()`（`translator.py`）是唯一的代理感知 OpenAI 客户端创建处（翻译+基准共用），默认 10s 超时。
- **`enable_thinking` 只有 Qwen 系认识，其它服务会 400——provider 自动规避**：模型配置 `no_think=True` 会让
  `OpenAICompatibleProvider._build_request_kwargs` 塞 `extra_body={"enable_thinking": False}`，但 OpenAI/
  Cerebras/Groq 等不认识该参数，会整请求报 400（如 Cerebras：`enable_thinking: property ... is unsupported`，
  `code: wrong_api_format`），导致**每一句翻译都失败**。已修（自愈式，不维护厂商白名单）：`translate()` 和
  `stream_translate()` 在首次请求被拒时，用 `is_enable_thinking_unsupported_error()`（识别 400/422 且报文含
  `enable_thinking`）判断，置 `self._enable_thinking_supported=False` 后**去掉该参数重试一次**；该实例之后不再
  发送。`no_think=False` 时本就不发。注意：探测是 per-provider-instance 的，换模型/重建 provider 会重新乐观尝试一次。
- 模型缓存检测（`is_asr_cached`/`get_local_model_path`）同时查 ModelScope 和 HuggingFace 路径，避免切 hub 重复下载。
- **硬件推荐的尺寸分档必须对齐 `WHISPER_VRAM_NEED_GB`**：`recommend_asr()` 的显存阈值（≥10→large-v3、
  ≥5→medium、否则 small）刻意与 `_update_whisper_vram_warning` 用的需求表一致。早期版本用 ≥8 推 large-v3，
  导致 8GB 卡（如 RTX 2070）既被推荐 large-v3 又被显存警告标红，自相矛盾。改这两处任一都要同步另一处。
  另：`detect_hardware()` 所有探测（torch/psutil）必须包 try/except——CI/无依赖环境会缺 torch，绝不能抛。
- 设置日志输出过滤 `models` 和 `system_prompt`，防泄露密钥。
- **FunASR Nano** 在 `AutoModel()` 前 `os.chdir(model_dir)`，让相对路径本地解析、不触发 HF Hub 联网。
- **FunASR `generate()` 必须 `disable_pbar=True`**——tqdm 在 GUI 进程刷 stderr 会崩。
- **音频块 = 32ms（16kHz 下 512 样本）**，匹配 Silero VAD 原生窗口，延迟最低。
- **ASR 引擎生命周期**：每个引擎暴露 `unload()`（移 CPU+释放）和 `to_device(device)`（原地迁移）。
  切设备：PyTorch 引擎（SenseVoice/FunASR）用 `to_device()`，ctranslate2(Whisper)整体重载。
  释放顺序：`unload()` → `del` → `gc.collect()` → `torch.cuda.empty_cache()`。
  **`to_device()` 契约：成功返回 `True`、失败返回 `False`（调用方据此 `_asr_type=None` 触发整体重载）。
  必须用 try/except 包住 `.to(device)`，否则迁移异常会冒泡打断 `_on_settings_changed` 后续处理
  （audio_device/asr_engine 等全部不执行）。anime-whisper 是正确范本。**
- **Whisper(ctranslate2) 只接受 `device="cuda"` 不接受 `"cuda:0"`**；索引经 `device_index` 传，
  从 combo 文本 `"cuda:0 (RTX 4090)"` 解析。
- ASR 文本密度过滤：≥2s 却只产出 ≤3 个字母数字字符的片段当噪声丢弃。
- **AssemblyAI 流式引擎 = ASR 引擎下拉 index 7**：`assemblyai-streaming` 是后加的引擎，几处只
  按 `(5, 6)`（openai-audio/assemblyai）判断的门控曾漏了它，导致**实际用起来不通**：①高级 ASR tab
  的「云端 API」配置组（`_asr_api_group`，含 API Key/Base/Model）选中流式引擎时不显示，用户无处填 key；
  ②欢迎卡的就绪判断 `_evaluate_setup_status` 把它当本地引擎（无需 key），没填 key 也不提示，但
  `AssemblyAIStreamingASRProvider` 运行时会抛 "API Key is empty" 且字幕全空。已修：`_create_*_tab`
  初始可见性、`_on_engine_changed_asr_api_vis`、`_update_asr_api_placeholders`（补 wss/u3-rt-pro 占位）
  统一改成 `(5, 6, 7)`，`_evaluate_setup_status` 的 key 判断补 `assemblyai-streaming`，
  `model_manager.ASR_DISPLAY_NAMES` 补 `assemblyai-streaming` 条目（否则悬浮窗状态栏显示原始 key）。
  **新增任何 ASR API 引擎都要同时过这几处门控 + index 映射（`engine_map_idx`/`engine_combo_idx`）
  + `ASR_DISPLAY_NAMES`。**
- **AssemblyAI 流式 speech_model 校验**：`asr_providers/assemblyai_streaming.py` 的 `speech_model`
  只接受固定白名单（`u3-rt-pro` / `universal-streaming-english|multilingual` / `whisper-rt` 等）。
  构造时经 `_normalize_model()` 校验，非法值（如误从 OpenAI 预设带过来的 `gpt-4o-transcribe`、
  `whisper-1` 或空值）会回退到 `u3-rt-pro` 并记 WARNING——否则服务端会逐片段拒绝、字幕全空但
  界面无提示。踩坑背景：home tab 的 ASR 预设共用同一个 `asr_api.model` 字段，切换服务商时
  OpenAI 的 model 名可能残留进 AssemblyAI 配置。
- **首页 ASR 卡只为云端引擎设计，本地引擎要走"哨兵条目 + 写回保护"**：`home_presets.ASR_PRESETS`
  只含云端引擎，没有本地引擎（sensevoice/whisper/funasr/anime）。早期 `_create_home_tab` 在
  `find_asr_preset()` 返回 -1（实际跑本地引擎）时兜底 `cur_asr_idx=0`，导致**首页显示成
  "OpenAI Whisper"**（与实际运行的本地引擎不符）；更糟的是用户一旦在该卡片改任意字段就触发
  `_on_home_asr_field_changed`，其结尾**无条件** `asr_engine="openai-audio"` + `_auto_save()`，
  把本地引擎配置静默覆盖成云端、造成配置丢失。已修（方案 A）：本地引擎时在「服务商」下拉首位插入
  一条只读哨兵条目「本地模型：<ASR_DISPLAY_NAMES 显示名>」（`setItemData(0,"local")`）并选中，
  禁用 Base/Key/Model/测试/获取模型（`_apply_home_asr_field_state`）；所有把 combo 下标当
  `ASR_PRESETS` 下标用的地方统一走 `_home_asr_preset_index()`（哨兵返回 -1，有哨兵时整体偏移 1）；
  `_on_home_asr_field_changed` / `_on_home_asr_preset` 开头 `if pidx<0: return` 守卫，**绝不**在哨兵态
  下改写 `asr_engine`/`asr_api`。本地引擎之间的切换仍只归高级页（ASR & VAD）`_asr_engine` 下拉。
  注意：初始化时 `currentIndexChanged` 在 `setCurrentIndex` 之后才 connect，故"仅打开页面不操作"
  不触发写回——改这段时务必保持该顺序。新增本地引擎记得同时加进 `home_presets.LOCAL_ASR_ENGINES`
  和 `model_manager.ASR_DISPLAY_NAMES`。
- **字幕窗多语言翻译不要复用 `self._tl_executor`**：`_translate_extra_langs` 在 `_tl_executor`
  的 worker 内运行并 `as_completed` 阻塞等子任务，若子任务再提交回同一个池，会在所有 worker 都被
  父任务占满时自死锁（父等子、子无 worker 可跑）。已改为用独立的短生命周期 `ThreadPoolExecutor`。
- **字幕窗额外目标语言的辅助 `Translator` 要缓存**：`with_target_language()` 会重建 Translator 及其
  httpx 连接池，若每段每语言新建会造成连接 churn + 额外握手延迟。已用 `self._aux_translators`
  （按 lang 缓存，`_aux_translators_lock` 保护）复用，仅在 `_on_model_changed`（换模型）和
  glossary 变更时清空失效。
- **划词热键要防重入**：`copy_selected_text()` 内部用嵌套 QEventLoop（`wait_ms`）等剪贴板，期间
  主事件循环可重入。快速连按热键会让第二次 `_on_selection_hotkey` 重入、两个 clear/copy/restore
  交错，破坏用户剪贴板或读到空文本。`_on_selection_hotkey` 用 `_selection_busy` 标志守卫。
- **子包里的对话框文案也必须走 i18n**：`selection_translate/panel.py`（划词结果弹窗）一度把
  标题/按钮/状态/字段标签全硬编码成中文，英文 UI 用户按全局热键弹出的是全中文窗口（违反双语硬约束）。
  已改为走 `t()`（复用 `copy_translation`/`copy_all`/`btn_close`/`history_*`，新增 `sel_dialog_title`/
  `sel_loading`/`sel_error`/`sel_done`）。注意：硬编码字符串容易藏在 `selection_translate/`、
  `tts_providers/` 等**子包**而非主 UI 文件里——新增任何用户可见文案，无论在哪个文件，都要走 i18n 双语。
- **`get_local_model_path` 选 HF 快照要校验有效性**：优先返回含 `config.json` 的快照，避免把空的/
  下载中断的残缺快照目录交给 ASR 加载器（否则报费解错误，而非回退到"未缓存→重新下载"）。
- **脱敏覆盖 URL 内嵌凭据**：`logging_utils` 除按 `SECRET_KEYS`（api_key/token/...）和 `sk-` 模式
  脱敏外，还会遮蔽 URL userinfo（如自定义代理 `http://user:pass@host`）的 `user:pass@`，避免代理
  凭据明文进日志和诊断包（`user_settings.redacted.json` 会完整序列化 models 含 proxy）。
- **列表删除项要修正 active 索引漂移**：删 TTS 方案（`_remove_tts_profile`）或翻译模型
  （`_remove_model`）时，若删除位置在 active 之前，active 索引必须 -1，否则会静默指向另一个条目
  （激活的方案/模型被悄悄换掉）。`_remove_model` 删掉的若正是 active 模型，还要 `model_changed`
  重建运行时翻译器，否则 overlay 显示的激活模型与实际翻译用的不一致。
- **样式预设回退索引用 `_preset_keys.index("custom")`**：预设里 custom 始终是最后一项（加入
  `paper_dark` 后是 index 15），早期 `_create_style_tab` 把未识别 preset 的回退写死成
  `setCurrentIndex(5)`（实际是 nord），已改为按 `_preset_keys` 动态定位。新增预设记得同时在
  `subtitle_presets.STYLE_PRESETS`、`_create_style_tab` 的 `preset_names` 和 i18n `preset_*` 三处加。
- **基准测试流式要跳过空 choices**：`benchmark.py` 流式读取时部分 OpenAI 兼容服务会发 `choices: []`
  的 keep-alive/usage 块，必须 `if not chunk.choices: continue`，否则 `choices[0]` 抛异常被外层
  `except` 接住、退回非流式重跑同一句，导致重复请求 + 计时污染。TTFT 应在首个有内容 token 计。
- **字幕窗 enabled/位置要回灌 panel widget**：托盘开关/拖动窗口经 `_save_subwin_state` 更新了
  `panel._current_settings["subtitle_mode"]`，但还要调 `SubtitleSettingsWidget.sync_external_state()`
  把 enabled/window_x/y 灌回 widget 内部 `_settings`，否则之后在面板改任意字幕样式时
  `_emit_settings` 会发出陈旧的 enabled/位置，静默把窗口可见性/位置回滚。
- **改目标语言要同步 panel 内存状态**：从悬浮窗/托盘下拉切换目标语言时，除了写盘还必须更新
  `panel._current_settings["target_language"]`。`get_settings()` 返回的是浅拷贝，只改拷贝写盘的话
  panel 内存仍是旧值，下次控制面板任意设置触发 auto-save（整体覆盖写）会把目标语言悄悄回滚。
  托盘路径 `_on_tray_lang_switch` 一直是对的；`_on_target_language_changed` 之前漏了，已补。
- **悬浮窗底栏透明度滑块持久化同理**：`overlay.opacity_changed` → main 的 `_save_overlay_opacity`
  必须同时更新 `panel._current_settings["style"]["window_opacity"]` 并回灌 `panel._window_opacity`
  控件，否则面板下次 auto-save 整体覆盖会把滑块改的透明度回滚（与上一条同根问题）。
- **改字幕外观要认清运行时实际读的是 `user_settings.json["style"]`，不是 `STYLE_PRESETS` 代码默认值**：
  改 `paper_dark` 等预设的色值后，已有用户若 `style.preset` 仍是别的（如旧版 `default` 纯黑），
  运行时根本不会用到你改的预设——会"改了没效果"。`STYLE_PRESETS`/`DEFAULT_STYLE` 只是基线，
  真正生效的是存盘的 `style`。所以：①新装默认走向导写盘的 `style`；②老用户靠 `_migrate_style()`
  迁移；③调试时先确认 `user_settings.json` 里的 `style.preset`/`bg_color`，或在设置中心切到对应预设。
- `stop()` 先 join 管线线程再 flush VAD，防 `_process_segment` 并发。
- 取消 ASR 下载/加载失败时，若旧引擎仍在则恢复 `_asr_ready`。
- `Translator._build_system_prompt` 捕获用户模板格式错误，回退 `DEFAULT_PROMPT`。
- `translate_iter()` 是产出累积部分文本的生成器；`translate()` 是阻塞等价物。
- 流式 UI：`update_streaming_signal` → `ChatMessage.update_streaming()`（50ms QTimer 节流），
  `set_translation()` 定稿。
- `RepetitionError`：模型输出出现长度 8+ 重复循环时抛出；`_translate_async` 捕获并向用户提示。
- **同传 TTS 流式分句（降同传延迟）**：分句策略已抽到 `tts_controller.py`。`_translate_async` 经
  `self._tts.begin_stream(source, target)` 取得 `TTSStream`（禁用时为 `None`），流式里 `feed(partial)`
  从未朗读的 remainder 里切出**完整句**（CJK `。！？…` / 西文 `!?` / 西文 `.` 仅在「后接空白且前一字符非数字」
  时才算句末，避免小数/缩写碎句）立即送 `TTSEngine.speak`，流结束 `finish(translated)` 只补念剩余 remainder。
  仅在跨语言（`source != target`）时分句，源=目标语言时整段念一次。`TTSEngine` 是有界 FIFO 队列、
  `speak()` 线程安全，分句多次入队天然按序播放。改触发逻辑时注意：①别在 `finish` 外再 speak 整段
  `translated`（会重复念）；②`TTSStream` 内部用 `_spoken_len` 跟踪已消费的流式前缀长度、`_remainder` 累加增量，
  别用整段 partial 重复切。切句纯函数 `extract_tts_sentences()` 在 `tts_controller` 模块级，可单测。
- **翻译错误提示**：provider 层抛错经 `readable_provider_error()` 归一成可读异常；字幕区再经
  `subtitle_error_label()` 映射成简短标签（`error_tl_network`/`timeout`/`auth`/`ratelimit`/`server`/
  `generic`），完整原因写日志。
- 更新日志：`i18n/CHANGELOG_{lang}.md` 在设置→更新日志 tab 渲染为 HTML。

### 增量 ASR（`incremental_asr` 开启时）
- 管线每 ~1s 在 VAD 累积语音时调 `_do_interim_asr()`（buffer > `interim_interval`）。
- 切句用 `pysbd`（规则，23 语言）+ 逗号回退：CJK `、` 25 字阈值、西文 `,，;；` 60 字阈值，
  两种回退都要求 before>15、after>3 字避免碎片。
- 提交句子后按比例裁剪音频 + 0.3s 余量减回声，保留 ≥0.5s。
- 回声去重：`_strip_committed_overlap()` 用已提交尾部匹配新文本前缀。
- 短碎片（≤8 字母数字）缓存进 `_interim_pending`，拼到下一句前。

### VAD 行为
- 渐进式静音：buffer 越长接受越短停顿切分（<3s=全，3-6s=半，6-10s=四分之一 silence_limit）。
- 自适应静音：跟踪近期停顿，阈值设为 P75×1.2，在 0.3s~2.0s 间自调。
- 回溯切分：到最大时长时回溯平滑置信度历史找最低谷切，余下保留到下段。
- 语音密度过滤：`_flush_segment()` 丢弃 <25% 块超过置信阈值的片段。
- 短段合并：低于 `min_speech_duration` 的段**不丢弃**，VAD 软复位（`_is_speaking=False`）但留 buffer，
  自然与下次语音起始合并。
- `_was_trimmed` 标志（`trim_front` 设）确保被裁剪的增量 ASR 余量走 `force_flush()` 而非被 min_speech 丢弃。

### 设置 UX
- 防抖自动保存：面板设置 300ms 防抖（`_auto_save`→`_do_auto_save`），无需保存按钮。
- 滑块特殊处理：实时更新标签，但仅在 `sliderReleased`（鼠标）或键盘输入时触发保存。
- 提示词自动应用：System prompt 600ms 防抖（`textChanged`）。
- `QDoubleSpinBox` 保存时 `round()` 到 2 位小数，避免浮点漂移。
- **下拉框禁滚轮**：快速开始页的服务商/模型/目标语言下拉用 `control_panel.NoScrollComboBox`
  （`wheelEvent` 里 `event.ignore()`），防止鼠标悬停在框上滚页面时误切模型/厂商；下拉展开后的
  列表仍可正常滚动（是独立 view）。新增此类下拉时优先用 `NoScrollComboBox`。

---

## 已知信息架构问题（待后续优化的方向，非 bug）

> 这些是当前菜单/IA 的债务，记录在此供后续重设计参考：

- **入口三处重叠**：模型切换、语言切换、显隐控制、导出在悬浮窗/托盘/控制面板都有，
  靠大量信号双向同步（`main()` 巨函数膨胀的主因）。
- **功能放错位置**：划词翻译+快捷键塞在"翻译"tab；转写自动保存塞在"缓存"tab。
- **入口缺失**：浏览器扩展应用内零入口；本地 API 只有 Token 框，无开关/端口/状态显示。

> ✅ 已解决（2026-05 菜单重构）：设置中心从"5 顶层 tab + 独立高级窗口 7 子 tab = 12 碎片面板"
> 重构为**单窗口左侧分组侧边栏导航**（常用/外观/引擎/语音同传/数据/工具，6 组 13 页）。TTS 先从二级子 tab
> 提为引擎组下的一级页面，后又因属核心功能**单独拆成「语音同传」分组**，并补了悬浮窗/托盘一键开关。
> 所有 `_create_*_tab` 内部逻辑未动，仅替换了外层容器。

## 语言 / 风格约定

- 对用户用中文回复。
- 代码注释仅在关键处用英文。
- Commit message 不加 Co-Authored-By。
