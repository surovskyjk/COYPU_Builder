#Requires -Version 5.1
<#
.SYNOPSIS
  Starts the backend service and the Godot client for development.
.PARAMETER Editor
  Open the Godot editor instead of running the main scene.
.PARAMETER NoBackend
  Do not spawn the backend (e.g. it is already running under a debugger).
#>
param(
    [int]$Port = 8765,
    [string]$Token = "dev",
    [switch]$Editor,
    [switch]$NoBackend
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    $uv = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe"
}
if (-not (Test-Path $uv)) {
    # winget's Links shim isn't always present; fall back to the versioned Packages install directory.
    $packaged = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages") `
        -Filter "uv.exe" -Recurse -Depth 1 -ErrorAction SilentlyContinue |
        Where-Object { $_.Directory.Name -like "astral-sh.uv_*" } |
        Select-Object -First 1
    if ($packaged) { $uv = $packaged.FullName }
}
if (-not (Test-Path $uv)) { throw "uv not found; install with: winget install --id astral-sh.uv -e" }

if (-not $NoBackend) {
    Write-Host "Starting backend on ws://127.0.0.1:$Port"
    Start-Process -FilePath $uv `
        -ArgumentList @("run", "coypu-builder-backend", "serve", "--port", "$Port", "--token", $Token) `
        -WorkingDirectory (Join-Path $root "backend")
}

$godot = Get-ChildItem (Join-Path $PSScriptRoot "godot") -Filter "Godot_v*_win64.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $godot) { throw "Godot not installed; run tools\install_godot.ps1 first" }

$godotArgs = @("--path", (Join-Path $root "client"))
if ($Editor) {
    $godotArgs += "--editor"
} else {
    $godotArgs += @("--", "--backend-url", "ws://127.0.0.1:$Port", "--backend-token", $Token)
}
& $godot.FullName @godotArgs
