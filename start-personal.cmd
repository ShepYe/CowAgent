@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-personal.ps1" %*
exit /b %errorlevel%
