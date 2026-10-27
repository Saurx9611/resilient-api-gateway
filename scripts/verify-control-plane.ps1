$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$suffix = [Guid]::NewGuid().ToString('N').Substring(0, 8)
$testProject = "rag-worker-p5-test-$suffix"
$demoProject = "rag-worker-p5-demo-$suffix"
$composeFile = Join-Path $repo 'compose.workers.yaml'
$evidencePath = Join-Path $repo 'docs/verification/phase-5-control-plane.txt'
$lines = [System.Collections.Generic.List[string]]::new()
$token = 'local-development-token-do-not-use-outside-compose-0001'
$baseUri = 'http://127.0.0.1:8000'

function Record([string]$line) {
    $lines.Add($line)
    Write-Host $line
}

function Compose([string]$project, [string[]]$argsList) {
    $base = @('-p', $project, '-f', $composeFile) + $argsList
    Record ("`n> docker compose " + ($base -join ' '))
    $output = & docker compose @base 2>&1
    $code = $LASTEXITCODE
    foreach ($item in $output) { Record $item.ToString() }
    if ($code -ne 0) { throw "docker compose exited $code" }
    return $output
}

function Api([string]$method, [string]$path, $body = $null, [string]$idempotency = '') {
    $headers = @{ Authorization = "Bearer $token" }
    if ($idempotency) { $headers['Idempotency-Key'] = $idempotency }
    if ($null -eq $body) {
        return Invoke-RestMethod -Method $method -Uri "$baseUri$path" -Headers $headers -TimeoutSec 5
    }
    return Invoke-RestMethod -Method $method -Uri "$baseUri$path" -Headers $headers `
        -ContentType 'application/json' -Body ($body | ConvertTo-Json -Depth 12 -Compress) -TimeoutSec 5
}

Push-Location $repo
try {
    Record "Phase 5 control-plane verification; UTC $([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))"
    Record (docker version --format 'Client {{.Client.Version}}; Server {{.Server.Version}}')
    Record "Unique projects: $testProject, $demoProject"
    try {
        Compose $testProject @('--profile', 'test', 'run', '--build', '--rm', 'tests') | Out-Null
    }
    finally {
        Compose $testProject @('down', '--volumes') | Out-Null
    }

    try {
        Compose $demoProject @('--profile', 'control', '--profile', 'workers', 'up', '--build', '--detach') | Out-Null
        $ready = $false
        for ($attempt = 0; $attempt -lt 45; $attempt++) {
            try {
                $response = Invoke-WebRequest "$baseUri/readyz" -TimeoutSec 2
                if ($response.StatusCode -eq 200) { $ready = $true; break }
            } catch { Start-Sleep -Seconds 1 }
        }
        if (-not $ready) { throw 'API did not become ready' }
        $docs = Invoke-WebRequest "$baseUri/docs" -TimeoutSec 5
        $openapi = Invoke-RestMethod "$baseUri/openapi.json" -TimeoutSec 5
        if ($docs.StatusCode -ne 200 -or -not $openapi.paths.'/v1/jobs') { throw 'Swagger/OpenAPI smoke failed' }
        Record 'Swagger /docs and /openapi.json returned 200; /readyz is ready.'

        $workers = $null
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            $workers = Api 'GET' '/v1/workers'
            if (@($workers.items | Where-Object { $_.state -eq 'active' }).Count -ge 3) { break }
            Start-Sleep -Seconds 1
        }
        if (@($workers.items | Where-Object { $_.state -eq 'active' }).Count -lt 3) { throw 'fewer than three workers active' }
        Record 'Three independent workers are active through the API.'

        $key = "smoke-$suffix"
        $jobBody = @{ job_type = 'synthetic_report'; payload = @{ records = @(@{ category = 'smoke'; amount = 7 }) }; priority = 2 }
        $job = Api 'POST' '/v1/jobs' $jobBody $key
        $again = Api 'POST' '/v1/jobs' $jobBody $key
        if ($job.job_id -ne $again.job_id) { throw 'same idempotency key returned different job IDs' }
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            $state = Api 'GET' "/v1/jobs/$($job.job_id)"
            if ($state.state -eq 'succeeded') { break }
            Start-Sleep -Milliseconds 300
        }
        if ($state.state -ne 'succeeded') { throw "job $($job.job_id) did not succeed" }
        Record "Immediate job $($job.job_id) was idempotently submitted and reached succeeded."

        $scheduleBody = @{ job_type = 'synthetic_report'; payload = @{ records = @() }; cron_expression = '0 0 1 1 *'; timezone = 'UTC' }
        $schedule = Api 'POST' '/v1/schedules' $scheduleBody
        $scheduleId = [string]$schedule.schedule_id
        if ($scheduleId -notmatch '^[0-9a-fA-F-]{36}$') { throw 'invalid schedule UUID returned' }
        $sql = "UPDATE worker_schedules SET next_occurrence_at=clock_timestamp() WHERE schedule_id='$scheduleId'"
        Compose $demoProject @('exec', '-T', 'postgres', 'psql', '-U', 'gateway', '-d', 'gateway_dev', '-c', $sql) | Out-Null
        $scheduled = $null
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            $page = Api 'GET' '/v1/jobs?limit=100'
            foreach ($candidate in $page.items) {
                if ($candidate.id -le $job.job_id) { continue }
                $history = Api 'GET' "/v1/jobs/$($candidate.id)/attempts"
                if (@($history.items | Where-Object { $_.event_type -eq 'scheduled' }).Count -gt 0) { $scheduled = $candidate; break }
            }
            if ($scheduled) { break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $scheduled) { throw 'scheduler did not materialize due cron occurrence' }
        $scheduledState = $null
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            $scheduledState = Api 'GET' "/v1/jobs/$($scheduled.id)"
            if ($scheduledState.state -eq 'succeeded') { break }
            Start-Sleep -Milliseconds 300
        }
        if ($scheduledState.state -ne 'succeeded') { throw "scheduled job $($scheduled.id) did not succeed" }
        $scheduleState = Api 'POST' "/v1/schedules/$scheduleId/cancel" @{ reason = 'verification cleanup' }
        if ($scheduleState.enabled) { throw 'schedule cancellation did not disable future occurrences' }
        Record "Scheduler materialized due occurrence as job $($scheduled.id), which reached succeeded; API disabled schedule $scheduleId."
        $stats = Api 'GET' '/v1/queue/stats'
        Record ("Queue stats observed states: " + ($stats.states | ConvertTo-Json -Compress))
        $logs = Compose $demoProject @('--profile', 'control', '--profile', 'workers', 'logs', '--no-color', 'api', 'scheduler', 'worker-1', 'worker-2', 'worker-3')
        Record 'Captured API, scheduler and three worker process logs above.'
    }
    finally {
        Compose $demoProject @('--profile', 'control', '--profile', 'workers', 'down', '--volumes') | Out-Null
    }
    Record 'Result: Phase 5 control-plane tests and isolated process smoke passed.'
}
catch {
    Record ("FAIL: " + $_.Exception.Message)
    throw
}
finally {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $evidencePath) | Out-Null
    $lines | Set-Content -LiteralPath $evidencePath -Encoding utf8
    Pop-Location
}
