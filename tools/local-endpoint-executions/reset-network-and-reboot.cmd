@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem reset-network-and-reboot.cmd
rem
rem Purpose:
rem   Resets local network state and schedules a reboot.
rem
rem Created by:
rem   Avraham Makovsky
rem
rem License:
rem   MIT
rem
rem Warning:
rem   This script is intentionally disruptive.
rem   Run only on a machine you are authorized to maintain.
rem   Network connectivity may drop before all commands finish.

net session >nul 2>&1
if not "%errorlevel%"=="0" (
  echo [ERROR] Run this script as Administrator.
  pause
  exit /b 5
)

set "TS="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss" 2^>nul`) do set "TS=%%I"
if not defined TS set "TS=%RANDOM%_%RANDOM%"

set "LOG=%TEMP%\reset_network_%COMPUTERNAME%_%TS%.log"

call :log "============================================================"
call :log "Network reset started"
call :log "Computer: %COMPUTERNAME%"
call :log "User: %USERNAME%"
call :log "Log: %LOG%"
call :log "============================================================"

echo.
echo This will reset local network state and reboot this machine.
echo Save your work before continuing.
echo.
choice /C YN /M "Continue"
if errorlevel 2 (
  call :log "[INFO] Canceled by user."
  exit /b 0
)

shutdown /r /t 15 /c "Network reset helper scheduled reboot" >> "%LOG%" 2>&1
call :log "[INFO] Reboot scheduled in 15 seconds."

call :run "ipconfig /release"   "Release DHCP lease"
call :run "ipconfig /renew"     "Renew DHCP lease"
call :run "netsh winsock reset" "Reset Winsock catalog"
call :run "ipconfig /flushdns"  "Flush DNS cache"

call :log "[INFO] Commands issued. Machine will reboot shortly."
exit /b 0

:run
set "CMD=%~1"
set "DESC=%~2"
call :log "---- %DESC% ----"
call :log "CMD: %CMD%"
cmd /c %CMD% >> "%LOG%" 2>&1
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  call :log "[WARN] Exit code: %RC%"
) else (
  call :log "[OK] Completed"
)
exit /b 0

:log
echo %~1
>> "%LOG%" echo %~1
exit /b 0
