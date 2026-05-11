param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8765,
    [switch]$Reload,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $RepoRoot "webui\frontend"
$FrontendIndex = Join-Path $FrontendDir "dist\index.html"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path $PythonExe)) {
    throw "Missing .venv. Run .\scripts\setup_local_env.ps1 first."
}

Set-Location $RepoRoot
New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "output") | Out-Null
$env:WEBUI_DATA_DIR = Join-Path $RepoRoot "output"

if ((-not $SkipBuild) -and (-not (Test-Path $FrontendIndex))) {
    if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
        throw "pnpm was not found. Run .\scripts\setup_local_env.ps1 first."
    }
    Push-Location $FrontendDir
    try {
        Write-Host "[webui] frontend build not found; building once"
        Invoke-Checked -Label "frontend build" -Command { pnpm build }
    } finally {
        Pop-Location
    }
}

$Url = "http://$HostName`:$Port"
Write-Host "[webui] WEBUI_DATA_DIR=$env:WEBUI_DATA_DIR"
Write-Host "[webui] open $Url"

$ArgsList = @(
    "-m", "uvicorn",
    "webui.server:create_app",
    "--factory",
    "--host", $HostName,
    "--port", "$Port"
)

if ($Reload) {
    $ArgsList += "--reload"
}

& $PythonExe @ArgsList
if ($LASTEXITCODE -ne 0) {
    throw "webui server exited with code $LASTEXITCODE"
}
