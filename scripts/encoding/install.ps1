# Install the Kiro UTF-8 profile + persist user environment vars.
# ----------------------------------------------------------------
# Run this script ONE TIME in a normal PowerShell window:
#
#     PS <Hermes repo>\integrations\aiask> .\scripts\encoding\install.ps1
#
# It will:
#   1. Copy scripts/encoding/profile.ps1 to your $PROFILE.CurrentUserAllHosts
#      (creating the Documents\WindowsPowerShell directory if missing).
#   2. Persist user-level environment variables PYTHONUTF8 and
#      PYTHONIOENCODING so every Python subprocess inherits UTF-8,
#      even from non-PowerShell launchers (cmd, scheduled tasks, etc).
#   3. Print a verification summary.
#
# Idempotent: safe to run multiple times. If you've customised your
# existing profile, the installer appends a clearly-marked block at
# the end instead of overwriting.
# ----------------------------------------------------------------

$ErrorActionPreference = 'Stop'

$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Source = Join-Path $PSScriptRoot 'profile.ps1'
$Target = $PROFILE.CurrentUserAllHosts
$TargetDir = Split-Path $Target

Write-Host "==> Repo root        : $Repo"
Write-Host "==> Profile source   : $Source"
Write-Host "==> Profile target   : $Target"
Write-Host ""

if (-not (Test-Path $Source)) {
    throw "Source profile not found at $Source"
}

# 1. Ensure parent directory exists
if (-not (Test-Path $TargetDir)) {
    Write-Host "Creating directory $TargetDir"
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
}

$Marker = '# === Kiro UTF-8 profile (managed by scripts/encoding/install.ps1) ==='
$EndMarker = '# === end Kiro UTF-8 profile ==='
$Block = "`n$Marker`n" + (Get-Content $Source -Raw) + "`n$EndMarker`n"

if (Test-Path $Target) {
    $existing = Get-Content $Target -Raw -ErrorAction SilentlyContinue
    if ($existing -and $existing.Contains($Marker)) {
        # Replace the existing managed block in-place.
        $pattern = [regex]::Escape($Marker) + '[\s\S]*?' + [regex]::Escape($EndMarker)
        $updated = [regex]::Replace($existing, $pattern, $Block.Trim()) + "`n"
        Set-Content -Path $Target -Value $updated -Encoding UTF8 -NoNewline
        Write-Host "Updated managed UTF-8 block in existing profile."
    } else {
        # Append the managed block.
        Add-Content -Path $Target -Value $Block -Encoding UTF8
        Write-Host "Appended managed UTF-8 block to existing profile."
    }
} else {
    # Create the profile from scratch.
    $header = @"
# Auto-created by scripts/encoding/install.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
# Edit above this line to add your own customisations.

"@
    Set-Content -Path $Target -Value ($header + $Block.Trim() + "`n") -Encoding UTF8
    Write-Host "Created fresh profile with UTF-8 block."
}

# 2. Persist user-level environment variables
foreach ($pair in @(
    @{ Name = 'PYTHONUTF8';       Value = '1' }
    @{ Name = 'PYTHONIOENCODING'; Value = 'utf-8' }
)) {
    $current = [Environment]::GetEnvironmentVariable($pair.Name, 'User')
    if ($current -ne $pair.Value) {
        [Environment]::SetEnvironmentVariable($pair.Name, $pair.Value, 'User')
        Write-Host ("Set user env  {0,-18} = {1}" -f $pair.Name, $pair.Value)
    } else {
        Write-Host ("Already set   {0,-18} = {1}" -f $pair.Name, $pair.Value)
    }
    # Also set for the current session so user can verify immediately.
    Set-Item -Path "Env:$($pair.Name)" -Value $pair.Value
}

# 3. Verify in the current session by reloading the profile
Write-Host ""
Write-Host "Verifying current session..."
. $Target
$status = [ordered]@{
    'Console.OutputEncoding' = [Console]::OutputEncoding.WebName
    'Console.InputEncoding'  = [Console]::InputEncoding.WebName
    '$OutputEncoding'         = $OutputEncoding.WebName
    'PYTHONUTF8'             = $env:PYTHONUTF8
    'PYTHONIOENCODING'       = $env:PYTHONIOENCODING
    'KIRO_TERMINAL_UTF8_READY' = $env:KIRO_TERMINAL_UTF8_READY
    'Active code page'        = (chcp.com)
}
$status.GetEnumerator() | ForEach-Object {
    Write-Host ("  {0,-30} : {1}" -f $_.Key, $_.Value)
}

Write-Host ""
Write-Host "Install complete. Restart Kiro / open a fresh terminal to see the effect."
