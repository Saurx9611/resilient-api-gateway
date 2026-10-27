param()

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$evidencePath = Join-Path $repo 'docs/verification/phase-4-recovery.txt'
$suffix = [Guid]::NewGuid().ToString('N').Substring(0, 8)
$testProject = "rag-worker-p4-test-$suffix"
$demoProject = "rag-worker-p4-demo-$suffix"
$composeFile = Join-Path $repo 'compose.workers.yaml'
$evidence = [System.Collections.Generic.List[string]]::new()

function Add-Evidence([string]$line) {
    $evidence.Add($line)
    Write-Host $line
}

function Invoke-ComposeLogged([string[]]$arguments) {
    Add-Evidence ("`n> docker compose " + ($arguments -join ' '))
    $lines = [System.Collections.Generic.List[string]]::new()
    & docker compose @arguments 2>&1 | ForEach-Object {
        $line = $_.ToString()
        $lines.Add($line)
        Add-Evidence $line
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "docker compose failed with exit code $exitCode" }
    return $lines.ToArray()
}

function Base-Compose([string]$project) {
    return @('-p', $project, '-f', $composeFile)
}

function Parse-LastJson([string[]]$lines) {
    for ($index = $lines.Count - 1; $index -ge 0; $index--) {
        $line = $lines[$index]
        if ($line.StartsWith('{') -or $line.StartsWith('[')) {
            return ConvertFrom-Json -InputObject $line
        }
    }
    throw 'compose command produced no JSON result'
}

