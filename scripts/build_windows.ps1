param(
    [switch]$InstallBuildDeps,
    [switch]$OneFile,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

$Python = $PythonPath
if (-not $Python) {
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $Python = $VenvPython
    } else {
        $Python = "python"
    }
}

Write-Step "Checking Python"
& $Python --version | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Python not found. Run install.bat first or pass -PythonPath <path-to-python.exe>."
}

if ($InstallBuildDeps) {
    Write-Step "Installing build dependencies"
    & $Python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller"
    }
}

Write-Step "Checking PyInstaller"
& $Python -m PyInstaller --version | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: .\scripts\build_windows.ps1 -InstallBuildDeps"
}

Write-Step "Checking runtime dependencies"
& $Python "scripts\dependency_check.py" --strict
if ($LASTEXITCODE -ne 0) {
    throw "Runtime dependency check failed. Run install.bat first."
}

Write-Step "Running smoke check"
& $Python "scripts\smoke_check.py"
if ($LASTEXITCODE -ne 0) {
    throw "Smoke check failed"
}

Write-Step "Running package preflight"
& $Python "scripts\package_preflight.py"
if ($LASTEXITCODE -ne 0) {
    throw "Package preflight failed"
}

Write-Step "Running runtime acceptance check"
& $Python "scripts\runtime_acceptance.py"
if ($LASTEXITCODE -ne 0) {
    throw "Runtime acceptance check failed"
}

$Args = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--splash", "assets\startup-splash.png",
    "--name", "LiveTranslate",
    "--add-data", "config.yaml;.",
    "--add-data", "i18n;i18n",
    "--add-data", "prompts;prompts",
    "--add-data", "assets;assets",
    "--add-data", "funasr_nano;funasr_nano",
    "--add-data", "browser-extension;browser-extension",
    "--add-data", "docs;docs",
    "--add-data", "screenshot;screenshot",
    "--hidden-import", "pyaudiowpatch",
    "--hidden-import", "PyQt6",
    "--hidden-import", "asr_engine",
    "--hidden-import", "asr_sensevoice",
    "--hidden-import", "asr_funasr_nano",
    "--hidden-import", "asr_anime_whisper",
    "--hidden-import", "asr_providers.openai_audio",
    "--hidden-import", "asr_providers.assemblyai",
    "--hidden-import", "asr_providers.assemblyai_streaming",
    "--hidden-import", "websocket",
    "--hidden-import", "home_presets",
    "--hidden-import", "audio_file_translate",
    "main.py"
)

if (-not $OneFile) {
    $Args = @("--onedir") + $Args
} else {
    $Args = @("--onefile") + $Args
}

Write-Step "Building LiveTranslate"
& $Python -m PyInstaller @Args
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed"
}

Write-Step "Build complete"
Write-Host "Output: $ProjectDir\dist\LiveTranslate" -ForegroundColor Green

Write-Step "Verifying build output"
& $Python "scripts\verify_build_output.py" "$ProjectDir\dist\LiveTranslate"
if ($LASTEXITCODE -ne 0) {
    throw "Build output verification failed"
}

Write-Step "Running packaged self-check"
$SelfCheckData = Join-Path $ProjectDir "dist\self-check-data"
if (Test-Path $SelfCheckData) {
    Remove-Item -LiteralPath $SelfCheckData -Recurse -Force
}
$env:LIVETRANSLATE_DATA_DIR = $SelfCheckData
& "$ProjectDir\dist\LiveTranslate\LiveTranslate.exe" --self-check
$SelfCheckExit = $LASTEXITCODE
Remove-Item Env:\LIVETRANSLATE_DATA_DIR -ErrorAction SilentlyContinue
if (Test-Path $SelfCheckData) {
    Remove-Item -LiteralPath $SelfCheckData -Recurse -Force
}
if ($SelfCheckExit -ne 0) {
    throw "Packaged self-check failed"
}
