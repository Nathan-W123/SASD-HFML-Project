@echo off
call "%PROGRAMFILES%\ArcGIS\Pro\bin\Python\scripts\propy.bat" -m streamlit run "%~dp0frontend\app.py"
pause
