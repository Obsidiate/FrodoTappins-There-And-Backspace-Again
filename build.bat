@echo off
REM Build FrodoTappins.exe   --   run this on Windows with Python 3.9+ installed.
setlocal

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo.
echo Building FrodoTappins.exe ...
REM --onefile    : single portable .exe
REM --noconsole  : no black console window (it's a GUI/tray app)
REM The GUI is PySide6 (Qt); PyInstaller's bundled Qt hook pulls in what's
REM needed. We exclude the heavy Qt modules the app never touches (WebEngine,
REM multimedia, 3D, QML, etc.) so the single-file exe stays as small as it can.
python -m PyInstaller --onefile --noconsole --name FrodoTappins ^
  --exclude-module PySide6.QtWebEngineCore ^
  --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtWebEngineQuick ^
  --exclude-module PySide6.QtWebChannel ^
  --exclude-module PySide6.QtWebSockets ^
  --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtQuick3D ^
  --exclude-module PySide6.Qt3DCore ^
  --exclude-module PySide6.QtMultimedia ^
  --exclude-module PySide6.QtMultimediaWidgets ^
  --exclude-module PySide6.QtCharts ^
  --exclude-module PySide6.QtDataVisualization ^
  --exclude-module PySide6.QtBluetooth ^
  --exclude-module PySide6.QtSql ^
  --exclude-module PySide6.QtPdf ^
  --exclude-module PySide6.QtNetwork ^
  tracker.py
if errorlevel 1 goto :error

echo.
echo Done.  Your executable is at:  dist\FrodoTappins.exe
echo Double-click it, then tick "Start automatically at login" in the window.
goto :eof

:error
echo.
echo Build failed - check the messages above.
exit /b 1
