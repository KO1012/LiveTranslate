param(
    [string]$Source = "e:\translate\LiveTranslate\dist\LiveTranslate",
    [string]$OutDir = "e:\translate\releases",
    [string]$ZipName = "LiveTranslate-windows-x64.zip"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$zip = Join-Path $OutDir $ZipName
if (Test-Path $zip) { Remove-Item -Force $zip }

Write-Host "START $(Get-Date -Format o)"
# includeBaseDirectory=$true so the archive contains a top-level LiveTranslate\ folder
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $Source,
    $zip,
    [System.IO.Compression.CompressionLevel]::Fastest,
    $true
)
Write-Host "DONE $(Get-Date -Format o)"

$item = Get-Item $zip
"{0}  ({1:N1} MB)" -f $item.FullName, ($item.Length / 1MB)
