@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem bulk-ping-launcher.cmd
rem
rem Small helper for opening continuous ping sessions for multiple hosts.
rem Paste hostnames/IPs into Notepad, save, close it, and the script opens
rem one ping session per host.
rem
rem Delimiters: spaces, commas, or new lines.
rem If Windows Terminal is available, it opens tabs. Otherwise, it opens
rem separate CMD windows.

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
