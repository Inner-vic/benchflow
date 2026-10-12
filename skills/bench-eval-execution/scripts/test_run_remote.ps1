<# Offline transport test; mocks SSH/SCP and never contacts a workstation. #>
$ErrorActionPreference = 'Stop'
$global:RemoteCalls = New-Object System.Collections.ArrayList
$global:LASTEXITCODE = 0
function ssh { [void]$global:RemoteCalls.Add(@($args)); $global:LASTEXITCODE = 0 }
function scp { [void]$global:RemoteCalls.Add(@($args)); $global:LASTEXITCODE = 0 }
$helper = Join-Path $PSScriptRoot 'run_remote.ps1'
$localScript = Join-Path $PSScriptRoot ('remote-test-' + [Guid]::NewGuid().ToString('N') + '.sh')
try {
    'true' | Set-Content -LiteralPath $localScript -Encoding UTF8
    & $helper -ScriptPath $localScript
    & $helper -ScriptPath $localScript
    if ($global:RemoteCalls.Count -ne 4) { throw 'Expected two SCP and two SSH calls.' }
    if ($global:RemoteCalls[0][-1] -eq $global:RemoteCalls[2][-1]) { throw 'Remote staging names collided.' }
    foreach ($unsafe in @('../escape.sh', 'name with spaces.sh', 'name;echo.sh')) {
        $blocked = $false
        try { & $helper -ScriptPath $localScript -RemoteName $unsafe } catch { $blocked = $true }
        if (-not $blocked) { throw "Unsafe RemoteName accepted: $unsafe" }
    }
    if ($global:RemoteCalls.Count -ne 4) { throw 'Unsafe input reached transport.' }
    Write-Output 'Remote helper mock passed: unique staging names; unsafe filenames blocked before transport.'
} finally {
    Remove-Item -LiteralPath $localScript -Force -ErrorAction SilentlyContinue
}
