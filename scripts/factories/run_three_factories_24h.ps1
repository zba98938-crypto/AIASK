param(
    [double] $Hours = 24,
    [string] $LogRoot,
    [string] $Python,
    [int] $HeartbeatInterval = 300,
    [int] $RestartDelay = 30,
    [int] $DispatchConcurrency = 5,
    [int] $DispatchDefaultUniverseLimit = 5,
    [switch] $NoEventIngest,
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

function Set-DefaultEnv {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,
        [Parameter(Mandatory = $true)]
        [string] $Value
    )

    $Current = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($Current)) {
        [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
    }
}

function Set-Env {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,
        [Parameter(Mandatory = $true)]
        [string] $Value
    )

    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Write-RunLog {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Message
    )

    $Line = "$(Get-Date -Format o) $Message"
    Write-Host $Line
    Add-Content -LiteralPath $RunLog -Value $Line -Encoding UTF8
}

function Stop-FactoryTree {
    param(
        [Parameter(Mandatory = $true)]
        [int] $ProcessId
    )

    try {
        & taskkill.exe /PID $ProcessId /T /F | Out-Null
    } catch {
        try {
            Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        } catch {
        }
    }
}

if ($Hours -le 0) {
    throw "Hours must be greater than 0."
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\.."))
$RuntimeHome = $env:AIASK_RUNTIME_HOME
if ([string]::IsNullOrWhiteSpace($RuntimeHome)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $RuntimeHome = Join-Path $env:LOCALAPPDATA "hermes-aiask"
    } else {
        $RuntimeHome = Join-Path $HOME ".aiask"
    }
}
$RuntimeHome = [System.IO.Path]::GetFullPath($RuntimeHome)
Set-DefaultEnv -Name "AIASK_RUNTIME_HOME" -Value $RuntimeHome
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeHome "data\db") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeHome "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeHome "tmp") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeHome "cache") | Out-Null

if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = $env:AIASK_FACTORY_PYTHON
}
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = $env:AIASK_VENV_PYTHON
}
if ([string]::IsNullOrWhiteSpace($Python)) {
    throw "Set -Python, AIASK_FACTORY_PYTHON, or AIASK_VENV_PYTHON to a Python >=3.12,<3.14 AIASK venv. This runner does not fall back to PATH python."
}

if ([string]::IsNullOrWhiteSpace($LogRoot)) {
    $LogRoot = Join-Path $RuntimeHome "logs\three_factories_24h"
}

$RunId = "three_factories_24h_{0}_{1}" -f (Get-Date -Format "yyyyMMdd_HHmmss_fff"), $PID
$RunDir = Join-Path $LogRoot $RunId
$ChildLogDir = Join-Path $RunDir "children"
$RunLog = Join-Path $RunDir "watchdog.log"
$StatusPath = Join-Path $RunDir "status.json"
$ExecutionRecordsPath = Join-Path $RunDir "execution_records.jsonl"
$StdoutPath = Join-Path $RunDir "supervisor.stdout.log"
$StderrPath = Join-Path $RunDir "supervisor.stderr.log"

New-Item -ItemType Directory -Force -Path $ChildLogDir | Out-Null
New-Item -ItemType File -Force -Path $RunLog | Out-Null
New-Item -ItemType File -Force -Path $ExecutionRecordsPath | Out-Null

$SqlitePath = $env:AIASK_SQLITE_PATH
if ([string]::IsNullOrWhiteSpace($SqlitePath)) {
    $SqlitePath = $env:AKSHARE_MCP_SQLITE_PATH
}
if ([string]::IsNullOrWhiteSpace($SqlitePath)) {
    $SqlitePath = Join-Path $RuntimeHome "data\db\akshare_mcp.sqlite3"
}

Set-DefaultEnv -Name "PYTHONIOENCODING" -Value "utf-8"
Set-DefaultEnv -Name "PYTHONUNBUFFERED" -Value "1"
Set-DefaultEnv -Name "AKSHARE_MCP_SQLITE_PATH" -Value $SqlitePath
Set-DefaultEnv -Name "AIASK_SQLITE_PATH" -Value $SqlitePath

Set-DefaultEnv -Name "FACTOR_MINING_FACTORY_ENABLED" -Value "1"
Set-DefaultEnv -Name "STRATEGY_FACTORY_FACTOR_CATALOG_ENABLED" -Value "1"
Set-DefaultEnv -Name "INCUBATION_FACTORY_OWNS_PAPER_TRADING" -Value "true"
Set-DefaultEnv -Name "INCUBATION_FACTORY_PAPER_INTAKE_ENABLED" -Value "1"
# 1500/轮:observe 积压上万时加速收敛(每策略孵化 ~0.05s,串行远低于 600s 上限)。
Set-DefaultEnv -Name "INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT" -Value "1500"
Set-Env -Name "INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED" -Value "0"
Set-DefaultEnv -Name "INCUBATION_FACTORY_GATE3_RECORD_ONLY_BATCH_LIMIT" -Value "300"
Set-DefaultEnv -Name "INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE" -Value "C"
Set-DefaultEnv -Name "STRATEGY_FACTORY_FACTOR_IC_GENERIC_INTAKE_ENABLED" -Value "1"
Set-DefaultEnv -Name "STRATEGY_FACTORY_EVENT_RUNTIME_MODE" -Value "refresh"

