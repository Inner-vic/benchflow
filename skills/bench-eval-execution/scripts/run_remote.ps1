<#
.SYNOPSIS
Repeated-safe way to run an arbitrary remote command on the workstation.

.DESCRIPTION
Fixes the recurring PowerShell inline-quoting / $var / $() / nested-quote
breakage. You write the remote command into a LOCAL .sh file (UTF-8, LF), then
run this helper. It scp's the file to /tmp on the workstation and executes it
with bash. The only thing PowerShell has to parse is the trivial
"bash /tmp/<name>.sh", so nothing fragile ever reaches the ssh argument string.

USAGE
    # 1) 把远程命令写入本地 .sh（用 Write 工具，逐字写入，含引号/$/$() 都安全）
    # 2) 执行：
    powershell -ExecutionPolicy Bypass -File run_remote.ps1 -ScriptPath C:\path\job.sh
    powershell -ExecutionPolicy Bypass -File run_remote.ps1 -ScriptPath C:\path\job.sh -Alias ubuntu-workstation
.PARAMETER ScriptPath
Absolute path of the local .sh containing the remote command.
.PARAMETER Alias
Workstation ssh alias (default ubuntu-workstation).
.PARAMETER RemoteName
Optional remote filename under /tmp (default = local basename).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ScriptPath,
    [string]$Alias = "ubuntu-workstation",
    [string]$RemoteName
)
$ErrorActionPreference = "Stop"

$path = (Resolve-Path -LiteralPath $ScriptPath).Path
if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "script not found: $path" }
$name = if ($RemoteName) { $RemoteName } else { Split-Path $path -Leaf }
if ($name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$' -or $name -eq '..') {
    throw 'RemoteName must be a simple filename containing letters, digits, dot, underscore or hyphen.'
}
if ($Alias -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { throw 'Alias must be a simple SSH host alias.' }
$remote = "/tmp/bench-eval-$([Guid]::NewGuid().ToString('N'))-$name"
$ext = [IO.Path]::GetExtension($path).ToLowerInvariant()
$interp = if ($ext -eq ".py") { "python3" } else { "bash" }

& scp -o BatchMode=yes $path "${Alias}:$remote"
if ($LASTEXITCODE -ne 0) { throw "scp failed for $path" }

Write-Host "==> running remote: $interp $remote"
& ssh -o BatchMode=yes -o ConnectTimeout=8 $Alias "$interp $remote"
if ($LASTEXITCODE -ne 0) { throw "remote run failed (exit $LASTEXITCODE)" }
