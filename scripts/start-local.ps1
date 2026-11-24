param(
    [string]$DatabaseHost = $env:LOCAL_DATABASE_HOST,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

$pidFile = Join-Path (Get-Location).Path 'var/api-local.pid'

if ($Stop) {
    if (Test-Path -LiteralPath $pidFile) {
        $record = Get-Content -Raw -LiteralPath $pidFile | ConvertFrom-Json
        $server = Get-Process -Id $record.id -ErrorAction SilentlyContinue
        if ($server -and $server.StartTime.ToUniversalTime().Ticks.ToString() -eq $record.started) {
            & taskkill.exe /PID $server.Id /T /F | Out-Null
            if ($LASTEXITCODE -ne 0 -and -not $server.HasExited) {
                throw 'Could not stop the local API.'
            }
            Write-Output 'Local API stopped.'
            exit 0
        }
    }
    Write-Output 'Local API is not running.'
    exit 0
}

$config = @{}
foreach ($line in Get-Content -Encoding UTF8 -LiteralPath '.env.docker') {
    if ($line -match '^\s*(DATABASE_URL|SESSION_SECRET|API_PORT)\s*=\s*(.*?)\s*$') {
        $config[$Matches[1]] = $Matches[2].Trim('"', "'")
    }
}

if (-not $DatabaseHost -and (Test-Path -LiteralPath '.env')) {
    foreach ($line in Get-Content -Encoding UTF8 -LiteralPath '.env') {
        if ($line -match '^\s*LOCAL_DATABASE_HOST\s*=\s*(.*?)\s*$') {
            $DatabaseHost = $Matches[1].Trim('"', "'")
        }
    }
}
if (-not $DatabaseHost) {
    throw 'Set LOCAL_DATABASE_HOST in .env or pass -DatabaseHost.'
}
if (-not $config.DATABASE_URL -or -not $config.SESSION_SECRET) {
    throw 'Set DATABASE_URL and SESSION_SECRET in .env.docker.'
}

$databaseUrl = [System.UriBuilder]::new($config.DATABASE_URL)
$databaseUrl.Host = $DatabaseHost
$env:DATABASE_URL = $databaseUrl.Uri.AbsoluteUri
$env:SESSION_SECRET = $config.SESSION_SECRET
$env:COOKIE_SECURE = 'false'

$port = $env:API_PORT
if (-not $port) { $port = $config.API_PORT }
if (-not $port) { $port = 8000 }
$port = [int]$port

$python = 'python'
if (Test-Path -LiteralPath '.venv/Scripts/python.exe') {
    $python = (Resolve-Path -LiteralPath '.venv/Scripts/python.exe').Path
} elseif (Test-Path -LiteralPath '.deps') {
    $dependencies = (Resolve-Path -LiteralPath '.deps').Path
    $env:PYTHONPATH = (@($dependencies, $env:PYTHONPATH) | Where-Object { $_ }) -join [IO.Path]::PathSeparator
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pidFile) | Out-Null
$lockFile = "$pidFile.lock"
try {
    $runLock = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None')
} catch [IO.IOException] {
    throw 'The local API is already starting or running.'
}

try {
    $server = Start-Process -FilePath $python -NoNewWindow -PassThru -ArgumentList @(
        '-m', 'uvicorn', 'app.main:create_app', '--factory', '--host', '127.0.0.1', '--port', $port
    )
    $record = @{ id = $server.Id; started = $server.StartTime.ToUniversalTime().Ticks.ToString() }
    $record | ConvertTo-Json -Compress | Set-Content -LiteralPath $pidFile -Encoding ASCII
    $server.WaitForExit()
    $exitCode = $server.ExitCode
} finally {
    if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
    $runLock.Dispose()
}
exit $exitCode
