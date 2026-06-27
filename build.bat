@echo off
REM Build Tallyton.exe   --   run this on Windows with Python 3.9+ installed.
setlocal

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo.
echo Building Tallyton.exe ...
REM --onefile    : single portable .exe
REM --noconsole  : no black console window (it's a GUI/tray app)
REM hidden-imports cover pystray's Win32 backend and PIL's Tk bridge
python -m PyInstaller --onefile --noconsole --name Tallyton ^
  --hidden-import pystray._win32 ^
  --hidden-import PIL._tkinter_finder ^
  tracker.py
if errorlevel 1 goto :error

echo.
echo Done.  Your executable is at:  dist\Tallyton.exe
echo Double-click it, then tick "Start automatically at login" in the window.
goto :eof

:error
echo.
echo Build failed - check the messages above.
exit /b 1
