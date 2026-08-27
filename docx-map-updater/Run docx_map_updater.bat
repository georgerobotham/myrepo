@echo off
"C:\Users\GeorgeRobotham\PyCharmMiscProject\.venv\Scripts\python.exe" "%~dp0docx_map_updater.py"
if errorlevel 1 (
    echo.
    echo Something went wrong - see error above.
    pause
)
