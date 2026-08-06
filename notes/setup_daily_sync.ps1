# setup_daily_sync.ps1
# Register Windows scheduled task: daily 18:30 run _sync_tables.py (ATS -> track + job tables)
#
# Why Windows Task Scheduler instead of ZCode hook:
#   ZCode hooks are in-session event hooks (PreToolUse/SessionStart), no cron capability.
#   "Daily 18:30 unattended" requires OS-level scheduler.
#
# Usage (PowerShell, normal user is fine, /Create uses current user identity):
#   powershell -ExecutionPolicy Bypass -File notes/setup_daily_sync.ps1
#
# Verify:  schtasks /query /tn MiniwaRecruitDailySync
# Remove:  schtasks /delete /tn MiniwaRecruitDailySync /f

$ErrorActionPreference = "Stop"

# --- Config ---
$taskName = "MiniwaRecruitDailySync"
$python = "C:\Python314\python.exe"
$workDir = "F:\miniwanob"
$script = "notes\_sync_tables.py"
$runTime = "18:30"

# --- Build action ---
# cmd /c runs silently (no popup window), output redirected to log
$logFile = "$workDir\notes\_sync_log.txt"
$action = "cmd /c `"$python`" `"$workDir\$script`" > `"$logFile`" 2>&1"

# --- Register scheduled task ---
Write-Host "Registering task: $taskName (daily $runTime)" -ForegroundColor Cyan
Write-Host "  python: $python"
Write-Host "  script: $workDir\$script"
Write-Host "  log:    $logFile"

schtasks /Create /TN $taskName /TR $action /SC DAILY /ST $runTime /F

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "[OK] Registered successfully" -ForegroundColor Green
    Write-Host "  Runs daily at $runTime, syncs track table + job table"
    Write-Host "  Log: $logFile"
    Write-Host ""
    Write-Host "  View:    schtasks /query /tn $taskName /v"
    Write-Host "  Run now: schtasks /run /tn $taskName"
    Write-Host "  Remove:  schtasks /delete /tn $taskName /f"
} else {
    Write-Host ""
    Write-Host "[FAIL] Registration failed (exit $LASTEXITCODE)" -ForegroundColor Red
    Write-Host "  If permission denied, run as admin."
    Write-Host "  NOTE: this task depends on lark-cli user token auto-refresh,"
    Write-Host "        must run as current user identity (not SYSTEM)."
}
