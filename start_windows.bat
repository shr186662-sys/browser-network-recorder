@echo off
setlocal
cd /d "%~dp0"
if defined NETWORK_RECORDER_PYTHON (
    "%NETWORK_RECORDER_PYTHON%" bootstrap.py
    goto :done
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 bootstrap.py
    goto :done
)
where python >nul 2>nul
if not errorlevel 1 (
    python bootstrap.py
    goto :done
)
echo Install Python 3.11+ from https://www.python.org/downloads/windows/
echo Select "Add python.exe to PATH" and include Tcl/Tk.
pause
exit /b 1
:done
if errorlevel 1 (
    echo Launch failed. Check Python/Tkinter, Chrome and network. See README.md.
    pause
    exit /b 1
)
