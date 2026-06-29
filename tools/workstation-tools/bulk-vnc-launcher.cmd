@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem bulk-vnc-launcher.cmd
rem
rem Small helper for opening multiple VNC sessions from a pasted host list.
rem Paste hostnames/IPs into Notepad, save, close it, and the script launches
rem one VNC Viewer session per host.
rem
rem Delimiters: spaces, commas, or new lines.
rem Update VNC_PATH below if RealVNC Viewer is installed elsewhere.

set "VNC_PATH=C:\Program Files\RealVNC\VNC Viewer\vncviewer.exe"

if not exist "%VNC_PATH%" (
  echo VNC Viewer was not found at:
  echo %VNC_PATH%
  echo.
  echo Update VNC_PATH inside this script and run it again.
  pause
  exit /b 1
)

set "TEMP_FILE=%TEMP%\bulk_vnc_hosts_%RANDOM%_%RANDOM%.txt"

> "%TEMP_FILE%" (
  echo # Paste VNC hosts below.
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

for /L %%I in (1,1,%count%) do (
  set "currentHost=!host%%I!"
  echo Opening VNC session for: !currentHost!
  start "" "%VNC_PATH%" !currentHost!
)

exit /b 0
