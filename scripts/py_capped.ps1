<#
.SYNOPSIS
    Запустити Python-скрипт проекту в тестовому образі з лімітом памʼяті (1 ГБ, без swap).
    Для eval і разових перевірок: той самий захист, що й для тестів.
.EXAMPLE
    .\scripts\py_capped.ps1 eval\run_eval.py
    .\scripts\py_capped.ps1 eval\run_eval.py -Network   # коли потрібна мережа (Groq)
#>
param(
    [Parameter(Mandatory = $true)][string]$Script,
    [string[]]$ScriptArgs = @(),
    [switch]$Network,
    [int]$MemoryMB = 1024
)
$Root = Split-Path -Parent $PSScriptRoot
$Image = "$((Split-Path -Leaf $Root).ToLower())-tests"
$args2 = @('run', '--rm', '--memory', "${MemoryMB}m", '--memory-swap', "${MemoryMB}m", '--cpus', '2',
           '-v', "$($Root):/work", '-w', '/work', '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=src',
           '-e', 'PYTHONIOENCODING=utf-8')
if (-not $Network) { $args2 += @('--network', 'none') }
$envFile = Join-Path $Root '.env'
if ($Network -and (Test-Path $envFile)) { $args2 += @('--env-file', $envFile) }
$extraCa = Join-Path $Root '.certs\extra-ca.pem'
if ($Network -and (Test-Path $extraCa)) {
    # Avast підміняє HTTPS: даємо контейнеру системний набір + локальний корінь.
    $args2 += @('-e', 'SSL_CERT_FILE=/work/.certs/bundle.pem')
}
$args2 += @('--entrypoint', 'sh', $Image, '-c')
$cmd = "python $($Script.Replace('\', '/')) $($ScriptArgs -join ' ')"
if ($Network -and (Test-Path $extraCa)) {
    $cmd = "cat /etc/ssl/certs/ca-certificates.crt /work/.certs/extra-ca.pem > /work/.certs/bundle.pem && $cmd"
}
docker @args2 $cmd
exit $LASTEXITCODE
