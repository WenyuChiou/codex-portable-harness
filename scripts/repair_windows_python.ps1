[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$PythonPath = '',
    [string]$Codex = 'codex'
)
$ErrorActionPreference = 'Stop'
if ($env:CODEX_HOME -and -not [IO.Path]::IsPathRooted($env:CODEX_HOME)) { throw 'Set CODEX_HOME to an absolute path before running the PowerShell entrypoint' }
$codexRoot = if ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }
if (-not $PythonPath) { $PythonPath = Join-Path $codexRoot 'research-runtime\Scripts\python.exe' }
$pythonResolved = (Resolve-Path -LiteralPath $PythonPath).Path
$rootResolved = (Resolve-Path -LiteralPath $codexRoot).Path.TrimEnd('\')
if ($rootResolved -match '(^|[\\/])\.(claude|gemini|opencode)([\\/]|$)' -or
    $rootResolved -eq $env:USERPROFILE.TrimEnd('\') -or $rootResolved -eq [IO.Path]::GetPathRoot($rootResolved).TrimEnd('\')) {
    throw 'CODEX_HOME must be a dedicated Codex directory'
}
if (-not $pythonResolved.StartsWith($rootResolved + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The virtual environment must be inside the active CODEX_HOME'
}
function Invoke-Captured {
    param([string]$Executable, [string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Executable @Arguments 2>&1
        return @{ code=$LASTEXITCODE; text=($output | Out-String).Trim() }
    } finally { $ErrorActionPreference = $previousPreference }
}
$info = Invoke-Captured $pythonResolved @('-B', '-c', 'import json,sys; print(json.dumps({"prefix":sys.prefix,"base":sys.base_prefix,"executable":sys._base_executable}))')
if ($info.code -ne 0) { throw 'The virtual environment must work outside the sandbox before repairing ACLs' }
$details = $info.text | ConvertFrom-Json
if ($details.prefix -eq $details.base) { throw 'A virtual environment is required; system Python is not a substitute' }
$baseRoot = (Resolve-Path -LiteralPath $details.base).Path
$baseExe = (Resolve-Path -LiteralPath $details.executable).Path
if ((Split-Path $baseExe -Parent) -ne $baseRoot -or (Split-Path $baseExe -Leaf) -ne 'python.exe' -or
    $baseRoot -eq [IO.Path]::GetPathRoot($baseRoot) -or
    -not (Test-Path -LiteralPath (Join-Path $baseRoot 'Lib\os.py'))) {
    throw 'Refusing an unexpected Python installation root'
}
$cursor = Get-Item -LiteralPath $baseRoot
while ($null -ne $cursor) {
    if ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing a reparse-point Python installation path' }
    $cursor = $cursor.Parent
}
$sandboxArgs = @('sandbox', '-c', 'windows.sandbox="elevated"', '--permission-profile', ':workspace', '-C', $rootResolved, '--')
$probe = Invoke-Captured $Codex ($sandboxArgs + @($pythonResolved, '-B', '-c', 'import sys; print(sys.prefix)'))
if ($probe.code -eq 0) { Write-Output 'PASS: original virtual-environment launcher already works in the elevated sandbox; no ACL changes.'; exit 0 }
$systemCommand = Join-Path $env:SystemRoot 'System32\cmd.exe'
$sandboxBaseline = Invoke-Captured $Codex ($sandboxArgs + @($systemCommand, '/d', '/c', 'exit', '0'))
if ($sandboxBaseline.code -ne 0) { throw 'The sandbox cannot launch a system program; repair sandbox setup first. No Python ACL changes.' }
$baseProbe = Invoke-Captured $Codex ($sandboxArgs + @($baseExe, '--version'))
if ($baseProbe.code -eq 0 -or $baseProbe.text -notmatch 'CreateProcessAsUserW failed: 5\b') {
    throw 'Failure is not the confirmed base-Python access-denied case; no ACL changes. Inspect the sandbox diagnostic.'
}
$group = [Security.Principal.NTAccount]::new($env:COMPUTERNAME, 'CodexSandboxUsers')
$sid = $group.Translate([Security.Principal.SecurityIdentifier]).Value
Write-Output "Confirmed Windows error 5. Target: $baseRoot; group: $($group.Value); permission: inheritable ReadAndExecute only."
if (-not $Apply) { Write-Output 'PLAN: rerun with -Apply to back up ACLs and repair this exact Python installation.'; exit 2 }
$backupDir = Join-Path $codexRoot ('machine-setup\acl-backups\' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$backup = Join-Path $backupDir 'python-acl.txt'
@{ target=$baseRoot; parent=(Split-Path $baseRoot -Parent); group_sid=$sid; before_sddl=(Get-Acl -LiteralPath $baseRoot).Sddl } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupDir 'repair.json') -Encoding UTF8
& icacls.exe $baseRoot /save $backup /T /Q
if ($LASTEXITCODE -ne 0) { throw 'ACL backup incomplete; no grant performed' }
& icacls.exe $baseRoot /grant ('*' + $sid + ':(OI)(CI)(RX)')
if ($LASTEXITCODE -ne 0) { throw "ACL grant incomplete; preserve backup and inspect: $backup" }
$verified = Invoke-Captured $Codex ($sandboxArgs + @($pythonResolved, '-B', '-c', 'import sys,ssl,sqlite3; print(sys.prefix)'))
if ($verified.code -ne 0) { throw "Post-repair sandbox verification failed. Do not retry blindly. Backup: $backup" }
Write-Output "PASS: original virtual environment works. ACL backup: $backup"
Write-Output 'Restore using icacls.exe <recorded-parent> /restore <backup>; restoring also restores the old access restriction.'
exit 0
