@echo off
REM Compila el ejecutable en Windows (distribucion en carpeta: dist\AGAsesorCuotas\)
setlocal
cd /d "%~dp0"

if not exist .venv (
    py -3.12 -m venv .venv || py -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error

python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt -r requirements-dev.txt -r requirements-build.txt || goto :error

python -m pytest -q || goto :error
pyinstaller --noconfirm --clean AGAsesorCuotas.spec || goto :error

echo.
echo Ejecutable generado en dist\AGAsesorCuotas\AGAsesorCuotas.exe
exit /b 0

:error
echo.
echo *** La compilacion ha fallado ***
exit /b 1
