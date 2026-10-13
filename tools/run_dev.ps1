#Requires -Version 5.1
<#
.SYNOPSIS
  Starts the backend service and the Godot client for development.
.PARAMETER Editor
  Open the Godot editor instead of running the main scene.
.PARAMETER NoBackend
  Do not spawn the backend (e.g. it is already running under a debugger).
.DESCRIPTION
  The backend creates its own session token and publishes it, with its port, in a discovery record that
  only the current user can read (docs/protocol/attach.md). The client is started with --attach and finds
  that record itself; no token is ever passed on a command line.
#>
param(
    [int]$Port = 8765,
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
        -ArgumentList @("run", "coypu-builder-backend", "serve", "--port", "$Port") `
        -WorkingDirectory (Join-Path $root "backend")

    # Wait until the backend has published its discovery record (the record is read here only to compare
    # its port and pid; the token in it is never printed).
    $runtime = Join-Path $env:LOCALAPPDATA "COYPU Builder\runtime"
    $deadline = (Get-Date).AddSeconds(60)
    $published = $false
    while (-not $published -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
        foreach ($file in @(Get-ChildItem -Path $runtime -Filter "backend-*.json" -ErrorAction SilentlyContinue)) {
            try {
                $record = [System.IO.File]::ReadAllText($file.FullName) | ConvertFrom-Json
                if ($record.port -eq $Port -and (Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) {
                    $published = $true
                }
            } catch { }
        }
    }
    if (-not $published) { throw "the backend did not publish a discovery record on port $Port within 60 s" }
}

$godot = Get-ChildItem (Join-Path $PSScriptRoot "godot") -Filter "Godot_v*_win64.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $godot) { throw "Godot not installed; run tools\install_godot.ps1 first" }

$godotArgs = @("--path", (Join-Path $root "client"))
if ($Editor) {
    $godotArgs += "--editor"
} else {
    $godotArgs += @("--", "--attach", "--backend-url", "ws://127.0.0.1:$Port")
}
& $godot.FullName @godotArgs
