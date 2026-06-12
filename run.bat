@echo off
REM Windows launcher — double-click this file (no typing needed).
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py) || (set PY=python)

if not exist ".venv" (
  echo First-time setup - creating a private environment...
  %PY% -m venv .venv
)
call .venv\Scripts\activate.bat

echo Checking dependencies...
python -m pip install -q --upgrade pip >nul 2>nul
pip install -q -r requirements.txt

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo   !! FFmpeg was not found on your PATH. Merging needs it.
  echo      Install:  winget install Gyan.FFmpeg   ^(then reopen this window^)
  echo.
)

start "" /b cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8000"

echo.
echo Starting... your browser will open at http://127.0.0.1:8000
echo Keep this window open while you use the app. Close it to stop.
echo.
python main.py
