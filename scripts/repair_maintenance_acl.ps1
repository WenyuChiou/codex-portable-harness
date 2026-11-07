[CmdletBinding()]
param([switch]$Apply, [string]$Path = '', [switch]$Hook)
$ErrorActionPreference = 'Stop'
# Resolve built-in security modules independently of an embedded pwsh module path.
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    $env:PSModulePath = "$env:WINDIR\System32\WindowsPowerShell\v1.0\Modules"
}
$codexRoot = if ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$documentsCodex = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Codex'
$backup = Join-Path $codexRoot ('maintenance\acl-backups\' + [guid]::NewGuid().ToString('N') + '.jsonl')
$failures = 0
function Test-Scope([string]$Item) {
    $full = [IO.Path]::GetFullPath($Item).TrimEnd('\')
    $allowed = @($codexRoot, $documentsCodex)
    if (-not ($allowed | Where-Object { $full.StartsWith($_.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) })) {
        throw "ACL target outside Codex-owned scope: $full"
    }
    $cursor = $full
    while ($cursor -and $cursor -ne [IO.Path]::GetPathRoot($cursor)) {
        if ([IO.Directory]::Exists($cursor)) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse ACL target: $cursor" }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $full
}
function Repair-One([string]$Item) {
    $full = Test-Scope $Item
    $acl = Get-Acl -LiteralPath $full
    $owner = $acl.Owner
    $isDirectory = [IO.Directory]::Exists($full)
    $inheritance = if ($isDirectory) { 'ContainerInherit,ObjectInherit' } else { 'None' }
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', $inheritance, 'None', 'Allow')
    $hasRule = @($acl.Access | Where-Object {
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sid.Value -and
        $_.AccessControlType -eq 'Allow' -and
        ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl -and
        (-not $isDirectory -or (($_.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ContainerInherit) -and
        ($_.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ObjectInherit)))
    }).Count -gt 0
    $sandboxOwner = $owner -match '\\CodexSandbox(Offline|Online|Users|[0-9]+)$'
    if ($hasRule -and -not $sandboxOwner) { return }
    Write-Output ("ACL {0}: owner={1}; grant current user inheritable FullControl; apply={2}" -f $full,$owner,$Apply)
    if ($Apply) {
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($backup)) -Force | Out-Null
        @{path=$full;sddl=$acl.Sddl;owner=$owner} | ConvertTo-Json -Compress | Add-Content -LiteralPath $backup -Encoding UTF8
        $acl.AddAccessRule($rule)
        if ($sandboxOwner) { $acl.SetOwner($sid) }
        Set-Acl -LiteralPath $full -AclObject $acl
        $check = Get-Acl -LiteralPath $full
        if ($sandboxOwner -and $check.Owner -eq $owner) { throw "Ownership readback failed: $full" }
        Get-ChildItem -LiteralPath $full -Force -ErrorAction Stop | Select-Object -First 1 | Out-Null
    }
}
function Scan([string]$Root) {
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push((Test-Scope $Root))
    while ($pending.Count) {
        $item = $pending.Pop()
        try {
            $null = Test-Scope $item
            $leaf = [IO.Path]::GetFileName($item)
            if ($item -eq $Root -or $leaf -match '^(\.ai|\.pytest_cache|pytest-of-.+|temp|tmp)$') { Repair-One $item }
            if ($item -eq $Root) {
                $gitMarker = Join-Path $item '.git'
                if (Test-Path -LiteralPath $gitMarker) { Repair-One $gitMarker }
            }
            try { $children = @(Get-ChildItem -LiteralPath $item -Directory -Force -ErrorAction Stop) }
            catch { Repair-One $item; $children = @(Get-ChildItem -LiteralPath $item -Directory -Force -ErrorAction Stop) }
            foreach ($child in $children) {
                if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
                if ($child.Name -in @('.git','node_modules','.venv','venv')) { continue }
                $pending.Push($child.FullName)
            }
        } catch {
            $script:failures++
            Write-Output "ACL BLOCKED $item : $_"
        }
    }
}
if ($Hook) {
    try { $inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json; $Path = $inputData.cwd } catch { exit 0 }
    if (-not $Path) { exit 0 }
    try { $Path = Test-Scope $Path } catch { exit 0 }
    # Task-end repair is limited to generated test areas in this task directory.
    foreach ($name in @('.ai','.pytest_cache','temp','tmp')) {
        $candidate = Join-Path $Path $name
        if (Test-Path -LiteralPath $candidate -PathType Container) { Scan $candidate }
    }
} elseif ($Path) {
    Scan (Test-Scope $Path)
} else {
    foreach ($relative in @('worktrees','tmp')) {
        $candidate = Join-Path $codexRoot $relative
        if (Test-Path -LiteralPath $candidate -PathType Container) { Scan $candidate }
    }
}
if (Test-Path -LiteralPath $backup) { Write-Output "ACL_BACKUP $backup" }
if ($failures) { exit 1 }
exit 0
