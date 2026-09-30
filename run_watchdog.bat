@echo off
setlocal EnableExtensions

set "PROJECT_DIR=C:\Users\rober\Spartacus\asistente_ia"
set "PYTHON_EXE=%PROJECT_DIR%\venv\Scripts\python.exe"
set "MAIN_SCRIPT=%PROJECT_DIR%\main.py"
set "LOG_DIR=%PROJECT_DIR%\logs"
set "WATCHDOG_LOG=%LOG_DIR%\watchdog.log"
set "LATIDO=%PROJECT_DIR%\latido.json"

rem Minutos sin latido tras los cuales se da por congelado el proceso. El job
rem heartbeat de main.py escribe cada 5 min. Bajado de 12 a 6 el 2026-09-27
rem (congelamientos frecuentes por Modern Standby/Wi-Fi del equipo): tolera
rem un latido perdido y fuerza el reinicio antes en vez de esperar hasta 12 min.
set "LATIDO_MAX_MIN=6"

rem Margen tras despertar la PC. Mientras el equipo duerme (Modern Standby),
rem main.py esta congelado y no escribe latido, asi que al despertar el latido
rem SIEMPRE parece vencido aunque el proceso este sano. El 2026-09-30 eso hizo
rem que el watchdog matara a Espartaco dos veces justo mientras procesaba un
rem mensaje recien llegado, y esas solicitudes se perdieron. Si la PC desperto
rem hace menos de estos minutos, no se mata: se revisa en la proxima pasada.
rem Se puede sobreescribir desde el entorno (lo usa la prueba end-to-end).
if not defined GRACIA_DESPERTAR_MIN set "GRACIA_DESPERTAR_MIN=6"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

rem OmniRoute ya no forma parte de Espartaco (2026-09-27, ver .env): este
rem watchdog dejo de vigilarlo/reiniciarlo a proposito, no es un descuido.

echo [%date% %time%] Verificando si main.py esta vivo Y escuchando... >> "%WATCHDOG_LOG%"

rem Que el proceso exista NO basta: el polling de Telegram puede haberse caido
rem dejando el proceso corriendo y el bot sordo. Por eso se mira ademas el
rem latido que main.py solo escribe mientras el updater sigue activo.
rem   exit 0 = sano   1 = no corre   2 = sin latido   3 = latido vencido
rem   exit 4 = latido vencido pero la PC acaba de despertar (se da margen)
rem El ultimo despertar sale del registro System: Kernel-Power 507 (salida de
rem Modern Standby) o Power-Troubleshooter 1 (reanudacion de suspension clasica).
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*main.py*' } | Sort-Object CreationDate | Select-Object -First 1; if (-not $p) { exit 1 }; if (((Get-Date) - $p.CreationDate).TotalMinutes -lt %LATIDO_MAX_MIN%) { exit 0 }; if (-not (Test-Path '%LATIDO%')) { exit 2 }; if (((Get-Date) - (Get-Item '%LATIDO%').LastWriteTime).TotalMinutes -le %LATIDO_MAX_MIN%) { exit 0 }; $w = Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=507},@{LogName='System'; ProviderName='Microsoft-Windows-Power-Troubleshooter'; Id=1} -MaxEvents 1 -ErrorAction SilentlyContinue; if ($w -and ((Get-Date) - $w.TimeCreated).TotalMinutes -lt %GRACIA_DESPERTAR_MIN%) { exit 4 }; exit 3"

set "SALUD=%ERRORLEVEL%"

if "%SALUD%"=="0" (
    echo [%date% %time%] main.py activo y escuchando. No se requiere accion. >> "%WATCHDOG_LOG%"
    goto :fin
)

if "%SALUD%"=="4" (
    echo [%date% %time%] Latido vencido pero la PC desperto hace menos de %GRACIA_DESPERTAR_MIN% min - se da margen, sin matar. >> "%WATCHDOG_LOG%"
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
