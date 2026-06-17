param(
    [switch]$PrintOnly
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Endpoint = "https://mcp.thethirdroom.xyz/mcp"
$McpHost = "127.0.0.1"
$McpPort = "8765"
$EnvFile = Join-Path $Root ".env"
$CloudflaredConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"

function Import-LocalEnv {
    if (Test-Path $EnvFile) {
        Get-Content $EnvFile | Where-Object { $_ -match "=" -and -not $_.TrimStart().StartsWith("#") } | ForEach-Object {
            $key, $value = $_ -split "=", 2
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }

    if (-not $env:FISH_MCP_USERNAME) {
        $env:FISH_MCP_USERNAME = "fish"
    }

    if (-not $env:FISH_MCP_PASSWORD) {
        $bytes = [byte[]]::new(24)
        [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        $env:FISH_MCP_PASSWORD = [Convert]::ToBase64String($bytes)
        Add-Content -Encoding ASCII -Path $EnvFile -Value "FISH_MCP_USERNAME=$env:FISH_MCP_USERNAME"
        Add-Content -Encoding ASCII -Path $EnvFile -Value "FISH_MCP_PASSWORD=$env:FISH_MCP_PASSWORD"
    }

    $env:FISH_MCP_PUBLIC_HOSTS = "mcp.thethirdroom.xyz"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
}

function Get-BasicAuthHeader {
    $pair = "$($env:FISH_MCP_USERNAME):$($env:FISH_MCP_PASSWORD)"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
    "Basic $encoded"
}

function Write-McpConfig {
    $auth = Get-BasicAuthHeader

    Write-Host ""
    Write-Host "======================================================="
    Write-Host "  Fish Speech S2 Pro - UI + MCP + Cloudflare"
    Write-Host "======================================================="
    Write-Host "Public MCP endpoint: $Endpoint"
    Write-Host "Local MCP endpoint:  http://${McpHost}:${McpPort}/mcp"
    Write-Host "MCP username:        $env:FISH_MCP_USERNAME"
    Write-Host "MCP password file:   $EnvFile"
    Write-Host ""
    Write-Host "Claude Code MCP JSON:"
    Write-Host @"
{
  "mcpServers": {
    "fish-audio": {
      "type": "http",
      "url": "$Endpoint",
      "headers": {
        "Authorization": "$auth"
      }
    }
  }
}
"@
    Write-Host ""
    Write-Host "Codex MCP TOML:"
    Write-Host @"
[mcp_servers.fish_audio]
url = "$Endpoint"
headers = { Authorization = "$auth" }
"@
    Write-Host ""
}

function Assert-Command($name, $message) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw $message
    }
}

Import-LocalEnv
Write-McpConfig

if ($PrintOnly) {
    return
}

Assert-Command "uv" "uv was not found in PATH. Run install.bat first, then reopen this shortcut."
Assert-Command "cloudflared" "cloudflared was not found in PATH."

if (-not (Test-Path (Join-Path $Root ".venv\Scripts\activate.bat"))) {
    throw "Virtual environment .venv not found. Run install.bat first."
}
if (-not (Test-Path $CloudflaredConfig)) {
    throw "Missing cloudflared config: $CloudflaredConfig"
}

Write-Host "Starting GUI window..."
Start-Process -FilePath "cmd.exe" -ArgumentList @("/k", "`"$Root\start.bat`"") -WorkingDirectory $Root

Write-Host "Starting MCP server window..."
Start-Process -FilePath "cmd.exe" -ArgumentList @("/k", "`"$Root\start_mcp.bat`" $McpHost $McpPort") -WorkingDirectory $Root

Start-Sleep -Seconds 5

Write-Host "Starting Cloudflare tunnel in this window..."
Write-Host "Leave this window open while using the public MCP endpoint."
Write-Host ""
cloudflared tunnel --config $CloudflaredConfig run
