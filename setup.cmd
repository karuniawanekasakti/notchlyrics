@echo off
REM one-time setup: local venv + deps. Needs Python 3.12 (winsdk has no 3.14 wheel).
cd /d "%~dp0"
if exist .venv rmdir /s /q .venv

py -3.12 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo Python 3.12 not found. Install it from python.org, then re-run.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo.
  echo Dependency install failed - see the pip output above.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" NotchLyrics.pyw --test
".venv\Scripts\python.exe" test_ui.py
echo.
echo done. Double-click NotchLyrics.pyw to open it.
pause
