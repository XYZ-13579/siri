$ErrorActionPreference = "Stop"
$ReleaseDir = "Release"

Write-Host ""
Write-Host "====================================="
Write-Host " Build: assistant.exe only"
Write-Host " (Rust + Tauri + index.html)"
Write-Host "====================================="
Write-Host ""

# ------------------------------------------------------------------
# 1. Tauri (Rust + フロントエンド) をビルド
# ------------------------------------------------------------------
Write-Host "[1/2] Building Tauri application (npm run tauri build)..."
cmd /c npm run tauri build

if ($LASTEXITCODE -ne 0) {
    throw "Tauri build failed."
}

# ------------------------------------------------------------------
# 2. Release フォルダへコピー
# ------------------------------------------------------------------
Write-Host "[2/2] Copying assistant.exe to Release folder..."

if (-not (Test-Path $ReleaseDir)) {
    New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
}

# assistant.exe を固定パスで直接コピー
$AssistantExePath = "src-tauri\target\release\assistant.exe"

if (-not (Test-Path $AssistantExePath)) {
    throw "assistant.exe not found at: $AssistantExePath"
}

Write-Host "  Copying: $AssistantExePath"
Copy-Item $AssistantExePath "$ReleaseDir\assistant.exe" -Force

Write-Host ""
Write-Host "====================================="
Write-Host " assistant.exe updated successfully."
Write-Host " Output : $ReleaseDir\assistant.exe"
Write-Host "====================================="
