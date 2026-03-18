@echo off
title Disasteroids NetLink Bridge
echo ============================================
echo   Disasteroids NetLink Bridge for Windows
echo ============================================
echo.

REM --- Configuration ---
set SERVER=saturncoup.duckdns.org:4822
set SECRET=SaturnDisasteroids2026!NetLink#Key
set BAUD=9600

REM --- Check Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3 from https://python.org
    echo Make sure "Add Python to PATH" is checked during install.
    pause
    exit /b 1
)

REM --- Check pyserial ---
python -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo Installing pyserial...
    pip install pyserial
    echo.
)

REM --- List available COM ports ---
echo Scanning for serial ports...
echo.

python -c "from serial.tools.list_ports import comports; ports=list(comports); [print(f'  {i+1}) {p.device}  -  {p.description}') for i,p in enumerate(ports)]; print() if ports else print('  No serial ports found!')" 2>nul
if errorlevel 1 (
    echo ERROR: Could not list serial ports.
    pause
    exit /b 1
)

echo.
set /p CHOICE="Enter the number of your modem's COM port (or type COM port name): "

REM --- Check if user entered a number or a COM port name ---
echo %CHOICE%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 (
    REM User typed a port name directly (e.g. COM3)
    set COMPORT=%CHOICE%
) else (
    REM User typed a number — resolve it to the port name
    for /f "delims=" %%P in ('python -c "from serial.tools.list_ports import comports; ports=list(comports()); print(ports[%CHOICE%-1].device if %CHOICE%>0 and %CHOICE%<=len(ports) else 'INVALID')"') do set COMPORT=%%P
)

if "%COMPORT%"=="INVALID" (
    echo ERROR: Invalid selection.
    pause
    exit /b 1
)

echo.
echo Using port: %COMPORT%
echo Server:     %SERVER%
echo.
echo Starting bridge... (Press Ctrl+C to stop)
echo ============================================
echo.

python "%~dp0bridge.py" --serial-port %COMPORT% --server %SERVER% --secret "%SECRET%" --baud %BAUD%

echo.
echo Bridge stopped.
pause
