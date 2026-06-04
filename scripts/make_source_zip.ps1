param(
    [string]$ProjectDir = "e:\translate\LiveTranslate",
    [string]$OutDir = "e:\translate\releases",
    [string]$ZipName = "LiveTranslate-source.zip"
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectDir
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

# Git quotes non-ASCII paths (e.g. the 网站/ dir) with octal escapes by default,
# which then breaks Join-Path/Test-Path with "illegal characters". Force raw UTF-8
# output and decode git stdout as UTF-8.
$prevOutEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    # Tracked + untracked-but-not-ignored files (so uncommitted source is included too)
    $files = @()
    $files += git -c core.quotepath=false ls-files
    $files += git -c core.quotepath=false ls-files --others --exclude-standard
    $files = $files | Sort-Object -Unique | Where-Object { $_ }
} finally {
    [Console]::OutputEncoding = $prevOutEnc
}

# Exclude runtime data / DBs / anything that could carry user data or secrets
$exclude = '(^|/)(data/|user_settings\.json$|.*\.db$|logs/|transcripts/|models/)'
$files = $files | Where-Object { $_ -notmatch $exclude }

Write-Host "Files to archive: $($files.Count)"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$zip = Join-Path $OutDir $ZipName
if (Test-Path $zip) { Remove-Item -Force $zip }

$archive = [System.IO.Compression.ZipFile]::Open($zip, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($f in $files) {
        $full = Join-Path $ProjectDir $f
        if (Test-Path -LiteralPath $full) {
            $entryName = "LiveTranslate/" + ($f -replace '\\', '/')
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive, $full, $entryName,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    }
} finally {
    $archive.Dispose()
}

$item = Get-Item $zip
"{0}  ({1:N2} MB, {2} files)" -f $item.FullName, ($item.Length / 1MB), $files.Count
