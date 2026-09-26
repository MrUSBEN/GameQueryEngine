@echo off
cd /d "%~dp0"
title Game Query Engine (live log; close this window to stop)
where py >nul 2>nul && (py -3 -m gqe & goto :done)
where python >nul 2>nul && (python -m gqe & goto :done)
echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/ then run this again.
:done
echo.
echo Game Query Engine has stopped. Any error above tells you why. Press any key to close.
pause >nul
