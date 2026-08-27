@echo off
"C:\Users\GeorgeRobotham\PyCharmMiscProject\.venv\Scripts\python.exe" "%~dp0webapp.py"
if errorlevel 1 (
    echo.
    echo Something went wrong - see error above.
    pause
)
