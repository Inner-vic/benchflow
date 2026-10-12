<#
.SYNOPSIS
Connects one personal computer to the fixed shared bench workstation.

.DESCRIPTION
Uses one fixed SSH target and one happy path: ensure local configuration when
authorized, perform a public-key SSH handshake, then validate Runner protocol.
It does not touch the bench, LPAI, or production activation.
#>
[CmdletBinding()]
param(
    [string]$Alias = "ubuntu-workstation",
    [string]$WorkstationHost = "192.0.2.10",
    [string]$WorkstationUser = "bench-user",
    [int]$Port = 22,
    [string]$ExpectedHostFingerprint = "SHA256:EXAMPLE_VERIFY_WITH_ADMIN",
    [string]$ExpectedRunnerProtocol = "1.1",
    [string]$RunnerEntry = "~/bench-eval/bin/bench-eval",
    [string]$SshDirectory = "$HOME\.ssh",
    [string]$IdentityPath = "$HOME\.ssh\bench_eval_ed25519",
    [switch]$FirstUse,
    [switch]$AuthorizeLocalSetup,
    [switch]$RegisterKeyWithPassword,
    [switch]$PromptForPassphrase,
    [switch]$UnlockKey,
    [switch]$NoPassphrase,
    [string]$Output,
    [int]$ConnectTimeoutSeconds = 3,
    [string]$SshExecutable,
    [string]$SshKeygenExecutable,
    [string]$SshKeyscanExecutable,
    [string]$SshAddExecutable
)

$ErrorActionPreference = "Stop"
$firstUseRequested = [bool]$FirstUse
if ($FirstUse) {
    # One explicit switch authorizes the two bounded onboarding mutations. It
    # never chooses the private-key passphrase policy for the user.
    $AuthorizeLocalSetup = $true
    $RegisterKeyWithPassword = $true
    $UnlockKey = $true
}
$watch = [Diagnostics.Stopwatch]::StartNew()
$localChanges = $false
$sshAttempts = 0

function Invoke-Native([string]$Command, [string[]]$Arguments) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        [string[]]$lines = @(& $Command @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
    [pscustomobject]@{ ExitCode = $code; Text = ($lines -join "`n").Trim() }
}

function New-Issue([string]$Code, [string]$Message, [string]$Action, [string]$Scope = "pc_to_workstation") {
    [ordered]@{ code=$Code; severity="blocker"; scope=$Scope; message=$Message; action=$Action }
}

function Finish([string]$Status, [object[]]$Issues, [hashtable]$Facts, [string]$PcCheck, [string]$RunnerCheck, [int]$ExitCode) {
    $watch.Stop()
    $Facts.elapsed_ms = $watch.ElapsedMilliseconds
    $Facts.ssh_attempts = $sshAttempts
    $Facts.local_changes_this_run = $localChanges
    $result = [ordered]@{
        contract="bench-workstation-access/v1"
        schema_version=1
        checked_at=[DateTime]::UtcNow.ToString("o")
        mode="fixed_workstation_connect"
        status=$Status
        safe_to_submit=($Status -eq "READY" -and $Issues.Count -eq 0)
        mutations_performed=$false
        checks=[ordered]@{ pc_to_workstation=$PcCheck; runner_handshake=$RunnerCheck; agent_forwarding="NOT_CHECKED" }
        issues=$Issues
        facts=$Facts
    }
    $json = $result | ConvertTo-Json -Depth 10
    if ($Output) {
        $outputPath = [IO.Path]::GetFullPath($Output)
        $parent = Split-Path -Parent $outputPath
        if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        [IO.File]::WriteAllText($outputPath, "$json`n", [Text.UTF8Encoding]::new($false))
    }
    $json
    exit $ExitCode
}

function Test-HostAlias([string]$ConfigPath, [string]$ExpectedAlias) {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { return $false }
    foreach ($line in Get-Content -LiteralPath $ConfigPath) {
        $parts = (($line -split "#", 2)[0].Trim() -split "\s+")
        if ($parts.Count -gt 1 -and $parts[0].ToLowerInvariant() -eq "host" -and $parts[1..($parts.Count - 1)] -contains $ExpectedAlias) { return $true }
    }
    return $false
}