Set-Env -Name "STRATEGY_FACTORY_EXECUTION_MODE" -Value "stock_first_observe_primary"
Set-Env -Name "STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED" -Value "1"
Set-Env -Name "STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED" -Value "1"

Set-DefaultEnv -Name "STRATEGY_TRADE_PREDICTION_PROMOTION_GATE_ENABLED" -Value "0"
Set-DefaultEnv -Name "STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED" -Value "1"
Set-DefaultEnv -Name "STRATEGY_TRADE_PREDICTION_FACTOR_DECAY_ENABLED" -Value "1"
Set-Env -Name "STRATEGY_FACTORY_MIN_VALIDATION_GRADE" -Value "C"
Set-Env -Name "STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED" -Value "0"

Set-Env -Name "LIVE_TRADING_ENABLED" -Value "0"
Set-Env -Name "LIVE_TRADING_ALLOW_WRITE" -Value "0"
Set-Env -Name "BROKER_ALLOW_WRITE" -Value "0"
Set-Env -Name "LIVE_TRADING_READ_ONLY" -Value "1"
Set-Env -Name "BROKER_READ_ONLY" -Value "1"
Set-Env -Name "AIASK_FACTORY_MCP_SERVICE_EXPECTED" -Value "0"

$SupervisorArgs = @(
    "-u",
    (Join-Path $Root "scripts\factories\run_three_factories.py"),
    "--log-dir",
    $ChildLogDir,
    "--heartbeat-interval",
    [string] $HeartbeatInterval,
    "--restart-delay",
    [string] $RestartDelay,
    "--strategy-dispatch-run",
    "--strategy-parallel-full-cycles",
    "--strategy-dispatch-concurrency",
    [string] $DispatchConcurrency,
    "--strategy-dispatch-shard-size",
    "1",
    "--strategy-dispatch-default-universe",
    "--strategy-dispatch-default-universe-limit",
    [string] $DispatchDefaultUniverseLimit,
    "--strategy-execution-mode",
    "stock_first_observe_primary"
)
if ($NoEventIngest) {
    $SupervisorArgs += "--no-event-ingest"
}

$StartedAt = Get-Date
$Deadline = $StartedAt.AddSeconds([Math]::Ceiling($Hours * 3600))

$InitialStatus = [ordered]@{
    run_id = $RunId
    status = "planned"
    mcp_service_expected = $false
    mcp_service_command = $null
    root = $Root
    started_at = $StartedAt.ToString("o")
    deadline = $Deadline.ToString("o")
    duration_hours = $Hours
    watchdog_pid = $PID
    supervisor_pid = $null
    supervisor_restart_count = 0
    log_dir = $RunDir
    child_log_dir = $ChildLogDir
    execution_records = $ExecutionRecordsPath
    python = $Python
    supervisor_args = $SupervisorArgs
    live_trading_enabled = $env:LIVE_TRADING_ENABLED
    live_trading_allow_write = $env:LIVE_TRADING_ALLOW_WRITE
    broker_allow_write = $env:BROKER_ALLOW_WRITE
    gate3_record_only = $env:STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED
    gate3_min_validation_grade = $env:STRATEGY_FACTORY_MIN_VALIDATION_GRADE
    incubation_gate3_record_only_intake = $env:INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED
}
$InitialStatus | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $StatusPath -Encoding UTF8

function Write-ExecutionRecord {
    param(
        [Parameter(Mandatory = $true)]
        [string] $EventType,
        [hashtable] $Fields = @{}
    )

    $Payload = [ordered]@{
        ts = (Get-Date -Format o)
        event_type = $EventType
        run_id = $RunId
        mcp_service_expected = $false
    }
    foreach ($Key in $Fields.Keys) {
        $Payload[$Key] = $Fields[$Key]
    }
    Add-Content -LiteralPath $ExecutionRecordsPath -Value ($Payload | ConvertTo-Json -Depth 8 -Compress) -Encoding UTF8
}

Write-RunLog "planned run_id=$RunId duration_hours=$Hours deadline=$($Deadline.ToString("o"))"
Write-RunLog "log_dir=$RunDir"
Write-RunLog "python=$Python"
Write-RunLog "supervisor_args=$($SupervisorArgs -join ' ')"
Write-RunLog "safety live=$env:LIVE_TRADING_ENABLED live_write=$env:LIVE_TRADING_ALLOW_WRITE broker_write=$env:BROKER_ALLOW_WRITE gate3_record_only=$env:STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED"
Write-RunLog "mcp_service_expected=false"
Write-ExecutionRecord -EventType "watchdog_plan" -Fields @{
    root = $Root
    duration_hours = $Hours
    deadline = $Deadline.ToString("o")
    python = $Python
    supervisor_args = $SupervisorArgs
    log_dir = $RunDir
    child_log_dir = $ChildLogDir
}

