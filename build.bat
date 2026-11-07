@echo off
cd /d "%~dp0"
echo === Building AdSwitch.exe ===
py -3.12 -m pip install --upgrade pyinstaller winsdk pycaw comtypes customtkinter pystray pillow
if errorlevel 1 goto fail
py -3.12 -m PyInstaller --noconfirm --clean --onefile --windowed --name AdSwitch --collect-all winsdk --collect-all pycaw --collect-all comtypes --collect-all customtkinter --hidden-import pystray._win32 adswitch_gui.py
if errorlevel 1 goto fail
echo.
echo DONE: dist\AdSwitch.exe
echo For an installer: open AdSwitch.iss in Inno Setup and press Compile.
pause
exit /b 0
:fail
echo.
echo BUILD FAILED. Check that Python 3.12 is installed: py -3.12 --version
pause
exit /b 1
