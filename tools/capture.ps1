#Requires -Version 5.1
<#
.SYNOPSIS
  Runs a capture scenario in the real app (real backend, real window, desktop GPU) and reports the result.
.DESCRIPTION
  Launches the windowed Godot console build with the scenario's resolution, waits for the in-app capture
  driver (client/tools/capture/capture_driver.gd) to write one PNG per shot plus manifest.json, and prints
  the output folder with one summary line per shot and for the frame-time statistics. Needs a desktop GPU
  and an interactive desktop session; it never runs on CI.

  Exit codes: 0 every shot written and every frame looks like a real scene, or the scenario was skipped because
  COYPU_DATA is not set (printed as "skipped: COYPU_DATA is not set"); 1 the capture failed (see the errors it
  prints); 2 it did not finish within -TimeoutS and the process tree was killed; 3 the capture finished but a
  backend process it started was still alive (it is killed here).
  A scenario that names ${COYPU_DATA} gets a copy with the variable expanded from the environment, in a temporary
  folder that is deleted afterwards. A scenario that names ${COYPU_CAPTURE_SAMPLES} gets the synthetic import samples
  (tools/write_import_samples.py) written to a temporary folder, which is set as that variable for the run and deleted
  afterwards.
.PARAMETER Scenario
  A scenario name (client/tools/capture/scenarios/<name>.json) or a path to a scenario file.
.PARAMETER Out
  Output folder. Default: captures\<yyyyMMdd-HHmmss>-<scenario>\ at the repository root (git-ignored).
.PARAMETER TimeoutS
  Wall-clock limit for the whole run, in seconds.
#>
param(
    [string]$Scenario = "kralupy_m2",
    [string]$Out = "",
    [int]$TimeoutS = 300
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$client = Join-Path $root "client"

if (Test-Path -LiteralPath $Scenario -PathType Leaf) {
    $scenarioPath = (Resolve-Path -LiteralPath $Scenario).Path
} else {
    $scenarioPath = Join-Path $client "tools\capture\scenarios\$Scenario.json"
    if (-not (Test-Path -LiteralPath $scenarioPath -PathType Leaf)) {
        throw "scenario '$Scenario' not found (looked for $scenarioPath)"
    }
}
$scenarioName = [System.IO.Path]::GetFileNameWithoutExtension($scenarioPath)
$scenarioText = [System.IO.File]::ReadAllText($scenarioPath)
# T-135 Part C: a scenario that names ${COYPU_DATA} needs the local data folder. Without it the scenario is skipped here,
# before Godot starts, so the repository never needs a real data path.
if ($scenarioText.Contains('${COYPU_DATA}') -and [string]::IsNullOrEmpty($env:COYPU_DATA)) {
    Write-Host "skipped: COYPU_DATA is not set"
    exit 0
}

$godot = Get-ChildItem (Join-Path $PSScriptRoot "godot") -Filter "Godot_v*_win64_console.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $godot) { throw "Godot not installed; run tools\install_godot.ps1 first" }

# The driver reports a malformed scenario itself (exit 1 with the errors), so a file this script cannot
# read just gets default resolution and no project instead of failing here.
$resolution = @(1600, 900)
$project = ""
# T-137 B: a scenario that marks the project's vertical profile as idealised passes --profile-idealised to the app.
$profileIdealised = $false
try {
    $parsed = [System.IO.File]::ReadAllText($scenarioPath) | ConvertFrom-Json
    if ($parsed.resolution -and @($parsed.resolution).Count -eq 2) { $resolution = @($parsed.resolution) }
    if ($parsed.project -is [string]) { $project = $parsed.project }
    if ($parsed.profile_idealised -eq $true) { $profileIdealised = $true }
} catch {
    Write-Host "note: could not read the scenario here ($($_.Exception.Message)); the app will report it"
}

if (-not $Out) {
    $Out = Join-Path $root ("captures\{0}-{1}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $scenarioName)
}
$Out = [System.IO.Path]::GetFullPath($(if ([System.IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path (Get-Location) $Out }))
New-Item -ItemType Directory -Force -Path $Out | Out-Null

function Get-BackendProcesses {
    # Matches the uv wrapper and the Python interpreter the client spawns; callers print ids only.
    Get-CimInstance Win32_Process |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match "coypu-builder-backend" }
}

function Quote([string]$value) { '"' + $value + '"' }

# The uv executable: on PATH, else the winget locations (docs/tasks/README.md, environment reminders).
function Find-Uv {
    $onPath = Get-Command uv -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $link = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe"
    if (Test-Path -LiteralPath $link) { return $link }
    $package = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages") -Filter "astral-sh.uv_*" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($package -and (Test-Path -LiteralPath (Join-Path $package.FullName "uv.exe"))) { return (Join-Path $package.FullName "uv.exe") }
    throw "uv not found; put it on PATH or install it with winget (astral-sh.uv)"
}

# Short commit of the checkout at $Path (plus "-dirty" when the tree has changes), or "unknown" when there is
# no git, no checkout or no commit. Windows PowerShell 5.1 turns a native command's stderr into a terminating
# error under $ErrorActionPreference = "Stop" once it is redirected, so relax it locally and catch.
function Get-GitCommit([string]$Path) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $commit = (& git -C $Path rev-parse --short HEAD 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $commit) { return "unknown" }
        if (& git -C $Path status --porcelain 2>$null) { return "$commit-dirty" }
        return "$commit"
    } catch {
        return "unknown"
    } finally {
        $ErrorActionPreference = $saved
    }
}

