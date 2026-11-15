param([Parameter(Mandatory=$true)][string]$ProjectRoot,[Parameter(Mandatory=$true)][string]$PythonwPath)
$ErrorActionPreference='Stop'
$bridgeRoot=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\','/')
$scriptPath=Join-Path $bridgeRoot 'desktop_frontend.py'
if(-not(Test-Path -LiteralPath $scriptPath -PathType Leaf)){throw 'Frontend program missing'}
$pythonw=[IO.Path]::GetFullPath($PythonwPath)
if(-not(Test-Path -LiteralPath $pythonw -PathType Leaf)){throw 'Installed Python runtime missing'}
$hash=[Security.Cryptography.SHA256]::Create()
try{$suffix=[BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($bridgeRoot.ToLowerInvariant()))).Replace('-','').ToLowerInvariant().Substring(0,16)}finally{$hash.Dispose()}
$nativeName='BenchDesktopBridge-'+$suffix+'-v2'
$arguments='-B "'+$scriptPath+'"'
$existing=Get-ScheduledTask -TaskName $nativeName -ErrorAction SilentlyContinue
if($existing){
 if($existing.Actions.Execute -ne $pythonw -or $existing.Actions.Arguments -ne $arguments){throw 'Existing frontend task differs; inspect before modifying'}
 [pscustomobject]@{task_name=$nativeName;already_registered=$true;state=[string]$existing.State}|ConvertTo-Json -Compress
 exit 0
}
$identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$action=New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $bridgeRoot
$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
 -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 4 -UseUnifiedSchedulingEngine
$login=New-ScheduledTaskTrigger -AtLogOn -User $identity
$periodic=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $nativeName -Action $action -Principal $principal -Settings $settings -Trigger @($login,$periodic) `
 -Description 'Feishu CC Connect desktop frontend; business state and worker remain on workstation.'|Out-Null
Disable-ScheduledTask -TaskName $nativeName|Out-Null
[pscustomobject]@{task_name=$nativeName;already_registered=$false;state='Disabled';parser_task_untouched=$true}|ConvertTo-Json -Compress
