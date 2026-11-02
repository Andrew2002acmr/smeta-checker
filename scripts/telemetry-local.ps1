param([ValidateSet('start','stop','status','logs')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    $composeArgs = @('compose', '--env-file', '.env.local', '-f', 'compose.telemetry.local.yaml')
    switch ($Action) {
        'start' { & docker @composeArgs up -d }
        'stop' { & docker @composeArgs stop }
        'status' { & docker @composeArgs ps }
        'logs' { & docker @composeArgs logs --tail 100 }
    }
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed: $LASTEXITCODE" }
} finally { Pop-Location }
