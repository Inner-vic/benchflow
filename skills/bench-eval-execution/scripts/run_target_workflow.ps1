<# Generic Windows entry for any reviewed protocol 1.1 Job and workstation Profile. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Prepare', 'Execute', 'Status', 'Run')]
    [string]$Phase,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')]
    [string]$RunId,
    [string]$JobPath,
    [string]$RemoteProfile,
    [string]$SubmittedBy,
    [ValidateSet('codex-windows', 'claude-code-windows')]
    [string]$Client = 'codex-windows'
)

$ErrorActionPreference = 'Stop'
$remoteRoot = "/home/bench-user/bench-eval/smoke/bench-eval-execution/staging/$RunId"
$remoteScript = "$remoteRoot/runner_workflow.py"
$localRoot = Join-Path $env:LOCALAPPDATA "bench-eval-execution\$RunId"

if ($Phase -eq 'Prepare' -or $Phase -eq 'Run') {
    if (-not $JobPath -or -not $RemoteProfile) { throw "$Phase requires -JobPath and -RemoteProfile." }
    if ($Phase -eq 'Run' -and $SubmittedBy -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
        throw 'Run requires a valid -SubmittedBy.'
    }
    if ($RemoteProfile -notmatch '^/[A-Za-z0-9/._-]+$' -or $RemoteProfile.Contains('..')) {
        throw 'RemoteProfile must be a simple absolute workstation path.'
    }
    $jobFile = (Resolve-Path -LiteralPath $JobPath).Path
    $job = Get-Content -Raw -LiteralPath $jobFile | ConvertFrom-Json
    if ($job.run_id -ne $RunId -or $job.runner_protocol -ne '1.1' -or $job.mode -ne 'target_workflow') {
        throw 'Job run_id/protocol/mode does not match the requested workflow.'
    }
    if (Test-Path -LiteralPath $localRoot) { throw 'Local run evidence exists; use a new run ID.' }
    New-Item -ItemType Directory -Path $localRoot | Out-Null
    $report = Join-Path $localRoot 'workstation-access.json'
    & powershell -NoProfile -ExecutionPolicy Bypass -File `
        (Join-Path $PSScriptRoot 'access\connect_workstation_ssh.ps1') -Output $report | Out-Null
    if ($LASTEXITCODE -ne 0) {
        if (Test-Path -LiteralPath $report -PathType Leaf) {
            $blocked = Get-Content -Raw -LiteralPath $report | ConvertFrom-Json
            $issue = @($blocked.issues)[0]
            if ($issue) { throw "Fixed workstation access failed ($($issue.code)): $($issue.action)" }
        }
        throw 'Fixed workstation access failed; inspect the workstation-access.json report.'
    }
    $access = Get-Content -Raw -LiteralPath $report | ConvertFrom-Json
    if ($access.status -ne 'READY' -or $access.safe_to_submit -ne $true) { throw 'Workstation access not READY.' }
    Copy-Item -LiteralPath $jobFile -Destination (Join-Path $localRoot 'job.json')
    Copy-Item -LiteralPath $report -Destination (Join-Path $localRoot 'access.json')
    & ssh -o BatchMode=yes ubuntu-workstation "test ! -e $remoteRoot && mkdir -p $remoteRoot"
    if ($LASTEXITCODE -ne 0) { throw 'Remote staging path exists or is not writable.' }
    & scp -o BatchMode=yes `
        (Join-Path $PSScriptRoot 'runner_workflow.py') `
        (Join-Path $localRoot 'job.json') `
        (Join-Path $localRoot 'access.json') "ubuntu-workstation:$remoteRoot/"
    if ($LASTEXITCODE -ne 0) { throw 'Workflow, Job or access report staging failed.' }
    $remoteCommand = "python3 $remoteScript prepare --job $remoteRoot/job.json --profile $RemoteProfile --access-report $remoteRoot/access.json"
    $sshArgs = @('-o', 'BatchMode=yes')
    if ($Phase -eq 'Run') {
        $needsLpai = @($job.workflow.targets | Where-Object { $_ -eq 'lpai_predictions' -or $_ -eq 'evaluation_report' }).Count -gt 0
        if ($needsLpai) { $sshArgs += '-A' }
        $remoteCommand += " && python3 $remoteScript execute --run-id $RunId --submitted-by $SubmittedBy --client $Client"
    }
    $sshArgs += @('ubuntu-workstation', $remoteCommand)
    & ssh @sshArgs
} elseif ($Phase -eq 'Execute') {
    if ($SubmittedBy -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { throw 'Valid -SubmittedBy is required.' }
    $job = Get-Content -Raw -LiteralPath (Join-Path $localRoot 'job.json') | ConvertFrom-Json
    $needsLpai = @($job.workflow.targets | Where-Object { $_ -eq 'lpai_predictions' -or $_ -eq 'evaluation_report' }).Count -gt 0
    $sshArgs = @('-o', 'BatchMode=yes')
    if ($needsLpai) { $sshArgs += '-A' }
    $sshArgs += @('ubuntu-workstation', "python3 $remoteScript execute --run-id $RunId --submitted-by $SubmittedBy --client $Client")
    & ssh @sshArgs
} else {
    & ssh -o BatchMode=yes ubuntu-workstation "python3 $remoteScript status --run-id $RunId"
}
if ($LASTEXITCODE -ne 0) { throw "Workflow $Phase failed for $RunId." }