# taskkill /T /F on a process tree, silently (it complains about children that already exited).
function Stop-ProcessTree([int]$ProcessId) {
    & cmd.exe /c "taskkill /T /F /PID $ProcessId >nul 2>&1"
}

$env:COYPU_CAPTURE_COMMIT = Get-GitCommit $root
$gitCommit = $env:COYPU_CAPTURE_COMMIT

$before = @(Get-BackendProcesses | ForEach-Object { $_.ProcessId })

# The scenario Godot reads. With ${COYPU_DATA} in it, a copy with the folder written in (forward slashes, so the JSON
# strings stay valid) goes to a temporary folder outside the repository; it is deleted when the run ends.
$captureScenarioPath = $scenarioPath
$expandedDir = $null
if ($scenarioText.Contains('${COYPU_DATA}')) {
    $expandedDir = Join-Path ([System.IO.Path]::GetTempPath()) ("coypu-capture-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $expandedDir | Out-Null
    $captureScenarioPath = Join-Path $expandedDir "$scenarioName.json"
    $dataFolder = $env:COYPU_DATA.Replace('\', '/')
    [System.IO.File]::WriteAllText($captureScenarioPath, $scenarioText.Replace('${COYPU_DATA}', $dataFolder), (New-Object System.Text.UTF8Encoding $false))
}

# T-136 Part C2: a scenario that names ${COYPU_CAPTURE_SAMPLES} gets the synthetic import samples written into a temporary
# folder outside the repository. The variable is set for the Godot process only; the folder and the variable are removed in
# the finally block below. The folder is never printed.
$samplesDir = $null
if ($scenarioText.Contains('${COYPU_CAPTURE_SAMPLES}')) {
    $samplesDir = Join-Path ([System.IO.Path]::GetTempPath()) ("coypu-capture-samples-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $samplesDir | Out-Null
    try {
        $uv = Find-Uv
        & $uv run --directory (Join-Path $root "backend") python -X utf8 (Join-Path $root "tools\write_import_samples.py") $samplesDir
        if ($LASTEXITCODE -ne 0) { throw "write_import_samples.py failed (exit $LASTEXITCODE)" }
    } catch {
        Remove-Item -LiteralPath $samplesDir -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
    $env:COYPU_CAPTURE_SAMPLES = $samplesDir
}

$argList = @("--path", (Quote $client), "--resolution", ("{0}x{1}" -f $resolution[0], $resolution[1]), "--")
if ($project) {
    $projectPath = if ([System.IO.Path]::IsPathRooted($project)) { $project } else { Join-Path $root $project }
    $argList += @("--project", (Quote $projectPath))
}
if ($profileIdealised) { $argList += "--profile-idealised" }
$argList += @("--capture", (Quote $captureScenarioPath), "--capture-out", (Quote $Out))

$stdoutLog = Join-Path $Out "godot_stdout.log"
$stderrLog = Join-Path $Out "godot_stderr.log"
$started = Get-Date
Write-Host "capturing '$scenarioName' -> $Out"
$proc = $null
$timedOut = $false
$survivors = @()
try {
    $proc = Start-Process -FilePath $godot.FullName -ArgumentList ($argList -join " ") -PassThru -NoNewWindow `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    $null = $proc.Handle   # keeps the handle open so ExitCode is readable after the process ends

    $timedOut = -not $proc.WaitForExit($TimeoutS * 1000)
    if ($timedOut) { Write-Host "TIMEOUT after $TimeoutS s; killing the process tree" }
} finally {
    # Runs on a timeout, an exception and Ctrl+C alike: never leave the app or its backend behind.
    if ($proc -and -not $proc.HasExited) {
        Stop-ProcessTree $proc.Id
        $null = $proc.WaitForExit(10000)
    }
    if ($expandedDir) { Remove-Item -LiteralPath $expandedDir -Recurse -Force -ErrorAction SilentlyContinue }
    if ($samplesDir) {
        Remove-Item -LiteralPath $samplesDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item Env:COYPU_CAPTURE_SAMPLES -ErrorAction SilentlyContinue
    }

    # Backend processes that appeared during this run and are still alive: the driver quits through
    # Backend.shutdown(), so there must be none. Give the OS a moment to reap a just-killed tree first.
    Start-Sleep -Milliseconds 500
    $survivors = @(Get-BackendProcesses | Where-Object { $before -notcontains $_.ProcessId })
    if ($survivors.Count -gt 0) {
        Write-Host ("LEAK: {0} backend process(es) survived the capture: {1}" -f $survivors.Count, (($survivors | ForEach-Object { "$($_.Name)#$($_.ProcessId)" }) -join ", "))
        foreach ($survivor in $survivors) { Stop-ProcessTree $survivor.ProcessId }
    } else {
        Write-Host "no backend process survived the run"
    }
}
if (-not $timedOut) { $proc.WaitForExit() }
$exitCode = if ($timedOut) { 2 } else { $proc.ExitCode }
if ($survivors.Count -gt 0 -and $exitCode -eq 0) { $exitCode = 3 }
$elapsed = (Get-Date) - $started

$manifestPath = Join-Path $Out "manifest.json"
if (Test-Path -LiteralPath $manifestPath) {
    $manifest = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    Write-Host ("scenario {0}  godot {1}  adapter {2}  commit {3}" -f $manifest.scenario, $manifest.godot, $manifest.adapter, $manifest.git_commit)
    foreach ($shot in $manifest.shots) {
        if ($null -ne $shot.luma_mean) {
            $orbit = if ($null -ne $shot.orbit_pitch) { "  pitch {0:N2} dist {1:N0} m above={2}" -f $shot.orbit_pitch, $shot.orbit_distance_m, $shot.camera_above_focus } else { "" }
            Write-Host ("  {0,-24} {1,-8} t={2,8} s  lead={3,9} m  scene {4:P2}  luma mean {5:N3} std {6:N3}{7}" -f $shot.file, $shot.camera, $shot.time_s, $shot.lead_station_m, $shot.scene_fraction, $shot.luma_mean, $shot.luma_std, $orbit)
        } else {
            Write-Host ("  {0,-24} {1,-8} NOT SAVED" -f $shot.file, $shot.camera)
        }
    }
    if ($manifest.perf_baseline) {
        $base = $manifest.perf_baseline
        Write-Host ("  perf baseline (ui hidden): {0} frames  p50 {1:N2}  p95 {2:N2}  p99 {3:N2}  max {4:N2} ms" -f $base.frames, $base.p50_ms, $base.p95_ms, $base.p99_ms, $base.max_ms)
    }
    if ($manifest.perf) {
        $perf = $manifest.perf
        Write-Host ("  perf: {0} frames  p50 {1:N2}  p95 {2:N2}  p99 {3:N2}  max {4:N2} ms  >16.7 ms: {5}  >33.3 ms: {6}" -f $perf.frames, $perf.p50_ms, $perf.p95_ms, $perf.p99_ms, $perf.max_ms, $perf.over_16_7_ms, $perf.over_33_3_ms)
    }
    foreach ($shot in $manifest.shots) {
        if (-not $shot.memory) { continue }
        $m = $shot.memory
        $levels = @($m.tiles_per_level.PSObject.Properties | Sort-Object { [int]$_.Name } | ForEach-Object {
            $drape = $m.draped_per_level.PSObject.Properties[$_.Name]
            "L{0} {1}/{2}" -f $_.Name, $_.Value, $(if ($drape) { $drape.Value } else { 0 })
        })
        Write-Host ("  memory {0,-24} texture {1:N1} MiB  video {2:N1} MiB  tiles/draped {3}" -f $shot.file, ($m.texture_mem_bytes / 1MB), ($m.video_mem_bytes / 1MB), ($levels -join "  "))
    }
    foreach ($skip in $manifest.skipped) { Write-Host "  skipped: $skip" }
    foreach ($problem in $manifest.errors) { Write-Host "  ERROR: $problem" }
} elseif (-not $timedOut) {
    Write-Host "no manifest.json was written"
    if ($exitCode -eq 0) { $exitCode = 1 }
}

if ($exitCode -ne 0 -and (Test-Path -LiteralPath $stderrLog)) {
    Write-Host "--- tail of $stderrLog"
    Get-Content -LiteralPath $stderrLog -Tail 15 | ForEach-Object { Write-Host $_ }
}

Write-Host ("output: {0}" -f $Out)
Write-Host ("finished in {0:N1} s, exit {1}" -f $elapsed.TotalSeconds, $exitCode)
exit $exitCode
