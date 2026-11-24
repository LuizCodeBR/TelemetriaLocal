@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" goto ambiente
if defined TELEMETRIA_PYTHON goto escolhido
where py >nul 2>nul
if errorlevel 1 goto procurar
py -3.12 -V >nul 2>nul
if not errorlevel 1 goto py312
py -3.13 -V >nul 2>nul
if not errorlevel 1 goto py313
:procurar
if exist "%~d0\Ferramentas\Python\uv-managed\cpython-3.12-windows-x86_64-none\python.exe" goto portatil
where python >nul 2>nul
if not errorlevel 1 goto python_path
echo Instale Python 3.12 ou 3.13 ou defina TELEMETRIA_PYTHON com o caminho do executavel.
exit /b 2
:ambiente
"%~dp0.venv\Scripts\python.exe" -B "%~dp0rodar.py" %*
exit /b %errorlevel%
:escolhido
"%TELEMETRIA_PYTHON%" -B "%~dp0rodar.py" %*
exit /b %errorlevel%
:py312
py -3.12 -B "%~dp0rodar.py" %*
exit /b %errorlevel%
:py313
py -3.13 -B "%~dp0rodar.py" %*
exit /b %errorlevel%
:portatil
"%~d0\Ferramentas\Python\uv-managed\cpython-3.12-windows-x86_64-none\python.exe" -B "%~dp0rodar.py" %*
exit /b %errorlevel%
:python_path
python -B "%~dp0rodar.py" %*
exit /b %errorlevel%
