@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
  echo ERROR: .env not found in %~dp0
  echo Copy .env.example to .env and set KBC_WORKER_SECRET first.
  exit /b 1
)

echo [%date% %time%] Checking Docker...
set /a tries=0
:docker_check
docker info >nul 2>&1
if %errorlevel%==0 goto docker_ready
set /a tries+=1
if %tries% GEQ 12 (
  echo Docker is not ready after 2 minutes. Exiting; Task Scheduler can retry later.
  exit /b 1
)
timeout /t 10 /nobreak >nul
goto docker_check

:docker_ready
echo Docker is ready. Starting KBC Media Worker...
docker compose up -d --build media-worker
if %errorlevel% NEQ 0 exit /b %errorlevel%

docker compose ps media-worker
endlocal
