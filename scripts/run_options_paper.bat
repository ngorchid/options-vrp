@echo off
REM Options VRP paper trading - daily run (Windows Task Scheduler, weekdays 21:30 CET).
REM Runs from the repo root regardless of where it's invoked; appends output to a log.
cd /d "%~dp0.."
if not exist "results\paper" mkdir "results\paper"
call .venv\Scripts\activate.bat
python scripts\run_options_paper.py --live %* 1>> "results\paper\run.log" 2>&1
