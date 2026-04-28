@echo off
REM EmiAi Desktop Launcher — start server or open browser if already running
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    start "" http://localhost:8000
    exit /b 0
)
cd /d "%~dp0"
start "EmiAi" cmd /k "python start.py"
:wait
timeout /t 2 /nobreak >nul
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if %errorlevel% neq 0 goto wait
start "" http://localhost:8000
