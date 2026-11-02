<#
  windows-render.ps1 - in-container entrypoint for VI Browser rendering on
  Windows. This is the Windows counterpart of docker-entrypoint.sh (Linux): it
    prepares the stock NI LabVIEW Windows container, then runs the SAME portable
    batch runner (runner.exe, built from .github/labview/toimages/main.go) which
    shells out to lvctl.exe per VI.

  Linux drives LabVIEW over VI Server TCP under Xvfb; Windows now uses the SAME
  VI Server TCP transport (no COM/ActiveX, no Xvfb): this script enables VI
    Server TCP in LabVIEW.ini, launches LabVIEW, waits for 127.0.0.1:3363, then
    runs the runner so lvctl attaches over TCP exactly like the Linux entrypoint.

  The runner writes <blob[:2]>/<blob>.json into -OutByBlob exactly like Linux;
  the calling workflow renames those to <blob>.windows.json on publish so the
  Windows renders coexist with the Linux ones (nothing about Linux changes).

  Invoked via: docker exec <container> powershell -File C:\repo\.github\labview\toimages\windows-render.ps1 -Workspace ... -Worklist ... -OutByBlob ... -Lvctl ... -Runner ...
#>
param(
    [Parameter(Mandatory = $true)] [string] $Workspace,   # repo root inside the container (WORKSPACE)
    [Parameter(Mandatory = $true)] [string] $Worklist,    # TSV of "<blob>\t<relpath>" (WORKLIST)
    [Parameter(Mandatory = $true)] [string] $OutByBlob,   # output dir for <ab>/<blob>.json (OUT_BY_BLOB)
    [Parameter(Mandatory = $true)] [string] $Lvctl,       # path to lvctl.exe (render engine)
    [Parameter(Mandatory = $true)] [string] $Runner,      # path to runner.exe (batch driver)
    [string] $CacheDir      = 'C:\lvctl-cache',           # where lvctl extracts its embedded VIs
    [string] $RenderTimeout = '5m',                       # per-VI lvctl timeout
    [string] $LabVIEWPath   = '',                         # optional explicit LabVIEW.exe
    [int]    $BatchTimeoutSeconds = 0,                    # total runner timeout; 0 derives from worklist and RenderTimeout
    [int]    $ComReadySeconds = 600                       # how long to wait for COM-ready LabVIEW
)
$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

function Resolve-LabVIEWPath([string]$Preferred) {
    if ($Preferred -and (Test-Path $Preferred)) { return $Preferred }
    $cands = @(Get-ChildItem 'C:\Program Files\National Instruments' -Directory -Filter 'LabVIEW *' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'LabVIEW.exe' } | Where-Object { Test-Path $_ })
    if ($cands.Count -gt 0) { return $cands[0] }
    throw 'LabVIEW.exe not found under C:\Program Files\National Instruments'
}

# Ensure the install's LabVIEW.ini has the scripting / dialog-suppression tokens
# AND the VI Server TCP keys a headless LabVIEW needs so lvctl can attach over
# 127.0.0.1:3363 (the same transport the Linux container uses).
function Enable-Scripting([string]$ExePath) {
    $ini = Join-Path (Split-Path -Parent $ExePath) 'LabVIEW.ini'
    $want = @{
        'SuperSecretPrivateSpecialStuff' = 'True'; 'unattended' = 'True'
        'AllowMultipleInstances' = 'True'; 'NIERAutoSendAndSuppressAllDialogs' = 'True'
        'neverShowLicensingStartupDialog' = 'True'; 'neverShowAddonLicensingStartup' = 'True'
        'SuppressRTConnectionDialogs' = 'True'; 'DWarnDialog' = 'False'; 'AutoSaveEnabled' = 'False'
        'server.tcp.enabled' = 'True'; 'server.tcp.port' = '3363'
        'server.tcp.serviceName' = '""'
        'server.tcp.access' = '"+*"'; 'server.vi.access' = '"+*"'
    }
    $lines = @()
    if (Test-Path $ini) { $lines = @(Get-Content $ini) }
    if (-not ($lines | Where-Object { $_.Trim() -ieq '[LabVIEW]' })) { $lines += '[LabVIEW]' }
    foreach ($k in $want.Keys) {
        if ($lines | Where-Object { $_ -match "^\s*$([regex]::Escape($k))\s*=" }) {
            $lines = $lines | ForEach-Object { if ($_ -match "^\s*$([regex]::Escape($k))\s*=") { "$k=$($want[$k])" } else { $_ } }
        } else {
            $out = @(); $done = $false
            foreach ($ln in $lines) { $out += $ln; if (-not $done -and $ln.Trim() -ieq '[LabVIEW]') { $out += "$k=$($want[$k])"; $done = $true } }
            $lines = $out
        }
    }
    [System.IO.File]::WriteAllLines($ini, [string[]]$lines, [System.Text.UTF8Encoding]::new($false))
    Write-Host "  [ini] scripting tokens ensured in $ini"
}

