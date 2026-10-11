@echo off
setlocal
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0user-entry.ps1" -Action Start
exit /b %errorlevel%
