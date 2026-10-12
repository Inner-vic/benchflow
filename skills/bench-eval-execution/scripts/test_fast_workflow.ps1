<# Isolated control-plane test: no SSH, SCP, Runner, or bench mutation. #>
$ErrorActionPreference = 'Stop'
$target = Join-Path $PSScriptRoot 'run_target_workflow.ps1'
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$temp = Join-Path $tempBase ("bench-fast-workflow-" + [Guid]::NewGuid().ToString('N'))
$previousLocalAppData = $env:LOCALAPPDATA
$global:MockSshCalls = New-Object System.Collections.ArrayList
$global:MockScpCalls = New-Object System.Collections.ArrayList
$global:LASTEXITCODE = 0

function ssh {
    [void]$global:MockSshCalls.Add(@($args))
    $global:LASTEXITCODE = 0
}
function scp {
    [void]$global:MockScpCalls.Add(@($args))
    $global:LASTEXITCODE = 0
}
function powershell {
    $values = @($args)
    $index = [Array]::IndexOf($values, '-Output')
    if ($index -lt 0) { throw 'Mock access command has no output path.' }
    '{"status":"READY","safe_to_submit":true}' | Set-Content -LiteralPath $values[$index + 1] -Encoding UTF8
    $global:LASTEXITCODE = 0
}

try {
    New-Item -ItemType Directory -Path $temp | Out-Null
    $env:LOCALAPPDATA = $temp
    $jobPath = Join-Path $temp 'job.json'
    $runId = 'fast-workflow-mock-' + [Guid]::NewGuid().ToString('N')
    @{run_id=$runId; runner_protocol='1.1'; mode='target_workflow'; workflow=@{targets=@('workstation_predictions')}} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $jobPath -Encoding UTF8

    & $target -Phase Run -RunId $runId -JobPath $jobPath -RemoteProfile '/home/bench-user/profile.json' -SubmittedBy 'mock-user'
    if ($global:MockSshCalls.Count -ne 2 -or $global:MockScpCalls.Count -ne 1) {
        throw "Expected two SSH and one SCP calls, got $($global:MockSshCalls.Count) and $($global:MockScpCalls.Count)."
    }
    $remote = [string]$global:MockSshCalls[1][-1]
    if ($remote -notmatch ' prepare ' -or $remote -notmatch ' && python3 .* execute ' -or
        $remote -notmatch ' --submitted-by mock-user ') {
        throw 'Fast path did not join verified prepare and execute in order.'
    }
    if (@($global:MockScpCalls[0] | Where-Object { $_ -like '*runner_workflow.py' -or $_ -like '*job.json' -or $_ -like '*access.json' }).Count -ne 3) {
        throw 'Fast path did not stage all three reviewed inputs in one transfer.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA "bench-eval-execution\$runId\access.json"))) {
        throw 'Fast path did not create the remote expected access.json filename.'
    }
    $global:MockSshCalls.Clear()
    $global:MockScpCalls.Clear()
    $uploadRunId = 'upload-workflow-mock-' + [Guid]::NewGuid().ToString('N')
    @{run_id=$uploadRunId; runner_protocol='1.1'; mode='target_workflow'; workflow=@{
        targets=@('lpai_predictions'); sources=@{workstation_predictions='/mnt/data/bench-eval/cache/predictions/existing/predictions'}
    }} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $jobPath -Encoding UTF8
    & $target -Phase Run -RunId $uploadRunId -JobPath $jobPath -RemoteProfile '/home/bench-user/profile.json' -SubmittedBy 'mock-user'
    $uploadCommand = [string]$global:MockSshCalls[1][-1]
    $forwarded = @($global:MockSshCalls[1] | ForEach-Object { $_ }) -contains '-A'
    if ($global:MockSshCalls.Count -ne 2 -or $global:MockScpCalls.Count -ne 1 -or
        -not $forwarded -or $uploadCommand -match '--runner-cli') {
        throw 'Upload-only path must use the one production Runner with personal agent forwarding.'
    }
    'Fast workflow mock passed: two SSH calls, one SCP call, ordered prepare then execute.'
} finally {
    $env:LOCALAPPDATA = $previousLocalAppData
    $resolvedTemp = [IO.Path]::GetFullPath($temp)
    if ($resolvedTemp.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedTemp)) {
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
