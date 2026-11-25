param([Parameter(Mandatory=$true)][string]$OutFile)
$ErrorActionPreference = 'Stop'
$stateDir = Join-Path $PSScriptRoot '../terraform/.kiln-local'
$tokenPath = Join-Path $stateDir 'credential/sensor.token'
$names = @('kiln-gateway', 'kiln-1', 'kiln-2')
$checks = [Collections.Generic.List[object]]::new()
function Record([string]$Check, $Expected, $Observed) {
    $checks.Add([pscustomobject]@{ check = $Check; expected = $Expected; observed = $Observed; pass = ($Expected -eq $Observed) })
}

$ports = @{}
foreach ($name in $names) {
    $meta = Get-Content -Raw -LiteralPath (Join-Path $stateDir "$name.json") | ConvertFrom-Json
    $port = [int]$meta.spec.PORT
    $ports[$name] = $port
    $endpoints = @(Get-NetTCPConnection -State Listen -LocalPort $port)
    $onlyLocal = $endpoints.Count -eq 1 -and $endpoints[0].LocalAddress -eq '127.0.0.1' -and $endpoints[0].OwningProcess -eq $meta.pid
    Record "loopback_and_owner_$name" $true $onlyLocal
}

$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($pair in @(@('credential', $tokenPath), @('runtime', $stateDir))) {
    $acl = Get-Acl -LiteralPath $pair[1]
    $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
    $allow = @($rules | Where-Object { $_.AccessControlType -eq 'Allow' } | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)
    $allowed = @($me)
    if ($pair[0] -eq 'runtime') { $allowed += 'S-1-5-18' }
    $exactPrincipals = $null -eq (Compare-Object $allowed $allow)
    Record ("restricted_dacl_" + $pair[0]) $true ($acl.AreAccessRulesProtected -and $exactPrincipals)
}

Add-Type -AssemblyName System.Net.Http
$handler = [Net.Http.HttpClientHandler]::new()
$handler.UseProxy = $false
$client = [Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(3)
function Status([int]$Port, [string]$Method, [string]$Route, [string]$Credential = '') {
    $req = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::new($Method), "http://127.0.0.1:$Port$Route")
    $resp = $null
    try {
        if ($Method -eq 'POST') {
            # Empty list exercises both auth and payload validation without writing anything.
            $req.Content = [Net.Http.StringContent]::new('{"readings":[]}', [Text.Encoding]::UTF8, 'application/json')
        }
        if ($Credential) { $req.Headers.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $Credential) }
        $resp = $client.SendAsync($req).GetAwaiter().GetResult()
        return [int]$resp.StatusCode
    } finally {
        if ($resp) { $resp.Dispose() }
        $req.Dispose()
    }
}
$gateway = $ports['kiln-gateway']
try {
    Record 'dashboard_without_producer_credential' 200 (Status $gateway 'GET' '/')
    Record 'gateway_denies_metrics' 403 (Status $gateway 'GET' '/metrics')
    Record 'operator_can_read_loopback_metrics' 200 (Status $ports['kiln-1'] 'GET' '/metrics')
    Record 'gateway_denies_delete' 405 (Status $gateway 'DELETE' '/api/readings')
    Record 'post_without_credential' 401 (Status $gateway 'POST' '/api/readings')
    Record 'post_with_wrong_credential' 401 (Status $gateway 'POST' '/api/readings' 'invalid-lab-credential')
    $producer = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $tokenPath).Path).Trim()
    Record 'authenticated_empty_batch_reaches_validation' 422 (Status $gateway 'POST' '/api/readings' $producer)
} finally { $client.Dispose() }

$failed = @($checks | Where-Object { -not $_.pass }).Count
$result = [ordered]@{
    timestamp_utc = [DateTimeOffset]::UtcNow.ToString('o')
    scope = 'One Windows host; same-host users can reach loopback listeners. Producer writes require the local credential.'
    cases = $checks.Count; failed = $failed
    checks = $checks.ToArray()
}
$outputPath = [IO.Path]::GetFullPath($OutFile)
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($outputPath)) | Out-Null
[IO.File]::WriteAllText($outputPath, ($result | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
Write-Output "checks=$($checks.Count) failed=$failed"
if ($failed) { exit 1 }