function Kill-LabVIEW {
    Get-Process LabVIEW -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

function Convert-DurationToSeconds([string]$Value) {
    if ($Value -match '^\s*(\d+)\s*ms\s*$') { return [Math]::Max(1, [int][Math]::Ceiling([double]$matches[1] / 1000.0)) }
    if ($Value -match '^\s*(\d+)\s*s\s*$')  { return [int]$matches[1] }
    if ($Value -match '^\s*(\d+)\s*m\s*$')  { return [int]$matches[1] * 60 }
    if ($Value -match '^\s*(\d+)\s*h\s*$')  { return [int]$matches[1] * 3600 }
    # A bare integer means seconds (e.g. render_timeout: 180). Do NOT fall through
    # to [TimeSpan]::Parse, which reads a bare integer as DAYS.
    if ($Value -match '^\s*(\d+)\s*$')       { return [int]$matches[1] }
    try { return [int][Math]::Ceiling(([TimeSpan]::Parse($Value)).TotalSeconds) }
    catch { return 300 }
}

function Write-LogFile([string]$Label, [string]$Path) {
    if (Test-Path $Path) {
        Write-Host "--- $Label ---"
        Get-Content $Path -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        Write-Host "--- end $Label ---"
    }
}

# Launch LabVIEW and wait until its VI Server TCP port accepts a connection, the
# Windows counterpart of docker-entrypoint.sh's launch+wait. Returns $true once
# 127.0.0.1:$Port is listening, $false if it never comes up within $WaitSeconds.
function Start-LabVIEWVIServer([string]$ExePath, [int]$Port, [int]$WaitSeconds) {
    Write-Host "Launching LabVIEW with VI Server TCP on 127.0.0.1:$Port (wait up to $WaitSeconds s) ..."
    Start-Process -FilePath $ExePath -WindowStyle Minimized | Out-Null
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        $ok = $false
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(1000)) {
                if ($client.Connected) { $client.EndConnect($iar); $ok = $true }
            }
        } catch { }
        finally { $client.Close() }
        if ($ok) {
            Write-Host "VI Server TCP is accepting connections on 127.0.0.1:$Port."
            return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "ERROR: LabVIEW VI Server did not open 127.0.0.1:$Port within $WaitSeconds second(s)."
    Write-Host "       The server.tcp.* keys in LabVIEW.ini were likely not honored by this LabVIEW."
    return $false
}

Write-Host "=== VI Browser Windows render ==="
$lvExe = Resolve-LabVIEWPath $LabVIEWPath
Write-Host "  LabVIEW.exe : $lvExe"
Write-Host "  Workspace   : $Workspace"
Write-Host "  Worklist    : $Worklist  (exists: $(Test-Path $Worklist))"
Write-Host "  OutByBlob   : $OutByBlob"
Write-Host "  lvctl.exe   : $Lvctl  (exists: $(Test-Path $Lvctl))"
Write-Host "  runner.exe  : $Runner (exists: $(Test-Path $Runner))"

New-Item -ItemType Directory -Force -Path $OutByBlob | Out-Null
New-Item -ItemType Directory -Force -Path $CacheDir  | Out-Null
Enable-Scripting $lvExe

