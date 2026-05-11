param(
    [switch]$SkipFrontend,
    [switch]$SkipBuild,
    [string]$PythonPath = "",
    [string]$PipIndexUrl = "",
    [string]$PipProxy = "",
    [string[]]$PipTrustedHost = @()
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvDir = Join-Path $RepoRoot ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$FrontendDir = Join-Path $RepoRoot "webui\frontend"

Set-Location $RepoRoot

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

function Get-PythonVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [string[]]$PrefixArgs = @()
    )

    try {
        $out = & $Command @PrefixArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return [version]($out | Select-Object -Last 1)
    } catch {
        return $null
    }
}

function Test-Python311 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [string[]]$PrefixArgs = @()
    )

    $ver = Get-PythonVersion -Command $Command -PrefixArgs $PrefixArgs
    return ($null -ne $ver -and $ver -ge [version]"3.11")
}

$BootstrapPythonCommand = ""
$BootstrapPythonArgs = @()

if ($PythonPath.Trim()) {
    $BootstrapPythonCommand = $PythonPath.Trim()
    if (-not (Test-Python311 -Command $BootstrapPythonCommand)) {
        $ver = Get-PythonVersion -Command $BootstrapPythonCommand
        throw "PythonPath must point to Python 3.11+. Current version: $ver"
    }
} elseif ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python311 -Command "py" -PrefixArgs @("-3"))) {
    $BootstrapPythonCommand = "py"
    $BootstrapPythonArgs = @("-3")
} elseif ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-Python311 -Command "python")) {
    $BootstrapPythonCommand = "python"
} else {
    $current = $null
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $current = Get-PythonVersion -Command "python"
    }
    throw "Python 3.11+ was not found. Current python version: $current. Install Python 3.11+ or pass -PythonPath C:\Path\To\python.exe."
}

if (-not (Test-Path $PythonExe)) {
    Write-Host "[setup] creating Python virtual environment: .venv"
    Invoke-Checked -Label "venv creation" -Command {
        & $BootstrapPythonCommand @BootstrapPythonArgs -m venv $VenvDir
    }
} elseif (-not (Test-Python311 -Command $PythonExe)) {
    $venvVersion = Get-PythonVersion -Command $PythonExe
    throw "Existing .venv uses Python $venvVersion. Delete .venv and rerun this script with Python 3.11+."
}

Write-Host "[setup] upgrading pip"
$PipSourceArgs = @()
if ($PipIndexUrl.Trim()) {
    $PipSourceArgs = @("--index-url", $PipIndexUrl.Trim())
}
if ($PipProxy.Trim()) {
    $PipSourceArgs += @("--proxy", $PipProxy.Trim())
}
foreach ($HostName in $PipTrustedHost) {
    if ($HostName.Trim()) {
        $PipSourceArgs += @("--trusted-host", $HostName.Trim())
    }
}

Invoke-Checked -Label "pip upgrade" -Command {
    & $PythonExe -m pip install @PipSourceArgs --upgrade pip
}

Write-Host "[setup] installing Python dependencies"
Invoke-Checked -Label "Python dependency installation" -Command {
    & $PythonExe -m pip install @PipSourceArgs -r (Join-Path $RepoRoot "webui\requirements.txt")
}

New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "output") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "output\logs") | Out-Null

if (-not $SkipFrontend) {
    if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
        if (Get-Command corepack -ErrorAction SilentlyContinue) {
            Write-Host "[setup] pnpm not found; enabling it through corepack"
            Invoke-Checked -Label "corepack enable" -Command { corepack enable }
            Invoke-Checked -Label "corepack pnpm activation" -Command { corepack prepare pnpm@latest --activate }
        } else {
            throw "pnpm was not found. Install Node.js 20+ with corepack, or install pnpm, then rerun this script."
        }
    }

    Push-Location $FrontendDir
    try {
        Write-Host "[setup] installing frontend dependencies"
        Invoke-Checked -Label "frontend dependency installation" -Command { pnpm install }

        if (-not $SkipBuild) {
            Write-Host "[setup] building frontend"
            Invoke-Checked -Label "frontend build" -Command { pnpm build }
        }
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "[setup] local environment is ready."
Write-Host "        Start Web UI:     .\scripts\start_webui.ps1"
Write-Host "        Run local mock:   .\scripts\run_local_mock.ps1"
Write-Host "        Run tests:        .\scripts\run_local_tests.ps1"
