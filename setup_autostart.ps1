<#
Registra el auto-arranque de Espartaco (OmniRoute + main.py + watchdog) en el
Task Scheduler de Windows, para que sobrevivan un reinicio del PC sin tener
que abrir nada a mano.

Correr UNA SOLA VEZ, en una consola de PowerShell como Administrador:

    powershell -ExecutionPolicy Bypass -File "C:\Users\rober\Spartacus\asistente_ia\setup_autostart.ps1"

Es seguro volver a correrlo despues (usa /f, sobreescribe las tareas si ya existen).
#>

$ErrorActionPreference = "Stop"

$esAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $esAdmin) {
    Write-Host "Este script necesita una consola de PowerShell como Administrador (boton derecho > Ejecutar como administrador)." -ForegroundColor Red
    exit 1
}

Write-Host "Registrando tareas en el Task Scheduler...`n" -ForegroundColor Cyan

schtasks /create /tn "OmniRoute-AutoStart" /tr "`"C:\Users\rober\AppData\Roaming\npm\omniroute.cmd`" serve --daemon --no-open" /sc ONLOGON /rl LIMITED /f

schtasks /create /tn "Espartaco-OnLogon" /tr "`"C:\Users\rober\Spartacus\asistente_ia\run_watchdog.bat`"" /sc ONLOGON /rl LIMITED /f

schtasks /create /tn "Espartaco-Watchdog-5min" /tr "`"C:\Users\rober\Spartacus\asistente_ia\run_watchdog.bat`"" /sc MINUTE /mo 5 /rl LIMITED /f

Write-Host "`nVerificando estado..." -ForegroundColor Cyan
foreach ($tarea in @("OmniRoute-AutoStart", "Espartaco-OnLogon", "Espartaco-Watchdog-5min")) {
    $linea = schtasks /query /tn $tarea /fo LIST /v | Select-String "^Status:"
    Write-Host ("{0,-28} -> {1}" -f $tarea, $linea.ToString().Replace("Status:", "").Trim())
}

Write-Host "`nListo. Las 3 deberian decir 'Ready'. OmniRoute y Espartaco van a arrancar solos en el proximo logon; para probarlos ahora sin reiniciar, corre manualmente:" -ForegroundColor Green
Write-Host '  schtasks /run /tn "OmniRoute-AutoStart"'
Write-Host '  schtasks /run /tn "Espartaco-OnLogon"'
