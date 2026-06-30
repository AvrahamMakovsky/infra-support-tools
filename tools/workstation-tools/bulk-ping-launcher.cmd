@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem bulk-ping-launcher.cmd
rem
rem Purpose:
rem   Opens continuous ping sessions for multiple hosts from one pasted list.
rem   Useful for quick connectivity checks during support or lab work.
rem
rem Created by:
rem   Avraham Makovsky
rem
rem License:
rem   MIT
rem
rem Notes:
rem   - Runs from the operator workstation.
rem   - Accepts hostnames or IP addresses separated by lines, spaces, or commas.
rem   - Uses Windows Terminal tabs when available.
rem   - Falls back to separate CMD windows when Windows Terminal is unavailable.

set "TEMP_FILE=%TEMP%\bulk_ping_hosts_%RANDOM%_%RANDOM%.txt"

> "%TEMP_FILE%" (
  echo # Paste hostnames or IP addresses below.
  echo # Separate by new lines, spaces, or commas.
  echo # Lines starting with # are ignored.
  echo.
  echo LAB-PC-001
  echo LAB-PC-002
)

start /wait notepad "%TEMP_FILE%"

set "rawHosts="
for /f "usebackq delims=" %%A in ("%TEMP_FILE%") do (
  set "line=%%A"
  if defined line (
    if not "!line:~0,1!"=="#" (
      set "rawHosts=!rawHosts! !line! "
    )
  )
)

del "%TEMP_FILE%" >nul 2>&1

rem Normalize comma-separated input into space-separated tokens.
set "rawHosts=%rawHosts:,= %"
set /a count=0

for %%A in (%rawHosts%) do (
  set /a count+=1
  set "host!count!=%%A"
)

if "%count%"=="0" (
  echo No hosts provided.
  pause
  exit /b 1
)

where wt.exe >nul 2>nul
if errorlevel 1 goto UseCmdWindows

rem Build one Windows Terminal command with a separate tab for each host.
set "wtCommand=wt -w 0 new-tab --title Ping_1 cmd /k ping -t !host1!"

for /L %%I in (2,1,%count%) do (
  set "currentHost=!host%%I!"
  set "wtCommand=!wtCommand! ; new-tab --title Ping_%%I cmd /k ping -t !currentHost!"
)

start "" %wtCommand%
exit /b 0

:UseCmdWindows
echo Windows Terminal not found. Opening separate CMD windows.

for /L %%I in (1,1,%count%) do (
  set "currentHost=!host%%I!"
  start "Ping_%%I" cmd /k ping -t !currentHost!
)

exit /b 0
