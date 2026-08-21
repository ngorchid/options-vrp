@echo off
REM Options VRP deployment tracker - daily (Windows Task Scheduler, weekdays 20:30 CET = mid US
REM session, so chains are live and the observation is "usable"). Uses yfinance chains, NOT the
REM Gateway; places no orders. First COLLECTS one observation, then prints the running summary.
cd /d "%~dp0.."
if not exist "results\paper" mkdir "results\paper"
call .venv\Scripts\activate.bat
echo ===== deployment tracker %DATE% %TIME% ===== >> "results\paper\deployment_tracker.log"
python scripts\track_deployment.py          1>> "results\paper\deployment_tracker.log" 2>&1
python scripts\track_deployment.py --report 1>> "results\paper\deployment_tracker.log" 2>&1
