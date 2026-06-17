@echo off
setlocal
cd /d "%~dp0"

:: Performance Optimizations for OpenMP (Threading Affinity)
:: This ensures threads stay on the fastest cores and prevents skipping.
set OMP_PROC_BIND=TRUE
set OMP_PLACES=CORES
set OMP_WAIT_POLICY=PASSIVE
set KMP_BLOCKTIME=0

:: Network binding configuration.
:: Keep 127.0.0.1 when exposing through cloudflared.
:: Override by passing host/port as args: start_mcp.bat 192.168.1.50 8765
set "MCP_HOST=%~1"
if "%MCP_HOST%"=="" set "MCP_HOST=127.0.0.1"
set "MCP_PORT=%~2"
if "%MCP_PORT%"=="" set "MCP_PORT=8765"
set "MCP_ENV_FILE=%~dp0.env"

echo.
echo =======================================================
echo   Fish Speech S2 Pro - MCP Server
echo =======================================================
echo Checking environment...

set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%APPDATA%\uv\bin;%LOCALAPPDATA%\uv\bin;%LOCALAPPDATA%\Programs\uv;%PATH%"
set "HF_HOME=%~dp0..\hf-cache"
set "HF_HUB_CACHE=%~dp0..\hf-cache\hub"

if exist "%MCP_ENV_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%MCP_ENV_FILE%") do (
        if /I "%%A"=="FISH_MCP_USERNAME" set "FISH_MCP_USERNAME=%%B"
        if /I "%%A"=="FISH_MCP_PASSWORD" set "FISH_MCP_PASSWORD=%%B"
    )
)

if "%FISH_MCP_USERNAME%"=="" set "FISH_MCP_USERNAME=fish"
if "%FISH_MCP_PASSWORD%"=="" (
    echo Creating local MCP password in .env...
    for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$bytes = [byte[]]::new(24); [Security.Cryptography.RandomNumberGenerator]::Fill($bytes); [Convert]::ToBase64String($bytes)"`) do set "FISH_MCP_PASSWORD=%%P"
    > "%MCP_ENV_FILE%" echo FISH_MCP_USERNAME=%FISH_MCP_USERNAME%
    >> "%MCP_ENV_FILE%" echo FISH_MCP_PASSWORD=%FISH_MCP_PASSWORD%
)

where uv >nul 2>nul
if errorlevel 1 goto uv_missing

echo Checking for Ninja...

where ninja >nul 2>nul
if errorlevel 1 (
    echo [WARNING] Ninja not found in PATH. torch.compile may be slow or fail.
    echo           Run install.bat if you haven't yet, or restart your terminal.
) else (
    echo [OK] Ninja found.
)

echo Starting MCP server with pre-warm...
echo   Binding host: %MCP_HOST%  port: %MCP_PORT%
echo   Local endpoint: http://127.0.0.1:%MCP_PORT%/mcp
echo   Auth username: %FISH_MCP_USERNAME%
echo   Auth password: stored in .env
if not "%MCP_HOST%"=="127.0.0.1" echo   LAN endpoint:   http://^<this-pc-ip^>:%MCP_PORT%/mcp  ^(run ipconfig to find IP^)
echo   Press Ctrl+C to stop.
echo.

if not exist ".venv\Scripts\activate.bat" goto venv_missing
call .venv\Scripts\activate.bat

uv run mcp_server.py --transport streamable-http --host %MCP_HOST% --port %MCP_PORT%
pause
exit /b 0

:uv_missing
echo [ERROR] uv not found in PATH. Run install.bat first, then reopen this terminal.
pause
exit /b 1

:venv_missing
echo [ERROR] Virtual environment .venv not found. Please run install.bat first.
pause
exit /b 1
