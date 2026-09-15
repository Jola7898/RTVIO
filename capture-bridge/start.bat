@echo off
cd /d "%~dp0"
echo Starting Drone Telemetry Tap...
echo (edit config.json first if you haven't set device_ip)
echo.
vendor\node.exe server.js
pause
