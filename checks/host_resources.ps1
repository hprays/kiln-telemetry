param([Parameter(Mandatory=$true)][string]$OutFile)
$ErrorActionPreference = 'Stop'
$stateDir = Join-Path $PSScriptRoot '../terraform/.kiln-local'
$services = @('kiln-gateway', 'kiln-1', 'kiln-2') | ForEach-Object {
    $record = Get-Content -Raw -LiteralPath (Join-Path $stateDir "$_.json") | ConvertFrom-Json
    $proc = Get-Process -Id $record.pid
    if ($record.name -ne $_ -or $proc.StartTime.ToUniversalTime().ToFileTimeUtc() -ne [long]$record.birth) {
        throw 'A deployed process has changed; rerun after it is stable.'
    }
    [pscustomobject]@{ name = $_; proc = $proc; port = [int]$record.spec.PORT }
}

function Read-Counters {
    $cpu = 0.0
    $bytes = [long]0
    foreach ($service in $services) {
        $service.proc.Refresh()
        if ($service.proc.HasExited) { throw 'Deployed process stopped during measurement.' }
        $cpu += $service.proc.TotalProcessorTime.TotalSeconds
        $bytes += $service.proc.WorkingSet64
    }
    [pscustomobject]@{ cpu_s = $cpu; working_set_bytes = $bytes }
}

$machineRam = (Get-CimInstance -ClassName Win32_ComputerSystem).TotalPhysicalMemory
$cores = [Environment]::ProcessorCount
$gateway = $services[0].port
Add-Type -AssemblyName System.Net.Http
$handler = [Net.Http.HttpClientHandler]::new()
$handler.UseProxy = $false
$http = [Net.Http.HttpClient]::new($handler)
$http.Timeout = [TimeSpan]::FromSeconds(2)
$samples = [Collections.Generic.List[object]]::new()
$ok = 0
$errors = 0
$clock = [Diagnostics.Stopwatch]::StartNew()
$startedAt = [DateTimeOffset]::UtcNow.ToString('o')
$first = Read-Counters
$nextSample = 0
try {
    # A single client offers 10 reads/s for eight seconds. Every request ends before the next.
    for ($i = 0; $i -lt 80; $i++) {
        $delay = [math]::Floor($i * 100 - $clock.Elapsed.TotalMilliseconds)
        if ($delay -gt 0) { Start-Sleep -Milliseconds ([int]$delay) }
        if ($clock.Elapsed.TotalSeconds -ge $nextSample) {
            $counter = Read-Counters
            $samples.Add([pscustomobject]@{
                elapsed_s = [math]::Round($clock.Elapsed.TotalSeconds, 3)
                working_set_mib = [math]::Round($counter.working_set_bytes / 1MB, 3)
            })
            $nextSample = [math]::Floor($clock.Elapsed.TotalSeconds) + 1
        }
        $response = $null
        try {
            $response = $http.GetAsync("http://localhost:$gateway/api/summary").GetAwaiter().GetResult()
            $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
            if ($response.IsSuccessStatusCode -and $body.kilns.Count -eq 6) { $ok++ } else { $errors++ }
        } catch { $errors++ } finally { if ($response) { $response.Dispose() } }
    }
    $remainder = [math]::Floor(8000 - $clock.Elapsed.TotalMilliseconds)
    if ($remainder -gt 0) { Start-Sleep -Milliseconds ([int]$remainder) }
    $last = Read-Counters
    $elapsed = $clock.Elapsed.TotalSeconds
} finally {
    $http.Dispose()
    foreach ($service in $services) { $service.proc.Dispose() }
}
$samples.Add([pscustomobject]@{
    elapsed_s = [math]::Round($elapsed, 3)
    working_set_mib = [math]::Round($last.working_set_bytes / 1MB, 3)
})
$peakMiB = ($samples | Measure-Object working_set_mib -Maximum).Maximum
$cpuOneCore = 100.0 * ($last.cpu_s - $first.cpu_s) / $elapsed
$days = 22; $hours = 8; $watts = 18; $wonPerKwh = 200; $users = 60
$kwh = $days * $hours * $watts / 1000.0
$result = [ordered]@{
    measured = [ordered]@{
        started_at_utc = $startedAt; read_requests = 80; successful_summary_reads = $ok; errors = $errors
        elapsed_s = [math]::Round($elapsed, 3)
        scope = 'gateway and both API processes; working sets can include shared pages'
        process_cpu_s = [math]::Round($last.cpu_s - $first.cpu_s, 4)
        cpu_percent_of_one_core = [math]::Round($cpuOneCore, 3)
        logical_processors = $cores
        cpu_percent_of_all_logical_processors = [math]::Round($cpuOneCore / $cores, 3)
        sampled_peak_working_set_mib = $peakMiB
        sampled_peak_working_set_percent_of_physical_ram = [math]::Round(100.0 * $peakMiB * 1MB / $machineRam, 3)
        working_set_samples = $samples.ToArray()
        db_bytes = (Get-Item -LiteralPath (Join-Path $stateDir 'data.sqlite3')).Length
        cloud_service_fees_krw = 0; hosting = 'existing local PC'
    }
    electricity_scenario = [ordered]@{
        basis = 'Illustrative operating inputs; power and tariff were not measured.'
        assumed_incremental_watts = $watts; hours_per_day = $hours; active_days_per_month = $days
        illustrative_krw_per_kwh = $wonPerKwh; assumed_monthly_active_users = $users
        monthly_kwh = $kwh; monthly_krw = $kwh * $wonPerKwh
        krw_per_monthly_active_user = $kwh * $wonPerKwh / $users
        excludes = 'hardware depreciation, labor, and off-site backup'
    }
}
$outputPath = [IO.Path]::GetFullPath($OutFile)
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($outputPath)) | Out-Null
[IO.File]::WriteAllText($outputPath, ($result | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
Write-Output "reads=$ok/80 errors=$errors peak_working_set_mib=$peakMiB"
if ($errors) { exit 1 }
