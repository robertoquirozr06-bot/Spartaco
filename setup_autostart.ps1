<#
Registra el auto-arranque de Espartaco (main.py + watchdog) en el Task
Scheduler de Windows, para que sobrevivan un reinicio del PC sin tener que
abrir nada a mano. Ya NO registra OmniRoute (2026-09-27): se dejo de usar
por completo, ver .env y brain.py.

Correr UNA SOLA VEZ, en una consola de PowerShell como Administrador, desde
la carpeta del proyecto:

    powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1

Las tareas apuntan a la carpeta donde vive este script, asi que si se mueve
el proyecto basta con volver a correrlo. Es seguro repetirlo (usa /f,
sobreescribe las tareas si ya existen).

Las tareas corren run_watchdog_hidden.vbs (que a su vez llama a
run_watchdog.bat) para que no se abra una ventana de consola cada 5 min.
#>

$ErrorActionPreference = "Stop"

$esAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $esAdmin) {
    Write-Host "Este script necesita una consola de PowerShell como Administrador (boton derecho > Ejecutar como administrador)." -ForegroundColor Red
    exit 1
}

$vbs = Join-Path $PSScriptRoot "run_watchdog_hidden.vbs"
$accion = "wscript.exe `"$vbs`""

Write-Host "Registrando tareas en el Task Scheduler (apuntando a $PSScriptRoot)...`n" -ForegroundColor Cyan

schtasks /create /tn "Espartaco-OnLogon" /tr $accion /sc ONLOGON /rl LIMITED /f

schtasks /create /tn "Espartaco-Watchdog-5min" /tr $accion /sc MINUTE /mo 5 /rl LIMITED /f

Write-Host "`nVerificando estado..." -ForegroundColor Cyan
foreach ($tarea in @("Espartaco-OnLogon", "Espartaco-Watchdog-5min")) {
    $linea = schtasks /query /tn $tarea /fo LIST /v | Select-String "^Status:"
    Write-Host ("{0,-28} -> {1}" -f $tarea, $linea.ToString().Replace("Status:", "").Trim())
}

Write-Host "`nListo. Las 2 deberian decir 'Ready'. Espartaco va a arrancar solo en el proximo logon; para probarlo ahora sin reiniciar, corre manualmente:" -ForegroundColor Green
Write-Host '  schtasks /run /tn "Espartaco-OnLogon"'
