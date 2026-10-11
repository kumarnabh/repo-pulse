@echo off
if not exist "%~dp0RepoPulse.exe" (
  echo RepoPulse.exe not found. Run Build.cmd first.
  pause
  exit /b 1
)
start "" "%~dp0RepoPulse.exe"
