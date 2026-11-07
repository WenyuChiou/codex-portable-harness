[CmdletBinding()]
param([switch]$Apply, [string]$Python = 'python', [string]$NightlyAt = '05:15')
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Desktop') { $env:PSModulePath = "$env:WINDIR\System32\WindowsPowerShell\v1.0\Modules" }
$codexRoot = if ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }
$resolvedPython = (& $Python -c 'import sys; print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Python resolution failed' }
$arguments = @('-B',(Join-Path $PSScriptRoot 'install_maintenance.py'),'--python',$resolvedPython)
if ($Apply) { $arguments += '--apply' }
& $resolvedPython @arguments
if ($LASTEXITCODE -ne 0) { throw 'Maintenance deployment failed; schedules were not installed' }
if (-not $Apply) { Write-Output "PLAN: daily $NightlyAt retention/worktrees; hourly permission repair; current user, no credentials"; exit 0 }
$runner = Join-Path $codexRoot 'maintenance\runtime\run_maintenance.ps1'
@{python=$resolvedPython;nightly_at=$NightlyAt;codex_home=$codexRoot} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $codexRoot 'maintenance\schedule.json') -Encoding UTF8
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$daily = New-ScheduledTaskTrigger -Daily -At $NightlyAt
$hourly = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Hours 1)
foreach ($entry in @(@{name='CodexHarnessNightlyMaintenance';mode='Nightly';trigger=$daily},@{name='CodexHarnessPermissionRepair';mode='Permissions';trigger=$hourly})) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $runner + '" -CodexHome "' + $codexRoot + '" -Mode ' + $entry.mode + ' -Apply') -WorkingDirectory $codexRoot
    $existing = Get-ScheduledTask -TaskName $entry.name -ErrorAction SilentlyContinue
    if ($existing -and (@($existing.Actions | Where-Object { $_.Arguments -notlike ('*' + $runner + '*') }).Count -gt 0)) { throw "Unowned scheduled task collision: $($entry.name)" }
    Register-ScheduledTask -TaskName $entry.name -Action $action -Trigger $entry.trigger -Settings $settings -Principal $principal -Description 'Codex portable harness owned maintenance; see CODEX_HOME/maintenance' -Force | Out-Null
    Get-ScheduledTask -TaskName $entry.name | Select-Object TaskName,State
}
