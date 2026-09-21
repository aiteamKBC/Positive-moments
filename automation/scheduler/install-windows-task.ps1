# ---------------------------------------------------------------------------
# Install the KBC Lecture Intelligence scheduler as a Windows Scheduled Task.
#
# This is what actually makes the scheduler run on the current local
# deployment. SCHEDULER_ENABLED=true only removes the platform's own refusal;
# the OS is what provides the timing.
#
# The trigger hours below MUST match SCHEDULER_CRON in backend/.env. They are
# passed explicitly rather than parsed out of the file, so a mismatch is
# visible here rather than silently disagreeing with the application.
# ---------------------------------------------------------------------------
param(
  # 21:00 and 23:00, matching SCHEDULER_CRON="0 21,23 * * *".
  [string[]] $AtTimes = @('21:00', '23:00'),
  [string]   $TaskName = 'KBC Lecture Scheduler'
)

$ErrorActionPreference = 'Stop'

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$BatPath = Join-Path $PSScriptRoot 'run-scheduler-cycle.bat'
if (-not (Test-Path $BatPath)) { throw "Not found: $BatPath" }

# NOTE ON TIMEZONE: Task Scheduler fires in the MACHINE's local time, while the
# platform's business day is Africa/Cairo. If this machine is not on Cairo
# time, convert the trigger times yourself - the application will still
# reconcile the correct Cairo business day, but it will do so at the wrong
# wall-clock moment relative to when transcripts become available.
Write-Host "Machine timezone: $((Get-TimeZone).Id)"
Write-Host "Platform business timezone: Africa/Cairo"

$Action = New-ScheduledTaskAction -Execute 'cmd.exe' `
  -Argument "/c `"$BatPath`"" -WorkingDirectory $ProjectDir

$Triggers = @()
foreach ($time in $AtTimes) { $Triggers += New-ScheduledTaskTrigger -Daily -At $time }

$Settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -WakeToRun `
  -MultipleInstances IgnoreNew `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 15) `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
  -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers `
  -Settings $Settings -Principal $Principal -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Triggers: $($AtTimes -join ', ') (machine local time)"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
