# Espartaco

Asistente personal y administrativo para un grupo de Telegram (pensado para una familia). Conversa en español y usa herramientas reales para:

- **Google Calendar**: crear y consultar eventos, con aviso diario de la agenda y recordatorios el día antes y 2 horas antes.
- **Google Tasks**: tareas delegadas con etiquetas (`[Agente]`, `[Persona 1]`, `[Persona 2]`) y agrupación por proyecto.
- **Google Sheets** como base de datos: gastos e ingresos, resúmenes y comparación entre meses, pagos recurrentes con recordatorio de vencimiento, medicamentos y hábitos con botón de confirmación, notas de largo plazo y recordatorios programados a futuro.
- **Internet** (vía [Serper.dev](https://serper.dev)): búsqueda general, negocios locales con contacto y lectura de páginas.
- **Fotos**: lee una imagen (cita, receta, invitación) y ofrece agendarla o crear un recordatorio, siempre con confirmación.

El razonamiento lo hace un modelo de [DeepSeek](https://api-docs.deepseek.com/) por su API compatible con OpenAI, con un loop manual de *function calling* en [brain.py](brain.py).

## Requisitos

- Python 3.12 o superior (desarrollado con 3.14).
- Un bot de Telegram (créalo con [@BotFather](https://t.me/BotFather)) agregado a tu grupo.
- Un proyecto de Google Cloud con las APIs de Calendar, Tasks y Sheets habilitadas y un cliente OAuth de tipo **escritorio**.
- Una API key de DeepSeek y, opcionalmente, una de Serper.dev.

## Instalación

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt      # Windows
# venv/bin/pip install -r requirements.txt        # Linux/macOS
```

1. **Configuración**: copia `.env.example` a `.env` y completa los valores.
2. **Google**: descarga el JSON del cliente OAuth como `credentials.json` en esta carpeta y corre `python google_auth.py` una vez. Se abre el navegador para autorizar y queda un `token.json`.
3. **Contexto de tu familia** (opcional): copia `contexto_familia.example.md` a `contexto_familia.md` y escribe ahí nombres, quién comparte qué gastos, tratamientos, etc. Se agrega al prompt del sistema y no se versiona.
4. **Hoja de cálculo**: crea las pestañas que vayas a usar con estos encabezados en la fila 1:

| Pestaña (variable en `.env`) | Columnas |
|---|---|
| Movimientos (`FINANCE_SHEET_TAB`) | Fecha, Descripcion, Categoria, Monto, Tipo, Registrado por |
| Pagos Recurrentes (`PAGOS_RECURRENTES_TAB`) | ID, Descripcion, Monto, DiaDelMes, Categoria, Responsable, Activo, UltimoMesPagado, UltimoAvisoEnviado, CalendarEventId |
| Medicamentos (`MEDICAMENTOS_TAB`) | ID, Nombre, Hora (HH:MM 24h), Notas, Activo, UltimoAvisoEnviado, UltimaTomaConfirmada |
| Habitos (`HABITOS_TAB`) | ID, Nombre, Hora, Notas, Activo, UltimoAvisoEnviado, UltimaConfirmacion |
| Notas (`NOTAS_TAB`) | Fecha, Categoria, Texto, GuardadoPor, VigenteHasta |
| Iniciativas (`INICIATIVAS_TAB`) | ID, ChatId, Descripcion, TipoAccion, FechaDisparo, Contexto, Estado |

5. **Arranque**: `venv\Scripts\python.exe main.py`. La primera vez, escribe `/start` en el grupo y copia el `chat_id` que aparece en el log a `ALLOWED_CHAT_ID`. **Sin esa variable el bot no responde en ningún chat**, a propósito: así nadie más puede usarlo para leer tu calendario o tus finanzas.

## Arranque automático en Windows (opcional)

`setup_autostart.ps1`, corrido una vez en una consola de PowerShell **como Administrador**, registra dos tareas programadas (al iniciar sesión y cada 5 minutos) que ejecutan `run_watchdog.bat`. El watchdog relanza `main.py` si se cayó o si dejó de escuchar a Telegram, usando el latido que el bot escribe en `latido.json`.

## Respaldo en la nube (opcional)

[apps_script/](apps_script/) tiene dos scripts de Google Apps Script que se pegan en la hoja (Extensiones > Apps Script) para que los recordatorios de medicamentos y de eventos lleguen aunque el PC esté apagado. Las instrucciones están al inicio de cada archivo. Necesitan `TELEGRAM_TOKEN` y `ALLOWED_CHAT_ID` en las propiedades del script.

## Seguridad

- Los secretos viven en `.env`, `credentials.json` y `token.json`, que están en `.gitignore`. No los subas.
- El bot solo atiende el chat de `ALLOWED_CHAT_ID`.
- `leer_pagina` solo abre URLs http/https que apuntan a IPs públicas (también en cada redirección), para que una página maliciosa no pueda usarlo para entrar a tu red local.
- El texto de las páginas web llega al modelo. Las acciones de varios pasos que nacen de una búsqueda pasan por `proponer_plan`, que exige confirmación explícita antes de crear tareas, pero conviene tener presente que el contenido web no es de confianza.

## Licencia

[MIT](LICENSE)
