#Requires -Version 5.1
<#
.SYNOPSIS
  Downloads the pinned Godot 4.x stable Windows build into tools\godot (git-ignored).
.EXAMPLE
  .\tools\install_godot.ps1            # installs 4.7.2
  .\tools\install_godot.ps1 -Version 4.7.3
#>
param(
    [string]$Version = "4.7.2",
    [string]$Dest = (Join-Path $PSScriptRoot "godot")
)

$ErrorActionPreference = "Stop"
$zipName = "Godot_v$Version-stable_win64.exe.zip"
$exeName = "Godot_v$Version-stable_win64.exe"
$url = "https://github.com/godotengine/godot/releases/download/$Version-stable/$zipName"
$exe = Join-Path $Dest $exeName

if (Test-Path $exe) {
    Write-Host "Godot $Version already installed: $exe"
    exit 0
}

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$zip = Join-Path $Dest $zipName
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Write-Host "Downloading $url"
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
Expand-Archive -Path $zip -DestinationPath $Dest -Force
Remove-Item $zip
if (-not (Test-Path $exe)) { throw "Archive did not contain $exeName" }
Write-Host "Installed: $exe"
& $exe --version
