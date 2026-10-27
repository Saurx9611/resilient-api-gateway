$ErrorActionPreference = 'Stop'

$base = if ($env:CHRONOS_API_URL) { $env:CHRONOS_API_URL.TrimEnd('/') } else { 'http://127.0.0.1:8000' }
$token = $env:CHRONOS_API_TOKEN
if (-not $token) { throw 'Set CHRONOS_API_TOKEN before running these examples.' }
$headers = @{ Authorization = "Bearer $token"; 'Idempotency-Key' = 'chronos-example-report-001' }

$jobBody = @{
    job_type = 'synthetic_report'
    payload = @{ records = @(@{ category = 'books'; amount = 4 }, @{ category = 'books'; amount = 7 }) }
    priority = 2
    max_attempts = 4
} | ConvertTo-Json -Depth 8 -Compress

$first = Invoke-RestMethod -Method Post -Uri "$base/v1/jobs" -Headers $headers -ContentType 'application/json' -Body $jobBody
$repeat = Invoke-RestMethod -Method Post -Uri "$base/v1/jobs" -Headers $headers -ContentType 'application/json' -Body $jobBody
if ($first.job_id -ne $repeat.job_id) { throw 'Idempotent submission returned different job IDs.' }
"Swagger UI: $base/docs"
"OpenAPI JSON: $base/openapi.json"
"Job submission (same ID returned twice):"
$first | ConvertTo-Json -Depth 8
"Job state:"
Invoke-RestMethod -Method Get -Uri "$base/v1/jobs/$($first.job_id)" -Headers @{ Authorization = "Bearer $token" } | ConvertTo-Json -Depth 8
"Attempt history:"
Invoke-RestMethod -Method Get -Uri "$base/v1/jobs/$($first.job_id)/attempts" -Headers @{ Authorization = "Bearer $token" } | ConvertTo-Json -Depth 8
"Queue stats:"
Invoke-RestMethod -Method Get -Uri "$base/v1/queue/stats" -Headers @{ Authorization = "Bearer $token" } | ConvertTo-Json -Depth 8
"Workers:"
Invoke-RestMethod -Method Get -Uri "$base/v1/workers" -Headers @{ Authorization = "Bearer $token" } | ConvertTo-Json -Depth 8

$scheduleBody = @{
    job_type = 'synthetic_report'
    payload = @{ records = @() }
    cron_expression = '0 9 * * 1-5'
    timezone = 'America/New_York'
    priority = 1
} | ConvertTo-Json -Depth 8 -Compress
$schedule = Invoke-RestMethod -Method Post -Uri "$base/v1/schedules" -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' -Body $scheduleBody
"Created recurring schedule:"
$schedule | ConvertTo-Json -Depth 8
$cancelBody = @{ reason = 'example schedule cleanup' } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri "$base/v1/schedules/$($schedule.schedule_id)/cancel" -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' -Body $cancelBody | ConvertTo-Json -Depth 8
