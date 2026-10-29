<# Exercise the actual orchestration script with transport spies; no SSH/replay. #>
$ErrorActionPreference = 'Stop'
$temp = Join-Path ([IO.Path]::GetTempPath()) ('arrival-hook-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temp) | Out-Null
try {
    $source = Join-Path $PSScriptRoot '../skills/bench-eval-execution/scripts/run_stream_batch.ps1'
    $code = [IO.File]::ReadAllText($source)
    $code = $code.Replace('$ssh = Join-Path $env:WINDIR ''System32\OpenSSH\ssh.exe''', '$ssh = ''Test-Ssh''')
    $code = $code.Replace('$scp = Join-Path $env:WINDIR ''System32\OpenSSH\scp.exe''', '$scp = ''Test-Scp''')
    [IO.File]::WriteAllText((Join-Path $temp 'run_stream_batch.ps1'), $code)
    $arrival = @'
param([switch]$Enabled,[switch]$Execute,[switch]$CheckRuntime,[string]$TaskId,[string]$TruthRoot,[string]$AttemptId)
$global:arrivalCalls.Add(@{ Enabled=[bool]$Enabled; Execute=[bool]$Execute; CheckRuntime=[bool]$CheckRuntime; TaskId=$TaskId })
if ($Execute -and $global:arrivalFailure) { throw 'simulated arrival failure after delivery' }
'@
    [IO.File]::WriteAllText((Join-Path $temp 'run_arrival_accuracy.ps1'), $arrival)
    function Test-Path { param($LiteralPath) return $true }
    function Test-Scp { $global:LASTEXITCODE = 0 }
    function Test-Ssh {
        if (($args -join ' ') -match 'python3 ') {
            $global:workflowCalls++
            $global:LASTEXITCODE = $global:workflowFailure
        } else { $global:LASTEXITCODE = 0 }
    }
    function Reset-Spies {
        $global:arrivalCalls = [Collections.Generic.List[object]]::new()
        $global:workflowCalls = 0
        $global:workflowFailure = 0
        $global:arrivalFailure = $false
    }
    function Assert-True($condition, $message) { if (-not $condition) { throw $message } }
    $scriptPath = Join-Path $temp 'run_stream_batch.ps1'
    $common = @{ SourceRun='baseline'; RunId='new-run'; Actor='tester' }
    Reset-Spies
    & $scriptPath @common -Execute
    Assert-True ($global:workflowCalls -eq 1 -and $global:arrivalCalls.Count -eq 0) 'Default-off changed the original path.'
    Reset-Spies
    & $scriptPath @common -ArrivalAccuracy -ArrivalTruthRoot '/truth' | Out-Null
    Assert-True ($global:workflowCalls -eq 1 -and $global:arrivalCalls.Count -eq 1 -and $global:arrivalCalls[0].CheckRuntime) 'Plan started calculation.'
    Reset-Spies
    & $scriptPath @common -Execute -ArrivalAccuracy -ArrivalTruthRoot '/truth'
    Assert-True ($global:workflowCalls -eq 1 -and $global:arrivalCalls.Count -eq 2 -and $global:arrivalCalls[1].Execute -and $global:arrivalCalls[1].TaskId -eq 'new-run') 'Delivery did not trigger independent arrival stage.'
    Reset-Spies
    & $scriptPath @common -ResumeDelivery -TaskId 'existing-task' -ArrivalAccuracy -ArrivalTruthRoot '/truth'
    Assert-True ($global:arrivalCalls[1].TaskId -eq 'existing-task' -and $global:arrivalCalls[1].Execute) 'Resume used the wrong task.'
    Reset-Spies
    $global:workflowFailure = 2
    $failed = $false
    try { & $scriptPath @common -Execute -ArrivalAccuracy -ArrivalTruthRoot '/truth' } catch { $failed = $true }
    Assert-True ($failed -and $global:arrivalCalls.Count -eq 1) 'Failed delivery still triggered calculation.'
    Reset-Spies
    $global:arrivalFailure = $true
    $failed = $false
    try { & $scriptPath @common -Execute -ArrivalAccuracy -ArrivalTruthRoot '/truth' } catch { $failed = $true }
    Assert-True ($failed -and $global:workflowCalls -eq 1 -and $global:arrivalCalls.Count -eq 2) 'Arrival failure was swallowed or replay was retried.'
    Reset-Spies
    $failed = $false
    try { & $scriptPath @common -ArrivalAccuracy } catch { $failed = $true }
    Assert-True ($failed -and $global:workflowCalls -eq 0 -and $global:arrivalCalls.Count -eq 0) 'Missing truth root was not caught before submission.'
    Write-Output 'PASS: 7 arrival orchestration paths; no SSH or replay performed.'
} finally {
    # Only the explicitly created unique test directory is removed.
    $resolved = [IO.Path]::GetFullPath($temp)
    $parent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolved.StartsWith($parent) -or -not ([IO.Path]::GetFileName($resolved)).StartsWith('arrival-hook-')) { throw 'Unsafe test cleanup path.' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
