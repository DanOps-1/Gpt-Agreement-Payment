param(
    [string]$Config = "CTF-pay\config.local-mock.json",
    [string]$Scenario = "",
    [switch]$NoJsonResult
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    throw "Missing .venv. Run .\scripts\setup_local_env.ps1 first."
}

Set-Location $RepoRoot
New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "output") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "output\logs") | Out-Null
$env:WEBUI_DATA_DIR = Join-Path $RepoRoot "output"

$ConfigPath = $Config
if (-not [System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath = Join-Path $RepoRoot $ConfigPath
}
if (-not (Test-Path $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

$RunConfig = Join-Path $RepoRoot "output\local-mock.config.json"
$ArtifactPath = Join-Path $RepoRoot "output\ctf_local_mock_latest.json"
$Cfg = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json

if (-not $Cfg.PSObject.Properties.Name.Contains("local_mock")) {
    $Cfg | Add-Member -MemberType NoteProperty -Name "local_mock" -Value ([pscustomobject]@{})
}

$Cfg.local_mock | Add-Member -MemberType NoteProperty -Name "enabled" -Value $true -Force
$Cfg.local_mock | Add-Member -MemberType NoteProperty -Name "artifact_path" -Value $ArtifactPath -Force
if ($Scenario.Trim()) {
    $Cfg.local_mock | Add-Member -MemberType NoteProperty -Name "scenario" -Value $Scenario.Trim() -Force
}

$Cfg | ConvertTo-Json -Depth 32 | Set-Content -Encoding UTF8 -Path $RunConfig

$CardPy = Join-Path $RepoRoot "CTF-pay\card.py"
$ArgsList = @(
    $CardPy,
    "auto",
    "--config", $RunConfig,
    "--local-mock"
)
if (-not $NoJsonResult) {
    $ArgsList += "--json-result"
}

Write-Host "[local-mock] config:   $RunConfig"
Write-Host "[local-mock] artifact: $ArtifactPath"
Write-Host "[local-mock] logs:     $(Join-Path $RepoRoot 'output\logs\card.log')"
& $PythonExe @ArgsList
if ($LASTEXITCODE -ne 0) {
    throw "local mock run failed with exit code $LASTEXITCODE"
}
