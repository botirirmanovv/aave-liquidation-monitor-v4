# Stop monitor supervisor and all per-chain child processes.
# Children are Windows spawn helpers — their CommandLine often lacks monitor_v4.py,
# so we kill by process tree rooted at any python that launched monitor_v4.py.
#
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File tools\stop_monitor.ps1

$ErrorActionPreference = "SilentlyContinue"

function Get-PythonProcs {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'"
}

$roots = Get-PythonProcs | Where-Object {
    $_.CommandLine -match 'monitor_v4\.py'
}

if (-not $roots) {
    # Fallback: workers named aave-* via multiprocessing, or leftover spawn mains.
    $roots = Get-PythonProcs | Where-Object {
        $_.CommandLine -match 'aave_bot|freeze_support|multiprocessing\.spawn'
    }
}

if (-not $roots) {
    Write-Host "No monitor processes found."
    exit 0
}

$toStop = New-Object System.Collections.Generic.HashSet[int]
foreach ($r in $roots) {
    [void]$toStop.Add([int]$r.ProcessId)
}

# Include descendants (chain workers).
$all = @(Get-PythonProcs)
$changed = $true
while ($changed) {
    $changed = $false
    foreach ($p in $all) {
        $procId = [int]$p.ProcessId
        $ppid = [int]$p.ParentProcessId
        if ($toStop.Contains($ppid) -and -not $toStop.Contains($procId)) {
            [void]$toStop.Add($procId)
            $changed = $true
        }
    }
}

foreach ($procId in $toStop) {
    Write-Host "Stopping PID $procId..."
    Stop-Process -Id $procId -Force
}

Start-Sleep -Seconds 2
$left = Get-PythonProcs | Where-Object {
    $_.CommandLine -match 'monitor_v4\.py|aave_bot\.supervisor|aave-'
}
if ($left) {
    Write-Host "Still running:"
    $left | ForEach-Object { Write-Host "  PID $($_.ProcessId)" }
    exit 1
}

Write-Host "Monitor stopped."
exit 0
