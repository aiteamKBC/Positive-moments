$ErrorActionPreference = 'Stop'

$ProjectDir = $PSScriptRoot
$BatPath = Join-Path $ProjectDir 'run-media-worker.bat'
$TaskName = 'KBC Media Worker'

$Action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$BatPath`"" -WorkingDirectory $ProjectDir
$AtLogon = New-ScheduledTaskTrigger -AtLogOn
$EveryThirtyMinutes = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration ([TimeSpan]::MaxValue)

$Settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -WakeToRun `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger @($AtLogon, $EveryThirtyMinutes) `
  -Settings $Settings `
  -Principal $Principal `
  -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