function Resolve-Identity([string]$Value) {
    $value = [Environment]::ExpandEnvironmentVariables($Value.Trim('"'))
    if ($value.StartsWith("~/") -or $value.StartsWith("~\")) { return [IO.Path]::GetFullPath((Join-Path $HOME $value.Substring(2))) }
    [IO.Path]::GetFullPath($value)
}

function Write-ManagedConfig([string]$ConfigPath, [string]$PrivateKeyPath) {
    $begin = "# BEGIN BENCH-WORKSTATION-ACCESS $Alias"
    $end = "# END BENCH-WORKSTATION-ACCESS $Alias"
    $identity = $PrivateKeyPath.Replace("\", "/")
    $block = @"
$begin
Host $Alias
    HostName $WorkstationHost
    User $WorkstationUser
    Port $Port
    IdentityFile $identity
    IdentitiesOnly yes
    UserKnownHostsFile $($knownHostsPath.Replace("\", "/"))
    ServerAliveInterval 5
    ServerAliveCountMax 1
$end
"@
    $existing = if (Test-Path -LiteralPath $ConfigPath) { Get-Content -LiteralPath $ConfigPath -Raw } else { "" }
    if ($existing) { Copy-Item -LiteralPath $ConfigPath -Destination "$ConfigPath.bak.$(Get-Date -Format yyyyMMdd-HHmmss)" }
    $pattern = "(?ms)^$([regex]::Escape($begin)).*?^$([regex]::Escape($end))\s*"
    $remaining = [regex]::Replace($existing, $pattern, "").TrimStart()
    [IO.File]::WriteAllText($ConfigPath, "$block`r`n$remaining", [Text.UTF8Encoding]::new($false))
}

function Add-PinnedHostKey([string]$Keyscan, [string]$Keygen, [string]$KnownHostsPath) {
    $scan = Invoke-Native $Keyscan @("-T", "3", "-p", "$Port", "-t", "ed25519", $WorkstationHost)
    if ($scan.ExitCode -ne 0 -or -not $scan.Text) { return New-Issue "SSH_TIMEOUT" "Could not read the fixed workstation SSH host key." "Check the corporate network or VPN, then use the network fallback only if needed." }
    $keyLines = @($scan.Text -split "`r?`n" | Where-Object { $_ -match '^\S+\s+ssh-ed25519\s+\S+' })
    if ($keyLines.Count -ne 1) { return New-Issue "SSH_HOST_KEY_INVALID" "The workstation scan did not return exactly one ED25519 host key." "Verify the SSH endpoint before trusting a host key." }
    $keyLine = $keyLines[0]
    $temp = Join-Path ([IO.Path]::GetTempPath()) "bench-workstation-key-$PID.txt"
    try {
        [IO.File]::WriteAllText($temp, "$keyLine`n", [Text.Encoding]::ASCII)
        $fingerprint = Invoke-Native $Keygen @("-lf", $temp, "-E", "sha256")
        if ($fingerprint.ExitCode -ne 0 -or $fingerprint.Text -notmatch [regex]::Escape($ExpectedHostFingerprint)) {
            return New-Issue "SSH_HOST_KEY_MISMATCH" "The fixed workstation host fingerprint does not match." "Stop and verify the fingerprint from the workstation console."
        }
        $known = if (Test-Path -LiteralPath $KnownHostsPath) { Get-Content -LiteralPath $KnownHostsPath -Raw } else { "" }
        $keyBody = ($keyLine -split "\s+")[2]
        if ($known -and $known -notmatch [regex]::Escape($keyBody)) {
            $old = Invoke-Native $Keygen @("-F", $WorkstationHost, "-f", $KnownHostsPath)
            if ($old.ExitCode -eq 0 -and $old.Text) { return New-Issue "SSH_HOST_KEY_CHANGED" "known_hosts contains another key for this workstation." "Stop and verify the fingerprint; do not automatically delete the old entry." }
        }
        if ($known -notmatch [regex]::Escape($keyBody)) { Add-Content -LiteralPath $KnownHostsPath -Value $keyLine -Encoding Ascii }
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
    return $null
}

function Read-ConnectionSettings([string]$Ssh, [string]$ConfigPath) {
    $expanded = Invoke-Native $Ssh @("-F", $ConfigPath, "-G", $Alias)
    if ($expanded.ExitCode -ne 0) { return $null }
    $values = @{}
    foreach ($line in ($expanded.Text -split "`r?`n")) {
        $parts = $line -split "\s+", 2
        if ($parts.Count -eq 2 -and -not $values.ContainsKey($parts[0])) { $values[$parts[0].ToLowerInvariant()] = $parts[1] }
    }
    $values
}

$facts = @{ alias=$Alias; workstation="$WorkstationUser@$WorkstationHost`:$Port"; runner_entry=$RunnerEntry; first_use=$firstUseRequested }
if ($Alias -notmatch '^[A-Za-z0-9._-]+$' -or $WorkstationHost -notmatch '^[A-Za-z0-9._-]+$' -or $WorkstationUser -notmatch '^[A-Za-z0-9._-]+$' -or $RunnerEntry -notmatch '^(~|/)[A-Za-z0-9._/+~-]+$' -or ($RunnerEntry -split '/') -contains '..') {
    Finish "BLOCKED" @((New-Issue "ACCESS_ARGUMENT_INVALID" "A fixed access argument is invalid." "Use reviewed Skill defaults or an absolute Runner path.")) $facts "BLOCKED" "NOT_VERIFIED" 2
}

$ssh = $SshExecutable
$keygen = $SshKeygenExecutable
$keyscan = $SshKeyscanExecutable
$sshAdd = $SshAddExecutable
function Find-OpenSsh([string]$Name) {
    $system = Join-Path $env:WINDIR "System32\OpenSSH\$Name.exe"
    if (Test-Path -LiteralPath $system -PathType Leaf) { return $system }
    return (Get-Command "$Name.exe" -ErrorAction SilentlyContinue).Source
}
if (-not $ssh) { $ssh = Find-OpenSsh "ssh" }
if (-not $keygen) { $keygen = Find-OpenSsh "ssh-keygen" }
if (-not $keyscan) { $keyscan = Find-OpenSsh "ssh-keyscan" }
if (-not $sshAdd) { $sshAdd = Find-OpenSsh "ssh-add" }
if (-not $ssh -or -not $keygen -or -not $keyscan) {
    $facts.required_authorization = "install_windows_openssh_client"
    Finish "NEEDS_USER_ACTION" @((New-Issue "OPENSSH_CLIENT_MISSING" "Windows OpenSSH Client is missing." "Authorize an administrator to install the OpenSSH.Client capability, then retry.")) $facts "BLOCKED" "NOT_VERIFIED" 3
}

$SshDirectory = [IO.Path]::GetFullPath($SshDirectory)
$IdentityPath = [IO.Path]::GetFullPath($IdentityPath)
$configPath = Join-Path $SshDirectory "config"
$knownHostsPath = Join-Path $SshDirectory "known_hosts"
$facts.ssh_config = $configPath

if (-not (Test-HostAlias $configPath $Alias)) {
    if (-not $AuthorizeLocalSetup) {
        $facts.required_authorization = "create_personal_key_and_managed_ssh_config"
        Finish "NEEDS_USER_ACTION" @((New-Issue "WORKSTATION_SSH_CONFIG_MISSING" "The fixed workstation SSH entry is missing." "Authorize this Skill to create a personal key, back up the config, and write its managed block.")) $facts "BLOCKED" "NOT_VERIFIED" 3
    }
    New-Item -ItemType Directory -Path $SshDirectory -Force | Out-Null
    if (-not (Test-Path -LiteralPath $IdentityPath -PathType Leaf)) {
        if ($NoPassphrase -and $PromptForPassphrase) {
            Finish "BLOCKED" @((New-Issue "KEY_MODE_CONFLICT" "NoPassphrase and PromptForPassphrase cannot be used together." "Choose exactly one key mode.")) $facts "BLOCKED" "NOT_VERIFIED" 2
        }
        if (-not $NoPassphrase -and -not $PromptForPassphrase) {
            $facts.required_user_action = "enter_and_confirm_key_passphrase_or_authorize_NoPassphrase"
            Finish "NEEDS_USER_ACTION" @((New-Issue "SSH_KEY_PASSPHRASE_REQUIRED" "Creating the personal key requires a passphrase choice." "Rerun with -PromptForPassphrase in the foreground, or explicitly authorize -NoPassphrase if team policy permits it.")) $facts "BLOCKED" "NOT_VERIFIED" 3
        }
        $keyArguments = @("-t", "ed25519", "-a", "64", "-f", $IdentityPath, "-C", "$env:USERNAME@$env:COMPUTERNAME-bench-workstation")
        if ($NoPassphrase) { $keyArguments += @("-N", '""') }
        $created = Invoke-Native $keygen $keyArguments
        if ($created.ExitCode -ne 0) { Finish "BLOCKED" @((New-Issue "SSH_KEYGEN_FAILED" "Could not create the personal SSH key." "Check .ssh permissions and free disk space.")) $facts "BLOCKED" "NOT_VERIFIED" 2 }
    }
    Write-ManagedConfig $configPath $IdentityPath
    $hostIssue = Add-PinnedHostKey $keyscan $keygen $knownHostsPath
    if ($hostIssue) { Finish "BLOCKED" @($hostIssue) $facts "BLOCKED" "NOT_VERIFIED" 2 }
    $localChanges = $true
}

$settings = Read-ConnectionSettings $ssh $configPath
if (-not $settings) { Finish "BLOCKED" @((New-Issue "SSH_CONFIG_INVALID" "OpenSSH cannot parse the fixed workstation entry." "Fix the syntax or restore the automatic backup.")) $facts "BLOCKED" "NOT_VERIFIED" 2 }
$activeIdentity = Resolve-Identity $settings.identityfile
$facts.identity_file = $activeIdentity
if (-not (Test-Path -LiteralPath $activeIdentity -PathType Leaf)) {
    Finish "BLOCKED" @((New-Issue "SSH_IDENTITY_MISSING" "The configured personal private key does not exist." "Authorize local setup to create a new key, or correct IdentityFile.")) $facts "BLOCKED" "NOT_VERIFIED" 2
}
$publicKeyPath = "$activeIdentity.pub"
# A protected private key that is not loaded into an agent otherwise looks
# exactly like an unregistered public key in BatchMode. Classify it locally
# before spending a network attempt or asking the user to register a duplicate.
$emptyPassphrase = Invoke-Native $keygen @("-y", "-P", '""', "-f", $activeIdentity)
if (-not (Test-Path -LiteralPath $publicKeyPath -PathType Leaf)) {
    if ($emptyPassphrase.ExitCode -eq 0 -and $emptyPassphrase.Text) {
        [IO.File]::WriteAllText($publicKeyPath, "$($emptyPassphrase.Text)`n", [Text.Encoding]::ASCII)
        $localChanges = $true
    } else {
        $facts.required_user_action = "export_public_key_with_private_key_passphrase"
        Finish "NEEDS_USER_ACTION" @((New-Issue "SSH_PUBLIC_KEY_MISSING" "The protected private key exists, but its .pub file is missing." "In the foreground, export the public key with ssh-keygen -y; never share the private key.")) $facts "BLOCKED" "NOT_VERIFIED" 3
    }
}
if ($emptyPassphrase.ExitCode -ne 0) {
    $facts.private_key_protected = $true
    if (-not $sshAdd) {
        $facts.required_user_action = "start_ssh_agent_and_unlock_private_key"
        Finish "NEEDS_USER_ACTION" @((New-Issue "SSH_AGENT_TOOL_MISSING" "The private key is protected, but ssh-add is unavailable." "Install Windows OpenSSH Client and unlock this key in the foreground.")) $facts "BLOCKED" "NOT_VERIFIED" 3
    }
    if ($UnlockKey) {
        [Console]::Error.WriteLine("Unlock the personal workstation key in the following ssh-add prompt.")
        & $sshAdd $activeIdentity
    }
    $keyFingerprint = Invoke-Native $keygen @("-lf", $publicKeyPath, "-E", "sha256")
    $agentKeys = Invoke-Native $sshAdd @("-l")
    $fingerprintToken = if ($keyFingerprint.Text) { ($keyFingerprint.Text -split "\s+")[1] } else { $null }
    if ($agentKeys.ExitCode -ne 0 -or -not $fingerprintToken -or $agentKeys.Text -notmatch [regex]::Escape($fingerprintToken)) {
        $facts.required_user_action = "start_ssh_agent_and_unlock_private_key"
        Finish "NEEDS_USER_ACTION" @((New-Issue "SSH_KEY_NOT_LOADED" "The configured private key is passphrase-protected and is not loaded in ssh-agent." "Start the personal ssh-agent, then rerun with -UnlockKey and enter the key passphrase once.")) $facts "BLOCKED" "NOT_VERIFIED" 3
    }
    $facts.private_key_loaded_in_agent = $true
} else {
    $facts.private_key_protected = $false
}

$remoteCommand = if ($RunnerEntry.EndsWith(".py")) { "python3 $RunnerEntry capabilities" } else { "$RunnerEntry capabilities" }
$sshArgs = @("-F", $configPath, "-o", "BatchMode=yes", "-o", "UpdateHostKeys=no", "-o", "ConnectTimeout=$ConnectTimeoutSeconds", "-o", "ConnectionAttempts=1", "-o", "NumberOfPasswordPrompts=0", $Alias, $remoteCommand)

# A caller using -FirstUse has already told us this computer is new and has
# explicitly authorized one password bootstrap. Register first, then perform
# the one authoritative public-key + Runner handshake. This avoids spending a
# guaranteed-to-fail public-key round trip on every new computer.
if ($firstUseRequested -and $RegisterKeyWithPassword) {
    [Console]::Error.WriteLine("One workstation password prompt follows; the script does not read, print, or store the password.")
    $keyParts = ((Get-Content -LiteralPath $publicKeyPath -Raw).Trim() -split '\s+')
    if ($keyParts.Count -lt 2 -or $keyParts[0] -notmatch '^ssh-(ed25519|rsa)$' -or $keyParts[1] -notmatch '^[A-Za-z0-9+/=]+$') {
        Finish "BLOCKED" @((New-Issue "SSH_PUBLIC_KEY_INVALID" "The local public key is invalid." "Regenerate the .pub file from the personal private key.")) $facts "BLOCKED" "NOT_VERIFIED" 2
    }
    $key = "$($keyParts[0]) $($keyParts[1])"
    $registerCommand = "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; grep -qxF -- '$key' ~/.ssh/authorized_keys || printf '%s\n' '$key' >> ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys"
    $sshAttempts++
    & $ssh -F $configPath -o BatchMode=no -o NumberOfPasswordPrompts=1 -o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no -o StrictHostKeyChecking=yes $Alias $registerCommand
    if ($LASTEXITCODE -ne 0) { Finish "BLOCKED" @((New-Issue "SSH_PUBLIC_KEY_REGISTRATION_FAILED" "One-time public-key registration failed." "Check the workstation password, or ask an onboarded teammate to register this .pub key.")) $facts "BLOCKED" "NOT_VERIFIED" 2 }
    $facts.public_key_registration = "REMOTE_PASSWORD_LOGIN_SUCCEEDED"
    $facts.onboarding_strategy = "PASSWORD_BOOTSTRAP_THEN_PUBLICKEY_VERIFY"
    $localChanges = $true
}

$sshAttempts++
$connected = Invoke-Native $ssh $sshArgs

if ($connected.ExitCode -ne 0 -and $connected.Text.ToLowerInvariant() -match "permission denied") {
    if ($firstUseRequested -and $facts.public_key_registration -eq "REMOTE_PASSWORD_LOGIN_SUCCEEDED") {
        Finish "BLOCKED" @((New-Issue "SSH_PUBLIC_KEY_VERIFY_FAILED" "The password bootstrap succeeded, but the registered public key was not accepted." "Preserve the reported public-key path and have an onboarded administrator compare the exact authorized_keys line; do not repeat password prompts or create another key.")) $facts "BLOCKED" "NOT_VERIFIED" 2
    }
    if (-not $RegisterKeyWithPassword) {
        $facts.required_authorization = "one_time_password_prompt_or_admin_public_key_registration"
        $facts.public_key_to_register = $publicKeyPath
        Finish "NEEDS_USER_ACTION" @((New-Issue "SSH_AUTH_FAILED" "Local setup is valid, but the personal public key is not registered on the fixed workstation." "Authorize one password prompt, or ask an onboarded teammate to register this .pub key.")) $facts "BLOCKED" "NOT_VERIFIED" 3
    }
    [Console]::Error.WriteLine("One workstation password prompt follows; the script does not read, print, or store the password.")
    $keyParts = ((Get-Content -LiteralPath $publicKeyPath -Raw).Trim() -split '\s+')
    if ($keyParts.Count -lt 2 -or $keyParts[0] -notmatch '^ssh-(ed25519|rsa)$' -or $keyParts[1] -notmatch '^[A-Za-z0-9+/=]+$') {
        Finish "BLOCKED" @((New-Issue "SSH_PUBLIC_KEY_INVALID" "The local public key is invalid." "Regenerate the .pub file from the personal private key.")) $facts "BLOCKED" "NOT_VERIFIED" 2
    }
    $key = "$($keyParts[0]) $($keyParts[1])"
    $registerCommand = "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; grep -qxF -- '$key' ~/.ssh/authorized_keys || printf '%s\n' '$key' >> ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys"
    $sshAttempts++
    & $ssh -F $configPath -o BatchMode=no -o NumberOfPasswordPrompts=1 -o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no -o StrictHostKeyChecking=yes $Alias $registerCommand
    if ($LASTEXITCODE -ne 0) { Finish "BLOCKED" @((New-Issue "SSH_PUBLIC_KEY_REGISTRATION_FAILED" "One-time public-key registration failed." "Check the workstation password, or ask an onboarded teammate to register the .pub key.")) $facts "BLOCKED" "NOT_VERIFIED" 2 }
    $facts.public_key_registration = "REMOTE_PASSWORD_LOGIN_SUCCEEDED"
    $facts.onboarding_strategy = "PUBLICKEY_PROBE_THEN_PASSWORD_BOOTSTRAP_THEN_VERIFY"
    $localChanges = $true
    $sshAttempts++
    $connected = Invoke-Native $ssh $sshArgs
}

if ($connected.ExitCode -ne 0) {
    $text = $connected.Text.ToLowerInvariant()
    $issue = if ($text -match "password has expired|password change required|account has expired") {
        New-Issue "WORKSTATION_ACCOUNT_PASSWORD_EXPIRED" "The configured workstation account requires a password change before SSH commands can run." "Use an interactive session to update that account password, or ask the workstation administrator to restore account access; then rerun the read-only handshake. Do not replace SSH keys or alter known_hosts."
    } elseif ($text -match "identification has changed|host key verification failed") {
        New-Issue "SSH_HOST_KEY_BLOCKED" "Fixed workstation host-key verification failed." "Stop and verify the pinned fingerprint; do not delete known_hosts to bypass it."
    } elseif ($text -match "timed out|connection refused|could not resolve") {
        New-Issue "SSH_UNREACHABLE" "The fixed workstation SSH service is unreachable." "Enter the network, VPN, or sshd fallback only now."
    } else {
        New-Issue "SSH_FAILED" "The fixed workstation SSH handshake failed." "Preserve the error and enter only the matching fallback; do not touch the bench."
    }
    Finish "BLOCKED" @($issue) $facts "BLOCKED" "NOT_VERIFIED" 2
}

try { $capabilities = $connected.Text | ConvertFrom-Json } catch {
    Finish "BLOCKED" @((New-Issue "RUNNER_INVALID_RESPONSE" "SSH succeeded, but Runner did not return valid JSON." "Check the selected Runner entry." "runner_handshake")) $facts "READY" "BLOCKED" 2
}
$facts.runner = $capabilities
$accepted = @($capabilities.accepted_runner_protocols)
if (-not $accepted -or $accepted -notcontains $ExpectedRunnerProtocol) {
    Finish "BLOCKED" @((New-Issue "RUNNER_PROTOCOL_MISMATCH" "Runner does not accept Job protocol $ExpectedRunnerProtocol." "Deploy a compatible Runner; do not switch production current or bypass the protocol check." "runner_handshake")) $facts "READY" "BLOCKED" 2
}
Finish "READY" @() $facts "READY" "READY" 0