if ($DryRun) {
    Write-RunLog "dry_run=true; exiting without starting supervisor"
    $InitialStatus["status"] = "dry_run"
    $InitialStatus["completed_at"] = (Get-Date).ToString("o")
    $InitialStatus | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $StatusPath -Encoding UTF8
    return
}

$RestartCount = 0
$LastExitCode = $null
$CurrentProcess = $null

try {
    while ((Get-Date) -lt $Deadline) {
        $AttemptStartedAt = Get-Date
        $CurrentProcess = Start-Process `
            -FilePath $Python `
            -ArgumentList $SupervisorArgs `
            -WorkingDirectory $Root `
            -RedirectStandardOutput $StdoutPath `
            -RedirectStandardError $StderrPath `
            -WindowStyle Hidden `
            -PassThru

        Write-RunLog "started supervisor pid=$($CurrentProcess.Id) restart_count=$RestartCount"
        Write-ExecutionRecord -EventType "supervisor_start" -Fields @{
            supervisor_pid = $CurrentProcess.Id
            supervisor_restart_count = $RestartCount
            supervisor_started_at = $AttemptStartedAt.ToString("o")
        }
        $Status = [ordered]@{
            run_id = $RunId
            status = "running"
            mcp_service_expected = $false
            mcp_service_command = $null
            root = $Root
            started_at = $StartedAt.ToString("o")
            deadline = $Deadline.ToString("o")
            duration_hours = $Hours
            watchdog_pid = $PID
            supervisor_pid = $CurrentProcess.Id
            supervisor_started_at = $AttemptStartedAt.ToString("o")
            supervisor_restart_count = $RestartCount
            last_exit_code = $LastExitCode
            log_dir = $RunDir
            child_log_dir = $ChildLogDir
            execution_records = $ExecutionRecordsPath
            stdout_log = $StdoutPath
            stderr_log = $StderrPath
        }
        $Status | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $StatusPath -Encoding UTF8

        while (-not $CurrentProcess.HasExited -and (Get-Date) -lt $Deadline) {
            Start-Sleep -Seconds 30
            $CurrentProcess.Refresh()
        }

        if (-not $CurrentProcess.HasExited) {
            Write-RunLog "deadline reached; stopping supervisor pid=$($CurrentProcess.Id)"
            Write-ExecutionRecord -EventType "deadline_reached" -Fields @{
                supervisor_pid = $CurrentProcess.Id
            }
            Stop-FactoryTree -ProcessId $CurrentProcess.Id
            $CurrentProcess.Refresh()
            $LastExitCode = $CurrentProcess.ExitCode
            break
        }

        $LastExitCode = $CurrentProcess.ExitCode
        Write-RunLog "supervisor exited pid=$($CurrentProcess.Id) exit_code=$LastExitCode before deadline"
        Write-ExecutionRecord -EventType "supervisor_exit" -Fields @{
            supervisor_pid = $CurrentProcess.Id
            exit_code = $LastExitCode
            before_deadline = $true
        }
        if ((Get-Date) -ge $Deadline) {
            break
        }
        $RestartCount += 1
        Start-Sleep -Seconds ([Math]::Max(1, $RestartDelay))
    }
} finally {
    $CompletedAt = Get-Date
    $FinalStatus = [ordered]@{
        run_id = $RunId
        status = "deadline_reached_or_stopped"
        mcp_service_expected = $false
        mcp_service_command = $null
        root = $Root
        started_at = $StartedAt.ToString("o")
        completed_at = $CompletedAt.ToString("o")
        deadline = $Deadline.ToString("o")
        elapsed_seconds = [Math]::Round(($CompletedAt - $StartedAt).TotalSeconds, 2)
        duration_hours = $Hours
        watchdog_pid = $PID
        supervisor_pid = if ($CurrentProcess -ne $null) { $CurrentProcess.Id } else { $null }
        supervisor_restart_count = $RestartCount
        last_exit_code = $LastExitCode
        log_dir = $RunDir
        child_log_dir = $ChildLogDir
        execution_records = $ExecutionRecordsPath
        stdout_log = $StdoutPath
        stderr_log = $StderrPath
    }
    $FinalStatus | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $StatusPath -Encoding UTF8
    Write-RunLog "finished status=$($FinalStatus.status) elapsed_seconds=$($FinalStatus.elapsed_seconds) restarts=$RestartCount last_exit_code=$LastExitCode"
    Write-ExecutionRecord -EventType "watchdog_finish" -Fields @{
        status = $FinalStatus.status
        elapsed_seconds = $FinalStatus.elapsed_seconds
        supervisor_restart_count = $RestartCount
        last_exit_code = $LastExitCode
    }
}
