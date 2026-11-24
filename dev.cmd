@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [x] .venv missing. Run: python -m venv .venv
  pause
  exit /b 1
)
if not exist ".env.local" (
  echo [x] .env.local missing
  pause
  exit /b 1
)
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env.local") do set "%%a=%%b"
if not exist "var\uploads" mkdir "var\uploads"
if not exist "var\logs" mkdir "var\logs"
echo ============================================
echo  AiGameApi - local dev server
echo  API docs : http://127.0.0.1:8000/docs
echo  Admin    : http://127.0.0.1:8000/admin
echo  Health   : http://127.0.0.1:8000/health
echo  Ctrl+C to stop
echo ============================================
".venv\Scripts\python.exe" -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
pause
