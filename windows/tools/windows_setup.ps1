param([string]$Action='wizard',[switch]$InstallPython)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
$clientRoot=Split-Path -Parent $PSScriptRoot
$clientPython=$null
$candidates=@()
if(Get-Command py.exe -ErrorAction SilentlyContinue){
 foreach($v in @('-3.13','-3.12','-3.11')){
  $p=& py.exe $v -c 'import sys;print(sys.executable)' 2>$null
  if($LASTEXITCODE -eq 0){$candidates+=($p | Select-Object -Last 1)}
 }
}
if(Get-Command python.exe -ErrorAction SilentlyContinue){$candidates+=(Get-Command python.exe).Source}
foreach($p in $candidates){
 if($p -like '*WindowsApps*'){continue}
 & $p -c 'import sys;sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>$null
 if($LASTEXITCODE -eq 0){$clientPython=$p;break}
}
if(-not $clientPython){
 if($InstallPython -and (Get-Command winget.exe -ErrorAction SilentlyContinue)){
  & winget.exe install --id Python.Python.3.12 --exact --source winget --scope user
  Write-Host 'Installation requested. Reopen this wizard after installation.'
 }else{
  Write-Host 'Python 3.11+ is required. Run tools/windows_setup.ps1 -InstallPython to install Python for this user, or use your company software center.'
 }
 exit 1
}
& $clientPython -B (Join-Path $PSScriptRoot 'windows_client_setup.py') $Action --root $clientRoot
exit $LASTEXITCODE
