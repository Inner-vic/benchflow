param([Parameter(Mandatory=$true)][string]$ProjectRoot,[Parameter(Mandatory=$true)][string]$PythonwPath)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$hostRoot=[System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\','/')
$hostScript=Join-Path $hostRoot 'tools/windows_service_host.py'
if(-not(Test-Path -LiteralPath $hostScript -PathType Leaf)){throw 'Fixed gateway host script missing'}
if(-not(Test-Path -LiteralPath (Join-Path $hostRoot 'config/gateway.json') -PathType Leaf)){throw 'Gateway config missing'}
$rootBytes=[Text.Encoding]::UTF8.GetBytes($hostRoot.ToLowerInvariant())
$rootDigest=[Security.Cryptography.SHA256]::Create()
try{$rootHash=[BitConverter]::ToString($rootDigest.ComputeHash($rootBytes)).Replace('-','').ToLowerInvariant().Substring(0,16)}finally{$rootDigest.Dispose()}
$nativeName='BenchGateway-'+$rootHash
$pythonw=[System.IO.Path]::GetFullPath($PythonwPath)
if(-not(Test-Path -LiteralPath $pythonw -PathType Leaf)){throw 'Installed Python runtime missing'}
$existing=Get-ScheduledTask -TaskName $nativeName -ErrorAction SilentlyContinue
if($existing){
 if($existing.Actions.Execute -ne $pythonw -or $existing.Actions.Arguments -ne ('-B "'+$hostScript+'"')){throw 'Existing task differs; inspect before changing it'}
 [pscustomobject]@{task_name=$nativeName;state=[string]$existing.State;already_registered=$true} | ConvertTo-Json -Compress
 exit 0
}
$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action=New-ScheduledTaskAction -Execute $pythonw -Argument ('-B "'+$hostScript+'"') -WorkingDirectory $hostRoot
$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $identity
$periodic=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $nativeName -Action $action -Principal $principal -Settings $settings -Trigger @($trigger,$periodic) `
 -Description 'Bench evaluation parser: current-user login and periodic recovery; workstation workers remain independent.' -ErrorAction Stop | Out-Null
$clientConfig=Get-Content -LiteralPath (Join-Path $hostRoot 'config/gateway.json') -Raw | ConvertFrom-Json
if($clientConfig.deployment_role -eq 'ai-client'){
 Disable-ScheduledTask -TaskName $nativeName -ErrorAction Stop | Out-Null
}
$task=Get-ScheduledTask -TaskName $nativeName -ErrorAction Stop
[xml]$xml=Export-ScheduledTask -TaskName $nativeName
[pscustomobject]@{task_name=$nativeName;state=[string]$task.State;logon_type=[string]$task.Principal.LogonType;
 run_level=[string]$task.Principal.RunLevel;restart_count=$task.Settings.RestartCount;
 restart_interval=$task.Settings.RestartInterval;execution_time_limit=$task.Settings.ExecutionTimeLimit;
 multiple_instances=[string]$task.Settings.MultipleInstances;trigger_count=@($task.Triggers).Count;
 fixed_action_verified=($task.Actions.Execute -eq $pythonw -and $task.Actions.Arguments -eq ('-B "'+$hostScript+'"'));
 registered_xml=$xml.OuterXml} | ConvertTo-Json -Compress -Depth 8
