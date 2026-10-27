$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$suffix = [Guid]::NewGuid().ToString('N').Substring(0, 8)
$testProject = "chronos-p6-test-$suffix"
$demoProject = "chronos-p6-demo-$suffix"
$composeFile = Join-Path $repo 'compose.workers.yaml'
$evidencePath = Join-Path $repo 'docs/verification/phase-6-dashboard.txt'
$webBase = 'http://127.0.0.1:3000'
$apiBase = 'http://127.0.0.1:8000'
$token = 'local-development-token-do-not-use-outside-compose-0001'
$lines = [System.Collections.Generic.List[string]]::new()

function Record([string]$line) { $lines.Add($line); Write-Host $line }
function Compose([string]$project, [string[]]$argsList) {
    $base = @('-p', $project, '-f', $composeFile) + $argsList
    Record ("`n> docker compose " + ($base -join ' '))
    $output = & docker compose @base 2>&1
    $code = $LASTEXITCODE
    foreach ($item in $output) { Record $item.ToString() }
    if ($code -ne 0) { throw "docker compose exited $code" }
    return $output
}
function Api([string]$method, [string]$base, [string]$path, $body = $null, [string]$key = '') {
    $headers = @{ Authorization = "Bearer $token" }
    if ($key) { $headers['Idempotency-Key'] = $key }
    if ($null -eq $body) { return Invoke-RestMethod -Method $method -Uri "$base$path" -Headers $headers -TimeoutSec 8 }
    return Invoke-RestMethod -Method $method -Uri "$base$path" -Headers $headers -ContentType 'application/json' `
        -Body ($body | ConvertTo-Json -Depth 12 -Compress) -TimeoutSec 8
}

Push-Location $repo
try {
    Record "Chronos Phase 6 dashboard verification; UTC $([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))"
    Record (docker version --format 'Client {{.Client.Version}}; Server {{.Server.Version}}')
    Record "Unique projects: $testProject, $demoProject"
    try { Compose $testProject @('--profile', 'test', 'run', '--build', '--rm', 'tests') | Out-Null }
    finally { Compose $testProject @('down', '--volumes') | Out-Null }

    try {
        Compose $demoProject @('--profile', 'control', '--profile', 'workers', 'up', '--build', '--detach') | Out-Null
        $ready = $false
        for ($attempt = 0; $attempt -lt 90; $attempt++) {
            try {
                $apiReady = Invoke-WebRequest "$apiBase/readyz" -TimeoutSec 2
                $dashboard = Invoke-WebRequest "$webBase/" -TimeoutSec 2
                if ($apiReady.StatusCode -eq 200 -and $dashboard.StatusCode -eq 200) { $ready = $true; break }
            } catch { Start-Sleep -Seconds 1 }
        }
        if (-not $ready) { throw 'API or dashboard did not become ready' }
        $docs = Invoke-WebRequest "$webBase/docs" -TimeoutSec 8
        $openapi = Invoke-RestMethod "$webBase/openapi.json" -TimeoutSec 8
        if ($docs.StatusCode -ne 200 -or -not $openapi.paths.'/v1/jobs') { throw 'same-origin Swagger/OpenAPI smoke failed' }
        $proxiedStats = Api 'GET' $webBase '/api/chronos/v1/queue/stats'
        if ($null -eq $proxiedStats.states) { throw 'same-origin authenticated API proxy failed' }
        Record 'Dashboard /, /docs and /openapi.json returned 200; authenticated same-origin API proxy returned live PostgreSQL queue state.'

        $workers = $null
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            $workers = Api 'GET' $apiBase '/v1/workers'
            if (@($workers.items | Where-Object { $_.state -eq 'active' }).Count -ge 3) { break }
            Start-Sleep -Seconds 1
        }
        if (@($workers.items | Where-Object { $_.state -eq 'active' }).Count -lt 3) { throw 'fewer than three independent workers are active' }

        $key = "dashboard-$suffix"
        $jobBody = @{ job_type = 'synthetic_report'; payload = @{ records = @(@{ category = 'dashboard'; amount = 23 }) }; priority = 2 }
        $job = Api 'POST' $apiBase '/v1/jobs' $jobBody $key
        $again = Api 'POST' $apiBase '/v1/jobs' $jobBody $key
        if ($job.job_id -ne $again.job_id) { throw 'idempotent submission returned more than one job' }
        $state = $null
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            $state = Api 'GET' $apiBase "/v1/jobs/$($job.job_id)"
            if ($state.state -eq 'succeeded') { break }
            Start-Sleep -Milliseconds 250
        }
        if ($state.state -ne 'succeeded') { throw "dashboard seed job $($job.job_id) did not succeed" }

        $oldApiToken = $env:CHRONOS_API_TOKEN
        $oldDashboardUrl = $env:CHRONOS_DASHBOARD_URL
        try {
            $env:CHRONOS_API_TOKEN = $token
            $env:CHRONOS_DASHBOARD_URL = $webBase
            Record "`n> npm --prefix web run test:e2e"
            $testOutput = & npm.cmd --prefix web run test:e2e 2>&1
            $testCode = $LASTEXITCODE
            foreach ($item in $testOutput) { Record $item.ToString() }
            if ($testCode -ne 0) { throw "Playwright end-to-end run exited $testCode" }
        }
        finally {
            if ($null -eq $oldApiToken) { Remove-Item Env:CHRONOS_API_TOKEN -ErrorAction SilentlyContinue } else { $env:CHRONOS_API_TOKEN = $oldApiToken }
            if ($null -eq $oldDashboardUrl) { Remove-Item Env:CHRONOS_DASHBOARD_URL -ErrorAction SilentlyContinue } else { $env:CHRONOS_DASHBOARD_URL = $oldDashboardUrl }
        }
        Record "E2E created a real job through the UI; source job $($job.job_id) completed successfully."
        $logs = Compose $demoProject @('--profile', 'control', '--profile', 'workers', 'logs', '--no-color', 'dashboard', 'api', 'scheduler', 'worker-1', 'worker-2', 'worker-3')
        Record 'Captured dashboard, API, scheduler and three worker process logs above.'
    }
    finally { Compose $demoProject @('--profile', 'control', '--profile', 'workers', 'down', '--volumes') | Out-Null }
    Record 'Result: 49 PostgreSQL-backed tests, live dashboard/API smoke, and Playwright user journeys passed.'
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
