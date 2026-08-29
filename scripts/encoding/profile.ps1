# Kiro Terminal UTF-8 Profile  (loaded by every new PowerShell session)
# ---------------------------------------------------------------------
# This file is INTENDED to be installed into:
#   $PROFILE.CurrentUserAllHosts
#   = C:\Users\<you>\Documents\WindowsPowerShell\profile.ps1
#
# Run scripts/encoding/install.ps1 once to install. It is referenced by
# .vscode/settings.json terminal.integrated.* so Kiro's terminal sources
# it on startup even if your stand-alone PowerShell doesn't.
#
# Why we need this on Windows + zh-CN locale:
#   * chcp returns 65001 (UTF-8) but PowerShell 5.1 still sets
#     [Console]::OutputEncoding to gb2312 from the locale, so any
#     Chinese byte stream is decoded twice.
#   * PSReadLine 2.0.0 (bundled with PS 5.1) throws SetCursorPosition
#     ArgumentOutOfRangeException when the prompt contains UTF-8
#     multi-byte characters. The traceback corrupts subsequent input.
#   * Python child processes inherit the host code page unless
#     PYTHONUTF8 is set, leaving stdout/stderr in gb2312.
# ---------------------------------------------------------------------

# 1. Console code page -> UTF-8 (idempotent, silent)
try {
    if ((chcp.com 2>$null) -notmatch '65001') {
        [void] (chcp.com 65001 2>&1 | Out-Null)
    }
} catch { }

# 2. PowerShell I/O encoding -> UTF-8 (no BOM)
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8NoBom
[Console]::InputEncoding  = $utf8NoBom
$OutputEncoding           = $utf8NoBom

# 3. Soften PSReadLine to avoid SetCursorPosition crashes on long input
try {
    if (Get-Module -Name PSReadLine -ListAvailable | Select-Object -First 1) {
        Import-Module PSReadLine -ErrorAction SilentlyContinue
        if (Get-Command Set-PSReadLineOption -ErrorAction SilentlyContinue) {
            Set-PSReadLineOption -EditMode Windows -ErrorAction SilentlyContinue
            Set-PSReadLineOption -PredictionSource None -ErrorAction SilentlyContinue
            Set-PSReadLineOption -BellStyle None -ErrorAction SilentlyContinue
        }
    }
} catch { }

# 4. Python — force UTF-8 mode for every subprocess in this shell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# 5. Marker so probes can confirm the profile actually ran
$env:KIRO_TERMINAL_UTF8_READY = '1'
