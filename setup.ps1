[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$Python = 'python',
    [string]$ResearchPython = '',
    [switch]$RepairPythonAcl,
    [switch]$LiveAgents,
    [string]$ControllerModel = ''
)
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
if ($env:CODEX_HOME -and -not [IO.Path]::IsPathRooted($env:CODEX_HOME)) { throw 'Set CODEX_HOME to an absolute path before running the PowerShell entrypoint' }
$codexRoot = if ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }
if (-not $ResearchPython) { $ResearchPython = $Python }
$harnessPython = Join-Path $codexRoot 'portable-harness-runtime\Scripts\python.exe'
function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed with exit $LASTEXITCODE; inspect before rerunning." }
}
if (-not $Apply) {
    Write-Output 'PLAN ONLY: no downloads, installs, logins, ACL or PATH changes.'
    Write-Output "CODEX_HOME: $codexRoot"
    Write-Output 'Apply: create isolated harness runtime, bootstrap baseline, install pinned research profile, verify.'
    Write-Output "Optional ACL repair: $RepairPythonAcl; optional paid native agent tests: $LiveAgents"
    exit 0
}
foreach ($command in @('git', 'npm', 'codex')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "Missing prerequisite: $command" }
}
# Check before creating directories: never install into another product's home.
Invoke-Checked $Python @('-B', (Join-Path $repo 'scripts\setup_machine.py'), '--research-python', $ResearchPython)
$scratch = Join-Path $codexRoot 'tmp'
New-Item -ItemType Directory -Path $scratch -Force | Out-Null
$previousTemporary = @{ TEMP=$env:TEMP; TMP=$env:TMP; PIP_CACHE_DIR=$env:PIP_CACHE_DIR }
try {
$env:TEMP = $scratch
$env:TMP = $scratch
$env:PIP_CACHE_DIR = Join-Path $codexRoot 'machine-setup\pip-cache'
if (-not (Test-Path -LiteralPath $harnessPython)) {
    Invoke-Checked $Python @('-m', 'venv', (Join-Path $codexRoot 'portable-harness-runtime'))
}
Invoke-Checked $harnessPython @('-m', 'pip', 'install', $repo)
Invoke-Checked $harnessPython @('-B', (Join-Path $repo 'scripts\bootstrap.py'), '--apply')
Invoke-Checked $harnessPython @('-B', (Join-Path $repo 'scripts\setup_machine.py'), '--apply', '--research-python', $ResearchPython)
if ($RepairPythonAcl) {
    # Different base versions can have different ACLs; verify both original
    # launchers rather than assuming the research repair covers the harness.
    foreach ($runtimePython in @($harnessPython, (Join-Path $codexRoot 'research-runtime\Scripts\python.exe'))) {
        & (Join-Path $repo 'scripts\repair_windows_python.ps1') -Apply -PythonPath $runtimePython
        if ($LASTEXITCODE -ne 0) { throw 'Python sandbox repair failed' }
    }
}
Invoke-Checked $harnessPython @('-B', (Join-Path $repo 'scripts\verify_installation.py'), '--sandbox', '--save')
Invoke-Checked $harnessPython @('-B', (Join-Path $repo 'scripts\functional_checks.py'))
& (Join-Path $repo 'scripts\install_maintenance.ps1') -Apply -Python $harnessPython
if ($LASTEXITCODE -ne 0) { throw 'Maintenance installation failed' }
if ($LiveAgents) {
    $liveArgs = @('-B', (Join-Path $repo 'scripts\agent_smoke.py'), '--run')
    if ($ControllerModel) { $liveArgs += @('--controller-model', $ControllerModel) }
    Invoke-Checked $harnessPython $liveArgs
}
} finally {
    foreach ($name in $previousTemporary.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousTemporary[$name], 'Process')
    }
}
. (Join-Path $codexRoot 'machine-setup\activate.ps1')
Write-Output 'Local setup and selected checks complete. Read the pending login steps in docs/MACHINE_SETUP.md.'
Write-Output "Tool paths are active in this terminal. In a new terminal: . '$codexRoot\machine-setup\activate.ps1'"
