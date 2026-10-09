@echo off
cd /d "%~dp0"
echo Opening the live council form and dumping its fields to data\form-inspect.txt
echo (Use this if a field stops being filled in - send me the file and I'll fix selectors.json)
.venv\Scripts\python.exe app.py --inspect
pause
