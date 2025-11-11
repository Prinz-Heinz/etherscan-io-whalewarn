@echo off
cd /d "%~dp0"

rem Run monitor.py again in 15 seconds if connection lost or script dies
:loop

python monitor.py

ping localhost -n 15 > nul

goto :loop

exit