Push-Location $repo
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $evidencePath) | Out-Null
    Add-Evidence "Phase 4 worker recovery verification; UTC $([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))"
    Add-Evidence (docker version --format 'Client {{.Client.Version}}; Server {{.Server.Version}}')
    Add-Evidence "Projects: $testProject and $demoProject (this script only removes these unique Compose projects)."

    $testBase = Base-Compose $testProject
    try {
        Invoke-ComposeLogged ($testBase + @('--profile', 'test', 'run', '--build', '--rm', 'tests')) | Out-Null
    }
    finally {
        Invoke-ComposeLogged ($testBase + @('down', '--volumes')) | Out-Null
    }

    $demoBase = Base-Compose $demoProject
    try {
        Invoke-ComposeLogged ($demoBase + @('--profile', 'workers', 'up', '--build', '--detach')) | Out-Null

        $workers = @()
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            $workerLines = Invoke-ComposeLogged ($demoBase + @('--profile', 'ops', 'run', '--rm', 'workers'))
            $workers = Parse-LastJson $workerLines
            if (@($workers | Where-Object state -eq 'active').Count -ge 3) { break }
            Start-Sleep -Seconds 1
        }
        if (@($workers | Where-Object state -eq 'active').Count -lt 3) { throw 'three independent workers did not become active' }
        Add-Evidence ("Verified three active process UUIDs: " + (($workers | ForEach-Object worker_id) -join ', '))

        $png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe'
        $jobs = @(
            @{ type = 'image_metadata'; payload = @{ png_base64 = $png } },
            @{ type = 'csv_transform'; payload = @{ csv_text = "name,city`nAda,london`n"; column = 'city' } },
            @{ type = 'synthetic_report'; payload = @{ records = @(@{ category = 'books'; amount = 4 }, @{ category = 'books'; amount = 7 }) } },
            @{ type = 'notification_simulation'; payload = @{ template = 'receipt'; recipient_ref = 'demo_account'; delay_ms = 100 } }
        )
        $jobIds = [System.Collections.Generic.List[int]]::new()
        foreach ($job in $jobs) {
            $payloadJson = $job.payload | ConvertTo-Json -Compress -Depth 8
            $enqueueLines = Invoke-ComposeLogged ($demoBase + @('--profile', 'ops', 'run', '--rm', 'enqueue', 'enqueue', '--type', $job.type, '--payload-json', $payloadJson))
            $queued = Parse-LastJson $enqueueLines
            $jobIds.Add([int]$queued.job_id)
        }

        $complete = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            $statusLines = Invoke-ComposeLogged ($demoBase + @('--profile', 'ops', 'run', '--rm', 'status', 'status', '--limit', '100'))
            $statuses = Parse-LastJson $statusLines
            $complete = @($jobIds | ForEach-Object { $id = $_; @($statuses | Where-Object { $_.id -eq $id -and $_.state -eq 'succeeded' }).Count -gt 0 } | Where-Object { -not $_ }).Count -eq 0
            if ($complete) { break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $complete) { throw "demo jobs did not all succeed: $($jobIds -join ',')" }
        Add-Evidence ("All four allowlisted workload jobs succeeded: " + ($jobIds -join ', '))

        $outagePayload = @{ template = 'alert'; recipient_ref = 'postgres_outage'; delay_ms = 5000 } | ConvertTo-Json -Compress
        $outageLines = Invoke-ComposeLogged ($demoBase + @('--profile', 'ops', 'run', '--rm', 'enqueue', 'enqueue', '--type', 'notification_simulation', '--payload-json', $outagePayload, '--max-attempts', '4'))
        $outageJob = [int](Parse-LastJson $outageLines).job_id
        $running = $false
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            $poll = Invoke-ComposeLogged ($demoBase + @('--profile', 'ops', 'run', '--rm', 'status', 'status', '--limit', '100'))
            $outageStatus = Parse-LastJson $poll | Where-Object { $_.id -eq $outageJob }
            if ($outageStatus.state -eq 'running') { $running = $true; break }
            Start-Sleep -Milliseconds 200
        }
        if (-not $running) { throw 'outage scenario job did not enter running state' }
        Add-Evidence "Stopping and restarting only this isolated Compose PostgreSQL service while job $outageJob is active."
        Invoke-ComposeLogged ($demoBase + @('stop', '--timeout', '1', 'postgres')) | Out-Null
        Start-Sleep -Seconds 3
        Invoke-ComposeLogged ($demoBase + @('start', 'postgres')) | Out-Null

        $postgresHealthy = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            $health = & docker inspect --format '{{.State.Health.Status}}' "$demoProject-postgres-1" 2>$null
            if ($LASTEXITCODE -eq 0 -and $health.Trim() -eq 'healthy') { $postgresHealthy = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $postgresHealthy) { throw 'isolated PostgreSQL healthcheck did not recover after restart' }
        Add-Evidence 'Isolated PostgreSQL healthcheck returned healthy after restart.'

        $recovered = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            $poll = Invoke-ComposeLogged ($demoBase + @('--profile', 'ops', 'run', '--rm', 'status', 'status', '--limit', '100'))
            $outageStatus = Parse-LastJson $poll | Where-Object { $_.id -eq $outageJob }
            if ($outageStatus.state -eq 'succeeded' -and $outageStatus.attempts -ge 2) { $recovered = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $recovered) { throw "worker processes did not recover job $outageJob after temporary PostgreSQL outage" }
        Add-Evidence "Temporary PostgreSQL outage recovered job $outageJob on attempt $($outageStatus.attempts)."
        Invoke-ComposeLogged ($demoBase + @('logs', '--no-color', 'worker-1', 'worker-2', 'worker-3')) | Out-Null
    }
    finally {
        Invoke-ComposeLogged ($demoBase + @('--profile', 'workers', '--profile', 'ops', 'down', '--volumes')) | Out-Null
    }

    Add-Evidence "Result: all PostgreSQL lease, fencing, retry/replay, process crash-window, temporary database outage, SIGTERM, and SIGKILL recovery checks passed."
    Set-Content -LiteralPath $evidencePath -Value $evidence -Encoding utf8
    Write-Host "Evidence written to $evidencePath"
}
catch {
    Add-Evidence ("FAILED: " + $_.Exception.Message)
    Set-Content -LiteralPath $evidencePath -Value $evidence -Encoding utf8
    throw
}
finally {
    Pop-Location
}
