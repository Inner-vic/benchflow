<# One local command for one Runner run containing one or many Stream scenes. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-z0-9][a-z0-9._-]{0,95}$')][string]$SourceRun,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-z0-9][a-z0-9._-]{0,95}$')][string]$RunId,
    [ValidatePattern('^[a-z0-9][a-z0-9._-]{0,95}$')][string]$TaskId,
    [string]$Scenes = 'all',
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-z0-9][a-z0-9._-]{0,95}$')][string]$Actor,
    [switch]$Execute,
    [switch]$ResumeDelivery,
    [switch]$ArrivalAccuracy,
    [string]$ArrivalTruthRoot,
    [ValidatePattern('^[a-z0-9][a-z0-9._-]{0,95}$')][string]$ArrivalAttemptId
)

$ErrorActionPreference = 'Stop'
if ($Execute -and $ResumeDelivery) { throw 'Execute and ResumeDelivery are mutually exclusive.' }
if (-not $TaskId) { $TaskId = $RunId }
if ($ArrivalAccuracy) {
    if (-not $ArrivalTruthRoot) { throw 'ArrivalTruthRoot is required with ArrivalAccuracy.' }
    & (Join-Path $PSScriptRoot 'run_arrival_accuracy.ps1') -Enabled -CheckRuntime -TruthRoot $ArrivalTruthRoot
}
if ($Scenes -notmatch '^(all|[1-9][0-9]*(,[1-9][0-9]*)*)$') {
    throw 'Scenes must be all or ascending manifest indices such as 1,2,3.'
}
$ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$scp = Join-Path $env:WINDIR 'System32\OpenSSH\scp.exe'
if (-not (Test-Path -LiteralPath $ssh) -or -not (Test-Path -LiteralPath $scp)) {
    throw 'Windows OpenSSH ssh.exe or scp.exe is missing.'
}
$remote = "/tmp/bench-eval-stream-batch-$RunId.py"
& $scp -o BatchMode=yes -o UpdateHostKeys=no `
    (Join-Path $PSScriptRoot 'run_stream_batch.py') "ubuntu-workstation:$remote"
if ($LASTEXITCODE -ne 0) { throw 'Could not stage Stream batch workflow on workstation.' }
$argsRemote = "python3 $remote --source-run $SourceRun --run-id $RunId --task-id $TaskId --scenes $Scenes --actor $Actor"
if ($Execute) { $argsRemote += ' --execute' }
if ($ResumeDelivery) { $argsRemote += ' --resume-delivery' }
$workflowExit = 1
try {
    & $ssh -o BatchMode=yes -o UpdateHostKeys=no ubuntu-workstation $argsRemote
    $workflowExit = $LASTEXITCODE
} finally {
    & $ssh -o BatchMode=yes -o UpdateHostKeys=no ubuntu-workstation "rm -f -- $remote" | Out-Null
}
if ($workflowExit -ne 0) { throw "Stream batch workflow failed for $RunId." }
if ($ArrivalAccuracy) {
    if ($Execute -or $ResumeDelivery) {
        $arrivalArgs = @{ Enabled = $true; Execute = $true; TaskId = $TaskId; TruthRoot = $ArrivalTruthRoot }
        if ($ArrivalAttemptId) { $arrivalArgs['AttemptId'] = $ArrivalAttemptId }
        & (Join-Path $PSScriptRoot 'run_arrival_accuracy.ps1') @arrivalArgs
    } else {
        Write-Output 'Arrival accuracy enabled: after successful workstation delivery, a separate XLSX evaluation will run. No replay or calculation was started in plan mode.'
    }
}
