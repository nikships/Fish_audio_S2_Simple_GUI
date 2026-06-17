@echo off
setlocal
cd /d "%~dp0"

set "MCP_HOST=127.0.0.1"
set "MCP_PORT=8765"
set "CLOUDFLARED_CONFIG=%USERPROFILE%\.cloudflared\config.yml"

echo.
echo =======================================================
echo   Fish Speech S2 Pro - MCP over Cloudflare Tunnel
echo =======================================================
echo Public endpoint: https://mcp.thethirdroom.xyz/mcp
echo Local endpoint:  http://127.0.0.1:%MCP_PORT%/mcp
echo.

where cloudflared >nul 2>nul
if errorlevel 1 goto cloudflared_missing

if not exist "%CLOUDFLARED_CONFIG%" goto config_missing

start "Fish MCP Server" cmd /k ""%~dp0start_mcp.bat" %MCP_HOST% %MCP_PORT%"
timeout /t 5 /nobreak >nul

echo Starting Cloudflare tunnel with %CLOUDFLARED_CONFIG%...
cloudflared tunnel --config "%CLOUDFLARED_CONFIG%" run
pause
exit /b 0

:cloudflared_missing
echo [ERROR] cloudflared not found in PATH.
pause
exit /b 1

:config_missing
echo [ERROR] Missing cloudflared config: %CLOUDFLARED_CONFIG%
pause
exit /b 1
