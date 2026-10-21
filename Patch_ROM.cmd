@echo off
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
  py -3 "%~dp0marl_universal_patcher.py" %*
) else (
  python "%~dp0marl_universal_patcher.py" %*
)
pause
