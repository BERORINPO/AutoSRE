<#
    AutoSRE demo launcher.

    One command instead of a pasted pipeline. The console needs the key in the
    URL (EventSource cannot send headers), so this reads it from the deployed
    service at run time and hands it straight to the browser - the key is never
    printed and never written to disk.

    Usage:
      .\scripts\demo.ps1              # break the target, then open the console
      .\scripts\demo.ps1 open         # just open the console
      .\scripts\demo.ps1 status       # guard + trust ledger + target health
      .\scripts\demo.ps1 autonomy-on  # let promoted classes recover with no click
      .\scripts\demo.ps1 autonomy-off # back to the human approval gate
#>
param(
    [ValidateSet('arm', 'open', 'status', 'autonomy-on', 'autonomy-off')]
    [string]$Action = 'arm'
)

$ErrorActionPreference = 'Stop'

$Project   = 'bero-devops-agent'
$Region    = 'asia-northeast1'
$AgentUrl  = 'https://sida-agent-860561433627.asia-northeast1.run.app'
$TargetUrl = 'https://sida-target-860561433627.asia-northeast1.run.app'

$sdk = Join-Path $env:LOCALAPPDATA 'Google\Cloud SDK\google-cloud-sdk\bin'
if (Test-Path $sdk) { $env:Path += ";$sdk" }

function Get-ConsoleKey {
    $svc = gcloud run services describe sida-agent --project $Project --region $Region --format=json | ConvertFrom-Json
    $key = ($svc.spec.template.spec.containers[0].env | Where-Object name -eq 'AUTOSRE_CONSOLE_KEY').value
    if ([string]::IsNullOrEmpty($key)) { throw 'AUTOSRE_CONSOLE_KEY is not set on the service.' }
    return $key
}

function Invoke-Agent([string]$Path, [string]$Method = 'GET') {
    $key = Get-ConsoleKey
    $req = @{
        Uri        = "$AgentUrl$Path"
        Method     = $Method
        Headers    = @{ Authorization = "Bearer $key" }
        TimeoutSec = 120
    }
    # A bodyless POST is rejected by the Cloud Run front end with 411 before it
    # ever reaches the container, so always send something.
    if ($Method -eq 'POST') { $req['Body'] = '' }
    return Invoke-RestMethod @req
}

function Get-TargetCode {
    # -SkipHttpErrorCheck is pwsh 7+ only, and the target is SUPPOSED to be 503
    # for most of this script's life. Both editions throw on a non-2xx, so read
    # the code out of the exception instead of asking for a flag 5.1 lacks.
    try {
        return [int](Invoke-WebRequest -Uri "$TargetUrl/health" -TimeoutSec 15 -ErrorAction Stop).StatusCode
    } catch {
        $resp = $_.Exception.Response
        if ($resp -and $resp.StatusCode) { return [int]$resp.StatusCode }
        return 0
    }
}

function Wait-Target([int]$Want, [int]$TimeoutSec = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ((Get-TargetCode) -eq $Want) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Open-Console {
    $key = Get-ConsoleKey
    Start-Process "$AgentUrl/?key=$key"
    Write-Host 'コンソールをブラウザで開きました。' -ForegroundColor Green
}

switch ($Action) {

    'arm' {
        Write-Host '障害を発生させています (DATABASE_URL を削除)...' -ForegroundColor Yellow
        $r = Invoke-Agent -Path '/reset' -Method POST
        if (-not $r.inject.ok) { throw "reset failed: $($r | ConvertTo-Json -Compress)" }
        Write-Host 'ロールアウト待ち...' -ForegroundColor Yellow
        if (Wait-Target -Want 503) {
            Write-Host 'sida-target = 503 (壊れました)' -ForegroundColor Red
        } else {
            Write-Warning '503 になりませんでした。手動で確認してください。'
        }
        Open-Console
        Write-Host ''
        Write-Host '[Run AutoSRE] を押すと調査が始まります。' -ForegroundColor Cyan
    }

    'open' { Open-Console }

    'status' {
        $guard = Invoke-Agent -Path '/guard'
        $trust = Invoke-Agent -Path '/trust'
        $code  = Get-TargetCode

        $health = if ($code -eq 200) { '(正常)' } else { '(異常 = デモ可能)' }
        $switch = if ($guard.killswitch.tripped) { 'ON (要解除)' } else { 'OFF' }
        $auto   = if ($trust.enabled) { 'ON' } else { 'OFF (承認ゲートあり)' }

        Write-Host ''
        Write-Host "対象サービス : $code $health"
        Write-Host "コストガード : 本日 $($guard.runs_today) 回 / 停止スイッチ $switch"
        Write-Host "自律実行     : $auto"
        foreach ($c in $trust.classes) {
            $state = if ($c.demoted) { '降格中' } elseif ($c.promoted) { '開放' } else { 'ゲート内' }
            Write-Host ("  {0,-14} {1,3}/{2,-3} 下限 {3:N3} / {4:N2}  -> {5}" -f `
                $c.env_var, $c.successes, $c.attempts, $c.wilson_lower_bound, $c.threshold, $state)
        }
        Write-Host ''
    }

    'autonomy-on' {
        gcloud run services update sida-agent --update-env-vars AUTOSRE_AUTONOMY_ENABLED=1 `
            --project $Project --region $Region --quiet | Out-Null
        Write-Host '自律実行を ON にしました。' -ForegroundColor Green
        Write-Warning '終了後は必ず autonomy-off に戻してください (毎時の再破壊と追いかけ合って PR を量産します)。'
    }

    'autonomy-off' {
        gcloud run services update sida-agent --remove-env-vars AUTOSRE_AUTONOMY_ENABLED `
            --project $Project --region $Region --quiet | Out-Null
        Write-Host '自律実行を OFF にしました (承認ゲートあり)。' -ForegroundColor Green
    }
}
