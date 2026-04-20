@echo off
pushd "%~dp0"
"%PROGRAMFILES%\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" -m pip install streamlit pandas openpyxl pyodbc --quiet
call "%PROGRAMFILES%\ArcGIS\Pro\bin\Python\scripts\propy.bat" -m streamlit run "%~dp0frontend\app.py"
popd
pause
