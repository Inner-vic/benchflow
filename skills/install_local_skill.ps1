<#
.SYNOPSIS
Installs one reviewed local Skill archive for Codex or Claude Code.

.DESCRIPTION
Extracts an archive into a temporary directory, requires exactly one top-level
Skill folder with SKILL.md, and refuses to overwrite an existing installation.
Use DestinationRoot only for isolated tests or managed enterprise deployment.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Archive,
    [Parameter(Mandatory = $true)]
    [ValidateSet("Codex", "ClaudeCode")]
    [string]$Client,
    [ValidateSet("User", "Project")]
    [string]$Scope = "User",
    [string]$ProjectRoot,
    [string]$DestinationRoot
)

$ErrorActionPreference = "Stop"
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$archiveEntries = @(& tar -tzf $archivePath)
if ($LASTEXITCODE -ne 0 -or $archiveEntries.Count -eq 0) {
    throw "Could not list the Skill archive."
}
foreach ($entry in $archiveEntries) {
    $normalized = ([string]$entry).Replace('\', '/')
    $segments = @($normalized.Split('/', [StringSplitOptions]::RemoveEmptyEntries))
    if ($normalized.StartsWith('/') -or $normalized -match '^[A-Za-z]:' -or $segments -contains '..') {
        throw "The Skill archive contains an unsafe path: $entry"
    }
}
$verboseEntries = @(& tar -tvzf $archivePath)
if ($LASTEXITCODE -ne 0 -or @($verboseEntries | Where-Object { $_ -match '^[lh]' }).Count -gt 0) {
    throw "The Skill archive contains an unsupported link or cannot be inspected."
}
if ($Scope -eq "Project" -and $Client -ne "ClaudeCode") {
    throw "Project scope is currently supported only for Claude Code."
}

if (-not $DestinationRoot) {
    if ($Scope -eq "Project") {
        if (-not $ProjectRoot) {
            throw "ProjectRoot is required for Claude Code project scope."
        }
        $resolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path
        $DestinationRoot = Join-Path $resolvedProject ".claude\skills"
    } elseif ($Client -eq "Codex") {
        $codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
        $DestinationRoot = Join-Path $codexRoot "skills"
    } else {
        $DestinationRoot = Join-Path $env:USERPROFILE ".claude\skills"
    }
}
$DestinationRoot = [IO.Path]::GetFullPath($DestinationRoot)

$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempPath = Join-Path $tempBase "bench-skill-install-$PID-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $tempPath | Out-Null
try {
    & tar -xzf $archivePath -C $tempPath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not extract the Skill archive."
    }
    $topLevel = @(Get-ChildItem -LiteralPath $tempPath -Force)
    if ($topLevel.Count -ne 1 -or -not $topLevel[0].PSIsContainer) {
        throw "The archive must contain exactly one top-level Skill directory."
    }
    $skillSource = $topLevel[0]
    $skillName = $skillSource.Name
    if ($skillName -notmatch '^[a-z0-9-]{1,64}$') {
        throw "The Skill directory name is invalid."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $skillSource.FullName "SKILL.md") -PathType Leaf)) {
        throw "The archive does not contain SKILL.md."
    }
    $skillText = Get-Content -Raw -LiteralPath (Join-Path $skillSource.FullName "SKILL.md")
    if ($Client -eq "ClaudeCode" -and $skillText -notmatch '(?m)^disable-model-invocation:\s*true\s*$') {
        throw "This is not a Claude Code explicit-only Skill archive."
    }
    if ($Client -eq "Codex") {
        $openaiPath = Join-Path $skillSource.FullName "agents\openai.yaml"
        if ($skillText -match '(?m)^disable-model-invocation:' -or -not (Test-Path -LiteralPath $openaiPath -PathType Leaf) -or (Get-Content -Raw -LiteralPath $openaiPath) -notmatch '(?m)^\s*allow_implicit_invocation:\s*false\s*$') {
            throw "This is not a Codex explicit-only Skill archive."
        }
    }
    $reparsePoints = @(Get-ChildItem -LiteralPath $skillSource.FullName -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
    if ($reparsePoints.Count -gt 0) {
        throw "The Skill archive contains links or reparse points."
    }

    New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
    $destination = Join-Path $DestinationRoot $skillName
    if (Test-Path -LiteralPath $destination) {
        throw "Skill already exists at $destination; remove or archive it explicitly before installing a new version."
    }
    Move-Item -LiteralPath $skillSource.FullName -Destination $destination
    [ordered]@{
        status = "INSTALLED"
        client = $Client
        scope = $Scope
        skill = $skillName
        destination = $destination
        implicit_invocation_expected = $false
        restart_recommended = $true
    } | ConvertTo-Json -Depth 4
} finally {
    $resolvedTemp = [IO.Path]::GetFullPath($tempPath)
    if ($resolvedTemp.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedTemp)) {
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
