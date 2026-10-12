<#
.SYNOPSIS
Prepares one per-user, per-computer LPAI SSH identity for public-key registration.

.DESCRIPTION
Creates or reuses a local ED25519 key, verifies its public fingerprint, ensures
the exact key is present in the current ssh-agent, and emits the public material
needed by LPAI's SSH key management form. It never uploads or prints a private
key and never connects to LPAI or the bench workstation.
#>
[CmdletBinding()]
param(
    [string]$SshDirectory = "$HOME\.ssh",
    [string]$IdentityPath,
    [string]$CredentialName,
    [switch]$AuthorizeCreate,
    [switch]$PromptForPassphrase,
    [switch]$NoPassphrase,
    [switch]$UnlockKey,
    [switch]$CopyPublicKey,
    [string]$Output,
    [string]$SshKeygenExecutable,
    [string]$SshAddExecutable
)

$ErrorActionPreference = "Stop"
$changed = $false

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

function Finish([string]$Status, [object[]]$Issues, [hashtable]$Facts, [int]$ExitCode) {
    $Facts.local_changes_this_run = $changed
    $result = [ordered]@{
        contract = "bench-workstation-access/lpai-key-preparation/v1"
        schema_version = 1
        status = $Status
        safe_to_register = ($Status -eq "READY_TO_REGISTER" -and $Issues.Count -eq 0)
        mutations_performed = $changed
        issues = $Issues
        facts = $Facts
    }
    $json = $result | ConvertTo-Json -Depth 8
    if ($Output) {
        $outputPath = [IO.Path]::GetFullPath($Output)
        $parent = Split-Path -Parent $outputPath
        if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        [IO.File]::WriteAllText($outputPath, "$json`n", [Text.UTF8Encoding]::new($false))
    }
    $json
    exit $ExitCode
}

function New-Issue([string]$Code, [string]$Message, [string]$Action) {
    [ordered]@{ code=$Code; severity="blocker"; scope="personal_computer_to_lpai"; message=$Message; action=$Action }
}

$keygen = $SshKeygenExecutable
$sshAdd = $SshAddExecutable
if (-not $keygen) { $keygen = (Get-Command "ssh-keygen.exe" -ErrorAction SilentlyContinue).Source }
if (-not $sshAdd) { $sshAdd = (Get-Command "ssh-add.exe" -ErrorAction SilentlyContinue).Source }

$facts = @{
    portal_url = "https://compute.example/"
    portal_path = "LPAI 工作台 -> 模型开发 -> 开发空间 -> SSH 秘钥管理"
    key_type = "sshkey"
}
if (-not $keygen -or -not $sshAdd) {
    Finish "NEEDS_USER_ACTION" @((New-Issue "OPENSSH_CLIENT_MISSING" "Windows OpenSSH key tools are unavailable." "Install the Windows OpenSSH Client, then retry.")) $facts 3
}

$SshDirectory = [IO.Path]::GetFullPath($SshDirectory)
$safeUser = (($env:USERNAME -replace '[^A-Za-z0-9._-]', '-').Trim('-')).ToLowerInvariant()
$safeComputer = (($env:COMPUTERNAME -replace '[^A-Za-z0-9._-]', '-').Trim('-')).ToLowerInvariant()
if (-not $safeUser) { $safeUser = "user" }
if (-not $safeComputer) { $safeComputer = "pc" }
if (-not $CredentialName) { $CredentialName = "lpai-$safeUser-$safeComputer" }
if ($CredentialName -notmatch '^[A-Za-z0-9._-]{1,64}$') {
    Finish "BLOCKED" @((New-Issue "LPAI_CREDENTIAL_NAME_INVALID" "The generated or supplied credential name is invalid." "Use 1-64 letters, numbers, dots, underscores, or hyphens.")) $facts 2
}
if (-not $IdentityPath) { $IdentityPath = Join-Path $SshDirectory "lpai_${safeUser}_${safeComputer}_ed25519" }
$IdentityPath = [IO.Path]::GetFullPath($IdentityPath)
$publicPath = "$IdentityPath.pub"
$facts.credential_name = $CredentialName
$facts.identity_file = $IdentityPath
$facts.public_key_file = $publicPath
$identityExistedAtStart = Test-Path -LiteralPath $IdentityPath -PathType Leaf
$facts.identity_preexisting = $identityExistedAtStart

if (-not $identityExistedAtStart) {
    if (-not $AuthorizeCreate) {
        $facts.required_authorization = "create_personal_lpai_key"
        Finish "NEEDS_USER_ACTION" @((New-Issue "LPAI_KEY_MISSING" "This computer has no dedicated personal LPAI key." "Authorize creation of one per-user, per-computer ED25519 key.")) $facts 3
    }
    if ($PromptForPassphrase -and $NoPassphrase) {
        Finish "BLOCKED" @((New-Issue "KEY_MODE_CONFLICT" "PromptForPassphrase and NoPassphrase cannot be combined." "Choose one key protection mode.")) $facts 2
    }
    if (-not $PromptForPassphrase -and -not $NoPassphrase) {
        $facts.required_user_action = "choose_key_passphrase_policy"
        Finish "NEEDS_USER_ACTION" @((New-Issue "LPAI_KEY_PASSPHRASE_REQUIRED" "A key protection policy must be chosen before creation." "Prefer PromptForPassphrase; use NoPassphrase only after explicit risk acceptance for unattended automation.")) $facts 3
    }
    New-Item -ItemType Directory -Path $SshDirectory -Force | Out-Null
    $comment = "$safeUser@$safeComputer-lpai"
    $args = @("-t", "ed25519", "-a", "64", "-f", $IdentityPath, "-C", $comment)
    if ($NoPassphrase) { $args += @("-N", '""') }
    $created = Invoke-Native $keygen $args
    if ($created.ExitCode -ne 0) {
        Finish "BLOCKED" @((New-Issue "LPAI_KEYGEN_FAILED" "Could not create the personal LPAI key." "Check the .ssh directory permissions and retry in the foreground.")) $facts 2
    }
    $changed = $true
}

