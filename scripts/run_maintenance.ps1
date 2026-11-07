[CmdletBinding()]
param([ValidateSet('Nightly','Permissions')][string]$Mode = 'Nightly', [switch]$Apply, [string]$CodexHome = '')
$ErrorActionPreference = 'Stop'
if ($CodexHome) { $env:CODEX_HOME = [IO.Path]::GetFullPath($CodexHome) }
$codexRoot = if ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }
$runtime = Join-Path $codexRoot 'maintenance\runtime'
$settings = Get-Content -LiteralPath (Join-Path $codexRoot 'maintenance\schedule.json') -Raw | ConvertFrom-Json
$logDir = Join-Path $codexRoot 'maintenance\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ((Get-Date -Format 'yyyyMMddTHHmmss') + '-' + $Mode + '-' + [guid]::NewGuid().ToString('N') + '.log')
$exitCode = 0
$digest = [Security.Cryptography.SHA256]::Create()
try { $key = [BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($codexRoot.ToLowerInvariant()))).Replace('-','') }
finally { $digest.Dispose() }
$mutex = New-Object Threading.Mutex($false, ('Local\CodexHarnessMaintenance-' + $key))
$acquired = $false
try {
    try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { Write-Output 'Another scheduled maintenance job is active; this occurrence is skipped.'; exit 0 }
    & {
        $aclArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $runtime 'repair_maintenance_acl.ps1'))
        if ($Apply) { $aclArgs += '-Apply' }
        & powershell.exe @aclArgs
        if ($LASTEXITCODE -ne 0) { $script:exitCode = 1; Write-Output "ACL repair reported blocked targets: $LASTEXITCODE" }
        if ($Mode -eq 'Nightly') {
            # Each candidate still gets its own fail-closed filesystem/Git checks.
            $arguments = @('-B',(Join-Path $runtime 'maintenance.py'))
            if ($Apply) { $arguments += '--apply' }
            & $settings.python @arguments
            if ($LASTEXITCODE -ne 0) { $script:exitCode = 1 }
        }
    } *>&1 | Out-File -LiteralPath $log -Encoding UTF8
} catch {
    $_ | Out-File -LiteralPath $log -Append -Encoding UTF8
    $exitCode = 1
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
Write-Output "MAINTENANCE_LOG $log"
Write-Output "EXIT_CODE $exitCode"
exit $exitCode
