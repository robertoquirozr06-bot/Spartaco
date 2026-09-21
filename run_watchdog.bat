@echo off
setlocal EnableExtensions

set "PROJECT_DIR=C:\Users\rober\Spartacus\asistente_ia"
set "PYTHON_EXE=%PROJECT_DIR%\venv\Scripts\python.exe"
set "MAIN_SCRIPT=%PROJECT_DIR%\main.py"
set "LOG_DIR=%PROJECT_DIR%\logs"
set "WATCHDOG_LOG=%LOG_DIR%\watchdog.log"
set "OMNIROUTE_CMD=C:\Users\rober\AppData\Roaming\npm\omniroute.cmd"
set "LATIDO=%PROJECT_DIR%\latido.json"

rem Minutos sin latido tras los cuales se da por congelado el proceso. El job
rem heartbeat de main.py escribe cada 5 min, asi que 12 tolera dos latidos
rem perdidos antes de actuar.
set "LATIDO_MAX_MIN=12"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo [%date% %time%] Verificando si OmniRoute esta en ejecucion... >> "%WATCHDOG_LOG%"

powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -like '*omniroute*' }; if ($p) { exit 0 } else { exit 1 }"

if %ERRORLEVEL% EQU 0 (
    echo [%date% %time%] OmniRoute sigue activo. No se requiere accion. >> "%WATCHDOG_LOG%"
) else (
    echo [%date% %time%] OmniRoute NO esta activo. Reiniciando... >> "%WATCHDOG_LOG%"
    call "%OMNIROUTE_CMD%" serve --daemon --no-open >> "%LOG_DIR%\omniroute_stdout.log" 2>> "%LOG_DIR%\omniroute_stderr.log"
    echo [%date% %time%] Comando de reinicio de OmniRoute enviado. >> "%WATCHDOG_LOG%"
)

echo [%date% %time%] Verificando si main.py esta vivo Y escuchando... >> "%WATCHDOG_LOG%"

rem Que el proceso exista NO basta: el polling de Telegram puede haberse caido
rem dejando el proceso corriendo y el bot sordo. Por eso se mira ademas el
rem latido que main.py solo escribe mientras el updater sigue activo.
rem   exit 0 = sano   1 = no corre   2 = sin latido   3 = latido vencido
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*main.py*' } | Sort-Object CreationDate | Select-Object -First 1; if (-not $p) { exit 1 }; if (((Get-Date) - $p.CreationDate).TotalMinutes -lt %LATIDO_MAX_MIN%) { exit 0 }; if (-not (Test-Path '%LATIDO%')) { exit 2 }; if (((Get-Date) - (Get-Item '%LATIDO%').LastWriteTime).TotalMinutes -gt %LATIDO_MAX_MIN%) { exit 3 }; exit 0"

set "SALUD=%ERRORLEVEL%"

if "%SALUD%"=="0" (
    echo [%date% %time%] main.py activo y escuchando. No se requiere accion. >> "%WATCHDOG_LOG%"
    goto :fin
)

rem SALUD 2 o 3: el proceso existe pero esta congelado o sordo. Hay que matarlo
rem primero, porque si no el "start" de abajo dejaria dos instancias peleandose
rem el polling -- exactamente el Conflict que causa este estado.
if %SALUD% GEQ 2 (
    echo [%date% %time%] main.py vivo pero SIN latido valido, codigo %SALUD% - proceso sordo o congelado. Terminandolo... >> "%WATCHDOG_LOG%"
    powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
) else (
    echo [%date% %time%] main.py NO esta activo. >> "%WATCHDOG_LOG%"
)

echo [%date% %time%] Reiniciando main.py... >> "%WATCHDOG_LOG%"
cd /d "%PROJECT_DIR%"
start "Espartaco" /B "%PYTHON_EXE%" -u "%MAIN_SCRIPT%" >> "%LOG_DIR%\main_stdout.log" 2>> "%LOG_DIR%\main_stderr.log"
echo [%date% %time%] Comando de reinicio enviado. >> "%WATCHDOG_LOG%"

:fin
endlocal
