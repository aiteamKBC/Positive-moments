@echo off
REM ---------------------------------------------------------------------------
REM KBC Lecture Intelligence - one scheduled orchestration cycle.
REM
REM Called by Windows Task Scheduler at the hours in SCHEDULER_CRON. It runs
REM ONE cycle and exits; nothing stays resident between firings.
REM
REM Two cycles cannot overlap even if Task Scheduler fires again while this one
REM is still running: the cycle takes a PostgreSQL advisory lock, and a second
REM cycle that cannot take it stands down immediately.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv not found in %CD%
  exit /b 1
)
if not exist "backend\.env" (
  echo ERROR: backend\.env not found in %CD%
  exit /b 1
)

set PYTHONPATH=%CD%
if not exist "logs" mkdir "logs"

echo [%date% %time%] Starting scheduler cycle...
.venv\Scripts\python.exe -m app.cli.main scheduler-cycle --json >> "logs\scheduler-cycle.log" 2>> "logs\scheduler-cycle-error.log"
set RC=%errorlevel%
echo [%date% %time%] Scheduler cycle finished with exit code %RC%
exit /b %RC%
