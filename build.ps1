$ErrorActionPreference = "Stop"

Write-Host "Building listener.exe..."
pyinstaller --onefile --noconsole listener/listener.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

Write-Host "Building Tauri Application..."
cmd /c npm run tauri build

if ($LASTEXITCODE -ne 0) {
    throw "Tauri build failed."
}

$ReleaseDir = "Release"

if (-not (Test-Path $ReleaseDir)) {
    New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
}

Write-Host "Copying files..."

# assistant.exe を固定パスで直接コピー
$AssistantExePath = "src-tauri\target\release\assistant.exe"

if (-not (Test-Path $AssistantExePath)) {
    throw "assistant.exe not found at: $AssistantExePath"
}

Write-Host "  Copying: $AssistantExePath"
Copy-Item $AssistantExePath "$ReleaseDir\assistant.exe" -Force

# listener.exe
if (!(Test-Path "dist\listener.exe")) {
    throw "listener.exe not found."
}

Copy-Item "dist\listener.exe" "$ReleaseDir\listener.exe" -Force

# llama
if (!(Test-Path "llama")) {
    throw "llama folder not found."
}

Copy-Item "llama" "$ReleaseDir\llama" -Recurse -Force

# model
if (!(Test-Path "model")) {
    throw "model folder not found."
}

Copy-Item "model" "$ReleaseDir\model" -Recurse -Force

Write-Host ""
Write-Host "====================================="
Write-Host " Build completed successfully."
Write-Host " Output : $ReleaseDir"
Write-Host "====================================="