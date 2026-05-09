@echo off
REM EmiAi Desktop Launcher — start server or open browser if already running
if not defined EMI_PORT set EMI_PORT=8000
netstat -ano | findstr ":%EMI_PORT% " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    start "" http://localhost:%EMI_PORT%
    exit /b 0
)
cd /d "%~dp0"
start "EmiAi" cmd /k "python start.py"
:wait
timeout /t 2 /nobreak >nul
netstat -ano | findstr ":%EMI_PORT% " | findstr "LISTENING" >nul 2>&1
if %errorlevel% neq 0 goto wait
start "" http://localhost:%EMI_PORT%
