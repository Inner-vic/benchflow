<# Optional arrival postprocessing of an existing delivered workstation task. #>
[CmdletBinding()]
param(
    [switch]$Enabled,
    [ValidatePattern('^[a-z0-9][a-z0-9._-]{0,95}$')][string]$TaskId,
    [string]$TruthRoot,
    [ValidatePattern('^[a-z0-9][a-z0-9._-]{0,95}$')][string]$AttemptId,
    [switch]$Execute,
    [switch]$CheckRuntime
)
$ErrorActionPreference = 'Stop'
$bridgeArgs = @((Join-Path $PSScriptRoot 'run_arrival_accuracy.py'))
if ($Enabled) { $bridgeArgs += '--enabled' }
if ($Execute) { $bridgeArgs += '--execute' }
if ($CheckRuntime) { $bridgeArgs += '--check-runtime' }
if ($TaskId) { $bridgeArgs += @('--task-id', $TaskId) }
if ($TruthRoot) { $bridgeArgs += @('--truth-root', $TruthRoot) }
if ($AttemptId) { $bridgeArgs += @('--attempt-id', $AttemptId) }
$bridgeArgs += @('--ssh', (Join-Path $env:WINDIR 'System32/OpenSSH/ssh.exe'))
& python @bridgeArgs
if ($LASTEXITCODE -ne 0) { throw 'Arrival accuracy stage failed; retry this stage only. Existing replay/delivery is unchanged.' }
