@echo off
REM EmiAi restart — stop any running instance (run_flask.py --replace kills it),
REM start fresh, open the browser once the new instance is listening.
if not defined EMI_PORT set EMI_PORT=8000
cd /d "%~dp0"
start "EmiAi" cmd /k ".venv\Scripts\python.exe run_flask.py --replace"
:wait
timeout /t 2 /nobreak >nul
netstat -ano | findstr ":%EMI_PORT% " | findstr "LISTENING" >nul 2>&1
if %errorlevel% neq 0 goto wait
start "" http://localhost:%EMI_PORT%
