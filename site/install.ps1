# agents.org installer (Windows)
#   irm https://agentsorg.sahilsidhu.pro/install.ps1 | iex
# or from a local checkout:  .\install.ps1
# Upgrading from 0.7: run it again. Your companies, clients and history are kept.
$ErrorActionPreference = "Stop"
$version = "0.8.3"
$src = if ($env:AGENTSORG_SRC) { $env:AGENTSORG_SRC } `
       elseif ($PSScriptRoot -and (Test-Path "$PSScriptRoot\pyproject.toml")) { $PSScriptRoot } `
       else { "https://agentsorg.sahilsidhu.pro/agentsorg-$version-py3-none-any.whl" }

Write-Host "Installing agents.org $version"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "Python 3.11+ is required: https://www.python.org/downloads/ (tick 'Add python.exe to PATH')" }
$ok = & python -c "import sys;print(sys.version_info>=(3,11))"
if ($ok -ne "True") { throw "Python 3.11+ is required (found $(& python -V))" }
$hasClaude = [bool](Get-Command claude -ErrorAction SilentlyContinue)

if (Get-Command pipx -ErrorAction SilentlyContinue) {
  pipx install --force $src | Out-Null
} else {
  # a private venv, so nothing else on this PC is touched
  $venv = Join-Path $env:LOCALAPPDATA "agentsorg\venv"
  python -m venv $venv
  & "$venv\Scripts\python.exe" -m pip install -q --upgrade --force-reinstall $src
  $bin = Join-Path $env:LOCALAPPDATA "agentsorg\bin"
  New-Item -ItemType Directory -Force $bin | Out-Null
  Set-Content -Encoding ascii (Join-Path $bin "agentsorg.cmd") "@`"$venv\Scripts\agentsorg.exe`" %*"
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (($userPath -split ";") -notcontains $bin) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
    $env:Path += ";$bin"
    Write-Host "Added $bin to your PATH (open a new terminal to use it everywhere)."
  }
}

Write-Host "`nInstalled agents.org $version." -ForegroundColor Green
if (-not $hasClaude) {
  Write-Host "`n1. Install Claude Code and log in once (agents.org runs on your Claude subscription):"
  Write-Host "     irm https://claude.ai/install.ps1 | iex; claude"
}
Write-Host "`nStart a company:"
Write-Host "  agentsorg init `$HOME\my-company"
Write-Host "  cd `$HOME\my-company; agentsorg start        # opens the dashboard"
Write-Host "`nIn the dashboard: describe your startup, accept the teams Claude designs, then tell it what to do."
Write-Host "Keep it running in the background:  agentsorg service install"
