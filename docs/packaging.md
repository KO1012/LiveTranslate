# Windows 打包说明

当前阶段仍是 Python 桌面程序，Windows 打包使用 PyInstaller。

## 前置条件

先完成普通开发环境安装：

```powershell
.\install.bat
```

如果本机还没有构建工具，先安装 PyInstaller：

```powershell
.\scripts\build_windows.ps1 -InstallBuildDeps
```

## 构建

```powershell
.\scripts\build_windows.ps1
```

默认优先使用 `.venv\Scripts\python.exe`；如果项目虚拟环境不存在，会回退到当前 `python`。也可以显式指定：

```powershell
.\scripts\build_windows.ps1 -PythonPath C:\Path\To\python.exe
```

默认输出：

```text
dist/LiveTranslate/
```

构建脚本会先自动运行：

- `scripts/dependency_check.py --strict`
- `scripts/smoke_check.py`
- `scripts/package_preflight.py`
- `scripts/dependency_check.py --strict` 会在 PyInstaller 前检查 PyQt6、PyAudioWPatch、faster-whisper、torchaudio、FunASR 等运行依赖；缺少必需依赖时会列出缺失项并停止构建。
- `scripts/package_preflight.py` 会同时检查默认 `translation.api_key`、`asr_api.api_key` 和 `local_api.token` 为空，避免把用户密钥打进发行产物。

如果默认配置里带了 API Key / 本地 Token，或打包脚本误加入了 `user_settings.json`、`data/`、`logs/`、`transcripts/` 这类用户数据，构建会直接失败。

## 打包内容

脚本会带上这些运行资源：

- `config.yaml`
- `i18n/`
- `prompts/`
- `funasr_nano/`
- `browser-extension/`
- `docs/`
- `screenshot/`

不会主动打包用户运行时数据：

- `user_settings.json`
- `data/history.db`
- `logs/`
- `transcripts/`
- 用户 API Key

打包后运行时，`user_settings.json`、`data/`、`logs/`、`transcripts/`、`models/` 默认写到 `%APPDATA%\LiveTranslate\`；`_internal/` 只放只读运行资源。可用 `LIVETRANSLATE_DATA_DIR` 覆盖运行时数据目录。旧版本如果曾把这些文件写到 `LiveTranslate.exe` 同级目录，新版本首次启动会自动复制到 `%APPDATA%\LiveTranslate\`。

## 发布前检查

1. 运行离线自检：

```powershell
.\smoke_check.bat
```

2. 运行发布预检：

```powershell
python scripts\package_preflight.py
```

3. 构建完成后检查产物资源和用户数据：

```powershell
python scripts\verify_build_output.py
dist\LiveTranslate\LiveTranslate.exe --self-check
```

4. 在干净目录运行 `LiveTranslate.exe`。
5. 配置一个 OpenAI Compatible 或 Ollama 模型。
6. 验证字幕窗口可以打开。
7. 验证本地 API：

```text
GET http://127.0.0.1:17891/api/health
```

8. 按 `docs/chrome_extension_install.md` 加载插件并测试右键翻译。
9. 在控制面板“诊断”页导出诊断包，确认 zip 包含脱敏后的配置摘要，且不包含历史数据库、转写文本和完整 API Key。

## 打成可交付压缩包

`dist\LiveTranslate\` 整个 onedir 目录就是可交付物（含 `LiveTranslate.exe` + `_internal/`）。
要打成单个分发文件，用：

```powershell
.\scripts\make_release_zip.ps1
```

默认输出到工作区根的 `..\releases\LiveTranslate-windows-x64.zip`（**故意放在项目目录外**，避免下次 `--clean` 重建时被清掉），压缩包内带顶层 `LiveTranslate\` 目录。脚本用 .NET `System.IO.Compression.ZipFile`（支持 Zip64），不要用 Windows PowerShell 5.1 自带的 `Compress-Archive`——它有约 2GB 上限，会在本项目 4GB+ 的产物上失败。

## 产物体积说明

- 带 **CUDA** 版 torch 的 venv 打出来约 **4.5GB**（`torch_cuda.dll` ≈1GB，外加 cuDNN/cuBLAS/cuFFT 等 CUDA 运行库）。这是为了核心的 GPU 加速能力，属预期体积。
- 若 venv 装的是 **CPU 版** torch，产物会小很多（约 1.2GB），但没有 GPU 推理。
- 体积主要由 torch + CUDA 运行库决定，与是否内嵌 ASR 模型无关（模型默认运行时下载到 `%APPDATA%\LiveTranslate\`，不打进包）。

## 已知限制

- 当前脚本只处理 Windows Python 版打包。
- ASR 模型和 Torch 体积很大，不建议直接内嵌到安装包。
- macOS 打包等真实音频捕获实现后再单独处理。
