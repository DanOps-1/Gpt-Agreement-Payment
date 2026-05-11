param(
    [switch]$BackendOnly,
    [switch]$FrontendOnly,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $RepoRoot "webui\frontend"

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
$env:WEBUI_DATA_DIR = Join-Path $RepoRoot "output"

if (-not $FrontendOnly) {
    Write-Host "[test] backend pytest"
    Invoke-Checked -Label "backend pytest" -Command { & $PythonExe -m pytest webui\tests -q }
}

if (-not $BackendOnly) {
    if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
        throw "pnpm was not found. Run .\scripts\setup_local_env.ps1 first."
    }

    Push-Location $FrontendDir
    try {
        Write-Host "[test] frontend vitest"
        Invoke-Checked -Label "frontend vitest" -Command { pnpm test }

        if (-not $SkipBuild) {
            Write-Host "[test] frontend build"
            Invoke-Checked -Label "frontend build" -Command { pnpm build }
        }
    } finally {
        Pop-Location
    }
}