# Mirror docker-entrypoint.sh (Linux): bring up ONE LabVIEW with VI Server TCP
# enabled, wait for 127.0.0.1:3363, then run the batch runner. lvctl ATTACHES to
# that LabVIEW over TCP per VI (it never launches its own), exactly like Linux.
Kill-LabVIEW
if (-not (Start-LabVIEWVIServer $lvExe 3363 $ComReadySeconds)) {
    Kill-LabVIEW
    exit 1
}
$env:LABVIEW_HOST = '127.0.0.1:3363'
Write-Host "Starting Go toimages batch runner..."
$env:WORKSPACE       = $Workspace
$env:WORKLIST        = $Worklist
$env:OUT_BY_BLOB     = $OutByBlob
$env:LVCTL           = $Lvctl
$env:LVCTL_CACHE_DIR = $CacheDir
$env:RENDER_TIMEOUT  = $RenderTimeout

$worklistCount = @(Get-Content $Worklist -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -ne '' }).Count
$perRenderSeconds = Convert-DurationToSeconds $RenderTimeout
if ($BatchTimeoutSeconds -le 0) {
    $BatchTimeoutSeconds = [Math]::Max(300, ($worklistCount * $perRenderSeconds) + 180)
}
Write-Host "Runner timeout: $BatchTimeoutSeconds second(s) for $worklistCount worklist item(s)"

$runnerOut = Join-Path $env:TEMP 'lvci-toimages-runner.stdout.log'
$runnerErr = Join-Path $env:TEMP 'lvci-toimages-runner.stderr.log'
Remove-Item $runnerOut, $runnerErr -Force -ErrorAction SilentlyContinue
$runnerProcess = Start-Process -FilePath $Runner -NoNewWindow -PassThru -RedirectStandardOutput $runnerOut -RedirectStandardError $runnerErr
# Cache the OS process handle immediately. Without this, a process started with
# -PassThru reports a $null ExitCode after it exits (a long-standing PowerShell
# quirk), which would make a perfectly good render look like a failure.
$null = $runnerProcess.Handle
# WaitForExit(int) takes milliseconds in an Int32; clamp so a large timeout can
# never overflow Int32 (which would throw before the render is ever awaited).
$waitMs = [int][Math]::Min([double]$BatchTimeoutSeconds * 1000.0, [double][int]::MaxValue)
if (-not $runnerProcess.WaitForExit($waitMs)) {
    Write-Host "Go toimages batch runner timed out after $BatchTimeoutSeconds second(s)"
    Stop-Process -Id $runnerProcess.Id -Force -ErrorAction SilentlyContinue
    Kill-LabVIEW
    Write-LogFile 'runner stdout' $runnerOut
    Write-LogFile 'runner stderr' $runnerErr
    exit 124
}
# Block until the process is fully reaped so ExitCode is committed before we read it.
$runnerProcess.WaitForExit()
$runnerProcess.Refresh()
$runnerExit = $runnerProcess.ExitCode
Write-LogFile 'runner stdout' $runnerOut
Write-LogFile 'runner stderr' $runnerErr

# Best-effort: leave LabVIEW closed so the container can stop cleanly.
Kill-LabVIEW

$produced = @(Get-ChildItem -Path $OutByBlob -Recurse -Filter '*.json' -ErrorAction SilentlyContinue).Count
Write-Host "=== done: $produced frame JSON file(s) under $OutByBlob ==="

if ($null -eq $runnerExit) {
    # Even with the handle cached this should not happen, but never discard a
    # good render over an unreadable exit code: fall back to the render's real
    # success signal -- whether the expected frame JSON files were produced.
    if ($produced -gt 0) {
        Write-Host "Runner did not report an exit code, but $produced frame JSON file(s) were produced; treating as success."
        $runnerExit = 0
    } else {
        Write-Host 'Go toimages batch runner exited without an exit code and produced no output; treating as failed.'
        $runnerExit = 1
    }
}
elseif (($runnerExit -eq 0) -and ($worklistCount -gt 0) -and ($produced -eq 0)) {
    Write-Host 'Non-empty worklist produced zero frame JSON files; treating as failed.'
    $runnerExit = 1
}
Write-Host "Runner exit code: $runnerExit"
# Mirror the runner's contract: exit non-zero only if the runner itself failed.
exit $runnerExit
