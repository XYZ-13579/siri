$ErrorActionPreference = "Stop"
$ReleaseDir = "Release"

Write-Host ""
Write-Host "====================================="
Write-Host " Build: listener.exe only"
Write-Host "====================================="
Write-Host ""

# ------------------------------------------------------------------
# 1. listener.py → listener.exe を PyInstaller でビルド
# ------------------------------------------------------------------
Write-Host "[1/2] Building listener.exe with PyInstaller..."
pyinstaller --onefile --noconsole listener/listener.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

# ------------------------------------------------------------------
# 2. Release フォルダへコピー
# ------------------------------------------------------------------
Write-Host "[2/2] Copying listener.exe to Release folder..."

if (-not (Test-Path $ReleaseDir)) {
    New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
}

if (!(Test-Path "dist\listener.exe")) {
    throw "dist\listener.exe not found. Build may have failed."
}

Copy-Item "dist\listener.exe" "$ReleaseDir\listener.exe" -Force

Write-Host ""
Write-Host "====================================="
Write-Host " listener.exe updated successfully."
Write-Host " Output : $ReleaseDir\listener.exe"
Write-Host "====================================="