if (-not (Test-Path -LiteralPath $publicPath -PathType Leaf)) {
    $derived = Invoke-Native $keygen @("-y", "-f", $IdentityPath)
    if ($derived.ExitCode -ne 0 -or -not $derived.Text) {
        Finish "NEEDS_USER_ACTION" @((New-Issue "LPAI_PUBLIC_KEY_MISSING" "The private key exists but its public key cannot be derived non-interactively." "Run in the foreground, enter the private-key passphrase once, and export the .pub file.")) $facts 3
    }
    [IO.File]::WriteAllText($publicPath, "$($derived.Text)`n", [Text.Encoding]::ASCII)
    $changed = $true
}

$publicKey = (Get-Content -LiteralPath $publicPath -Raw).Trim()
if ($publicKey -notmatch '^(ssh-ed25519|ssh-rsa)\s+[A-Za-z0-9+/=]+(?:\s+.*)?$') {
    Finish "BLOCKED" @((New-Issue "LPAI_PUBLIC_KEY_INVALID" "The .pub file is not a supported one-line SSH public key." "Regenerate the public key from the matching private key; never paste a private-key PEM block.")) $facts 2
}
$fingerprintResult = Invoke-Native $keygen @("-lf", $publicPath, "-E", "sha256")
$fingerprintMatch = [regex]::Match($fingerprintResult.Text, 'SHA256:[A-Za-z0-9+/=]+')
if ($fingerprintResult.ExitCode -ne 0 -or -not $fingerprintMatch.Success) {
    Finish "BLOCKED" @((New-Issue "LPAI_KEY_FINGERPRINT_UNAVAILABLE" "Could not compute the LPAI public-key fingerprint." "Verify the .pub file and OpenSSH installation.")) $facts 2
}
$fingerprint = $fingerprintMatch.Value
$facts.identity_fingerprint = $fingerprint
$facts.public_key = $publicKey

$agentKeys = Invoke-Native $sshAdd @("-l")
if ($agentKeys.ExitCode -ne 0) {
    $agentError = $agentKeys.Text.ToLowerInvariant()
    if ($agentError.Contains("permission denied")) {
        Finish "NEEDS_USER_ACTION" @((New-Issue "SSH_AGENT_ACCESS_DENIED" "The current AI process cannot access this Windows user's ssh-agent." "Run the AI tool in the same interactive Windows user context, or restart it after the user ssh-agent service is available; do not create another key.")) $facts 3
    }
    if ($agentError.Contains("could not open a connection") -or $agentError.Contains("authentication agent")) {
        Finish "NEEDS_USER_ACTION" @((New-Issue "SSH_AGENT_NOT_RUNNING" "The current Windows user has no accessible ssh-agent." "Start this user's ssh-agent, then retry; do not create another key.")) $facts 3
    }
}
if ($agentKeys.ExitCode -ne 0 -or $agentKeys.Text -notmatch [regex]::Escape($fingerprint)) {
    $unprotected = Invoke-Native $keygen @("-y", "-P", '""', "-f", $IdentityPath)
    if ($UnlockKey -or $unprotected.ExitCode -eq 0) {
        [Console]::Error.WriteLine("Loading the dedicated personal LPAI key into the current ssh-agent.")
        & $sshAdd $IdentityPath
    }
    $agentKeys = Invoke-Native $sshAdd @("-l")
    if ($agentKeys.ExitCode -ne 0 -or $agentKeys.Text -notmatch [regex]::Escape($fingerprint)) {
        $agentError = $agentKeys.Text.ToLowerInvariant()
        if ($agentError.Contains("permission denied")) {
            Finish "NEEDS_USER_ACTION" @((New-Issue "SSH_AGENT_ACCESS_DENIED" "The current AI process cannot access this Windows user's ssh-agent." "Run the AI tool in the same interactive Windows user context, or restart it after the user ssh-agent service is available; do not create another key.")) $facts 3
        }
        if ($agentError.Contains("could not open a connection") -or $agentError.Contains("authentication agent")) {
            Finish "NEEDS_USER_ACTION" @((New-Issue "SSH_AGENT_NOT_RUNNING" "The current Windows user has no accessible ssh-agent." "Start this user's ssh-agent, then retry; do not create another key.")) $facts 3
        }
        $facts.required_user_action = "start_ssh_agent_and_unlock_lpai_key"
        Finish "NEEDS_USER_ACTION" @((New-Issue "LPAI_KEY_NOT_LOADED" "The exact LPAI key is not available in the current ssh-agent." "Start the personal ssh-agent and unlock this key once; do not copy it to the workstation.")) $facts 3
    }
}
$facts.loaded_in_agent = $true

if ($CopyPublicKey) {
    Set-Clipboard -Value $publicKey
    $facts.public_key_copied_to_clipboard = $true
} else {
    $facts.public_key_copied_to_clipboard = $false
}
if ($identityExistedAtStart) {
    $facts.next_action = "Validate the existing LPAI alias and agent-forwarding first; register this public key only if LPAI rejects the exact fingerprint."
} else {
    $facts.next_action = "Create an sshkey credential in LPAI SSH key management using credential_name and public_key; confirmation is required immediately before Add Key."
}
Finish "READY_TO_REGISTER" @() $facts 0
