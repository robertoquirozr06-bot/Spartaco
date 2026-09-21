# Nota — Pagos recurrentes (contexto de la sesión más reciente)

Contexto de continuidad sobre la funcionalidad de "Fase 5" (ver `CLAUDE.md`), con las decisiones y pendientes que salieron después de implementar el código.

## Decisiones tomadas en esta sesión

- **Sheets es la fuente de verdad de las fechas**, Calendar es solo un espejo visual opcional (no bloqueante). Se le preguntó al usuario si prefiere quitar del todo el espejo en Calendar para simplificar — **pendiente de respuesta**.
- **Gastos compartidos de la casa** (Internet, Gas, Agua, Luz, Arriendo) llevan `Responsable = "Roberto y Leidy"` y **no tienen división fija** (ni 50/50 ni ningún porcentaje) — la pareja decide en el momento quién paga cada uno. Espartaco solo recuerda el vencimiento y registra quién confirmó; nunca debe sugerir ni asumir una división. Ya está reflejado en `CLAUDE.md` y en el `SYSTEM_PROMPT` de `brain.py`.
- **Mercado y Bebé NO van en la pestaña "Pagos Recurrentes"** (no tienen fecha fija de vencimiento). Se manejan como categorías de gasto normal con la herramienta ya existente `registrar_movimiento` (ej. "registra un gasto de 150000 en mercado, hoy"), sin necesidad de fila ni configuración nueva.

## Datos de la pestaña "Pagos Recurrentes" — 11 filas, falta el Monto

El usuario ya definió ID/Descripcion/Responsable/DiaDelMes; **falta que confirme el Monto de cada uno** antes de poder darla por completa (sin Monto, el job de recordatorios descarta la fila silenciosamente y no avisa nada):

| ID | Descripcion | Responsable | DiaDelMes | Monto |
|---|---|---|---|---|
| internet | Internet | Roberto y Leidy | 4 | pendiente |
| gas | Gas | Roberto y Leidy | 1 | pendiente |
| agua | Agua | Roberto y Leidy | 6 | pendiente |
| luz | Luz | Roberto y Leidy | 24 | pendiente |
| arriendo | Arriendo | Roberto y Leidy | 6 | pendiente |
| planilla_roberto | Mi planilla Roberto | Roberto | 7 | pendiente |
| planilla_leidy | Mi planilla Leidy | Leidy | 5 | pendiente |
| complementario_roberto | Plan complementario Roberto | Roberto | 7 | pendiente |
| complementario_leidy | Plan complementario Leidy | Leidy | 7 | pendiente |
| celular_roberto | Plan de celular Roberto | Roberto | 15 | pendiente |
| celular_leidy | Plan de celular Leidy | Leidy | 5 | pendiente |

Una vez el usuario dé los montos, cargarlos directo en la columna C de la hoja "Pagos Recurrentes" (columnas D en adelante — Categoria/Activo/UltimoMesPagado/UltimoAvisoEnviado/CalendarEventId — pueden quedar vacías, el código las trata como default).

## Cómo hablarle al bot (referencia rápida)

- **Pago recurrente nuevo**: "Agrega un pago recurrente: id 'netflix', descripción Netflix, monto 40000, vence el día 15" (dar el `id` explícito para que coincida con la convención de la hoja).
- **Gasto no recurrente** (Mercado, Bebé, o cualquier otro): "Registra un gasto de 150000 en mercado, hoy".
- **Confirmar pago del día**: "Ya pagué el arriendo", o tocar el botón "✅ Ya pagué" del aviso.
- **Consultar**: "Muéstrame los pagos recurrentes", "¿Cuánto llevamos gastado en mercado este mes?"
- **Pausar**: "Pausa el pago de Netflix".

## Pendientes generales

1. Usuario confirma montos de las 11 filas (tabla arriba).
2. Usuario decide si se elimina el espejo de Calendar (`_crear_espejo_calendario` en `google_services.py`) o se deja como está.
3. Prueba end-to-end contra el bot real en Telegram una vez la pestaña esté completa (ver checklist de verificación original en la sección "Fase 5" de `CLAUDE.md`).

---

# Nota — Medicamentos de Leidy (Fase 6) y arranque del bot

Contexto de continuidad de la sesión donde se implementó y probó en vivo el recordatorio de medicamentos (ver `CLAUDE.md` para el detalle técnico completo).

## Lo que pasó al probarlo en vivo

- **El bot no estaba corriendo** cuando se probó por primera vez ("consulta en el grupo y no reaccionó"): no era un tema de activar nada en Telegram, `main.py` simplemente estaba caído (ninguna tarea de Task Scheduler está registrada todavía, así que nada lo revive solo). Se arrancó manualmente.
- Al reconectar (`drop_pending_updates=False`), el bot procesó normal el backlog de mensajes acumulados mientras estuvo offline — la reconciliación funcionó: detectó "Registra que Leidy ya tomó los medicamentos de la mañana" y confirmó `eutirox`/`pnatal` en la columna G de la hoja.
- **Bug encontrado en vivo y corregido:** aunque la confirmación en la hoja sí se guardó, el bot fallaba al responder ese mensaje atrasado en el chat (`telegram.error.BadRequest: Message to be replied not found` — intentaba citar un mensaje que Telegram ya no dejaba referenciar). Arreglado en `main.py`: `handle_message` ahora responde con `context.bot.send_message(...)` en vez de `message.reply_text(...)`, para no depender de poder citar el mensaje original (justo el escenario que puede darse al procesar backlog).
- Bot reiniciado con el fix aplicado, arrancó limpio.

## Pendiente

1. **Corregir el formato de la pestaña "Medicamentos"** — el usuario la creó pero solo con 2 columnas (Nombre, Hora en "6:00 AM" 12h). Falta la columna ID al inicio y la Hora debe ir en formato 24h "HH:MM" en la columna C (ver tabla exacta en `CLAUDE.md`). Sin esto, `calcular_recordatorios_medicamentos` descarta las 4 filas ("Hora invalida") y el job no avisa nada, aunque no se cae.
2. **Confirmar la hora del prenatal:** el acuerdo original era 6:20 AM pero el usuario cargó 6:30 AM en la hoja rota — sin confirmar si fue apropósito o un error de dedo.
3. Completar el setup manual de Apps Script (pegar `.gs`, Script Properties, correr `instalarTrigger`) — ver pasos en `CLAUDE.md`.
4. Una vez corregida la hoja: probar el ciclo completo (aviso a la hora + botón "✅ Ya lo tomé" + confirmación en columna G).

---

# Nota — Cuelgue de red, resiliencia y fallback de Apps Script (medicamentos)

Contexto de continuidad de la sesión donde se investigó por qué no avisó el medicamento de las 9pm del 2026-09-05, y se implementó la resiliencia de red + el fallback de Apps Script.

## Diagnóstico del incidente (2026-09-05, 9pm)

Dos causas independientes, cualquiera bastaba:

1. **Formato de la hoja "Medicamentos"** (pendiente #1 de la nota anterior): faltaba columna ID y la Hora estaba en 12h. Se corrigió (ver "Estado final de la hoja" abajo).
2. **`main.py` se congeló ~4h42min** (17:35 a 22:17 hora Bogotá) sin ninguna actividad de log, justo tras una racha de `getaddrinfo failed` (corte de red/DNS). Causa más probable: `google_services.py` construía los clientes de Calendar/Tasks/Sheets con `httplib2.Http()` sin timeout de socket — un socket colgado ocupa para siempre un worker del `ThreadPoolExecutor` compartido de `asyncio.to_thread`, y como el job de medicamentos llama a Sheets cada 5 min, bastaron un par de cuelgues para agotar el pool y congelar TODO el proceso (no solo ese job).

## Cambios de código (resiliencia de red)

- **`google_services.py`**: `_client_apis()` ahora construye Calendar/Tasks/Sheets con `AuthorizedHttp(creds, http=httplib2.Http(timeout=15))` en vez de `httplib2.Http()` sin límite.
- **`brain.py`**: `AsyncOpenAI` (cliente de OmniRoute) ahora usa `timeout=httpx.Timeout(connect=5.0, read=90.0, write=30.0, pool=5.0)` y `max_retries=2` en vez del default de 600s.
- **`main.py`**: los 3 jobs periódicos (`alerta_calendario_diaria`, `recordatorio_pagos_diario`, `recordatorio_medicamentos`) ahora son wrappers con `asyncio.wait_for` (45s/90s/90s respectivamente) sobre su lógica real (renombrada a `_impl`), para que un cuelgue puntual falle con un log claro en vez de bloquear futuras ejecuciones. Se agregó un job `heartbeat` cada 5 min (solo loguea, sin llamadas de red) para detectar al instante un futuro congelamiento.
- Validado en producción: el 2026-09-06 el timeout de socket de Google (15s) y el timeout de job (90s) SÍ se activaron por cuelgues reales puntuales (uno mientras se editaba la hoja en vivo, otro en un `TimeoutError` de lectura real) — en ambos casos el proceso se recuperó solo en el siguiente ciclo de 5 min, en vez de congelarse indefinidamente como el 2026-09-05.

## Estado final de la hoja "Medicamentos" (2026-09-06)

Corregir el formato de Hora tomó varios intentos porque **el formato de celda importa, no solo el texto**: mientras la celda tenga formato Fecha/Hora, Sheets siempre le devuelve a la API una representación humana (con segundos, AM/PM) sin importar lo que se escriba; y cambiar el formato a "Texto sin formato" **no convierte retroactivamente un valor ya guardado** — hay que borrar la celda y volver a escribirla con el formato ya en texto plano.

Valores finales confirmados (columna C, formato Texto sin formato):
| ID | Hora |
|---|---|
| EUTIROX | 06:00 |
| pnatal | 06:05 |
| caltrate plus | 21:00 |
| Acido Folico | 21:05 |

**Ojo con 24h vs 12h:** escribir `9:00` para un medicamento de la noche lo manda a las 9am, no 9pm — hay que escribir `21:00`. Esto realmente pasó: durante el ajuste, caltrate/folico quedaron unas horas con `9:00`/`9:05`, el bot los detectó como pendientes (ya pasadas las 9am) y mandó avisos reales + marcó columna F (UltimoAvisoEnviado) con la fecha de hoy — lo cual iba a bloquear el aviso real de las 9pm de esa misma noche. Se corrigió borrando F4/F5 a mano. **Recordatorio para el futuro:** si se edita la Hora de un medicamento y ya pasó esa hora "mal interpretada", revisar/limpiar la columna F de esa fila para no perder el aviso real del día.

Aclaración sobre columna F que surgió en esta sesión: es solo un flag de un solo día (`si columna F == fecha de hoy, ya se avisó`), se sobreescribe solo cada día — no se acumula ni requiere limpieza manual en operación normal.

## Fallback de Apps Script — completado

Se pegó `apps_script/RecordatorioMedicamentos.gs`, se configuraron las Script Properties (`TELEGRAM_TOKEN`/`ALLOWED_CHAT_ID`, copiados tal cual del `.env`) y se ejecutó `instalarTrigger` (aceptando la pantalla de "Google hasn't verified this app" — normal para un script propio sin publicar). Trigger de 5 min instalado y autorizado.

## Pendiente

1. **Validar con la PC realmente apagada**: todavía no se ha probado apagar `main.py`/la PC durante la hora real de un medicamento y confirmar que el aviso llega solo por Apps Script. Es la prueba de fondo que falta para dar por cerrado el fallback.
2. **Revisar en el editor de Apps Script** (ícono de reloj "Disparadores"/"Triggers") que el trigger de `revisarMedicamentos` siga listado como "Basado en tiempo, cada 5 minutos" — no hay forma de verificar esto por API/CLI desde aquí (no hay `clasp` instalado ni permisos de Apps Script API en el `token.json` actual).
3. **Limitación conocida, no bloqueante:** `main.py` y Apps Script comparten la columna F como "mutex" de un solo día; si ambos llegan a estar activos justo en el mismo instante (ej. la PC prendiendo/apagando justo en la ventana de un aviso), en teoría podría duplicarse un mensaje. Preferible a perder el aviso, no se ha mitigado más allá de eso.
4. **Task Scheduler / autoarranque de `main.py`/OmniRoute sigue pendiente** (pospuesto a propósito) — ya NO es necesario para que los recordatorios de medicamentos funcionen con la PC apagada (eso ya lo cubre Apps Script), pero sigue haciendo falta para que el resto de Espartaco (chat, calendario, pagos, tareas) arranque solo. Se retomará cuando se defina la nueva arquitectura 24/7.

---

# Nota — Fase 7 (agente real: buscar en internet + plan con freno) — falta configurar la API de búsqueda

Contexto de continuidad: se implementó `web_tools.py` (`buscar_en_internet`, `buscar_negocios_locales`, `leer_pagina`) y el freno de planes multi-paso (`proponer_plan`/`confirmar_plan`/`cancelar_plan` en `google_services.py`) — ver detalle técnico completo en `CLAUDE.md` (sección "Fase 7"). Falta un solo paso para que quede operativo.

## Pendiente del usuario

**Falta `SERPER_API_KEY` en `.env`** (queda vacía a propósito). Sin ella, `buscar_en_internet`/`buscar_negocios_locales`/`leer_pagina` responden avisando que falta configurar, en vez de fallar feo — el resto del bot (calendario, tareas, pagos, medicamentos) no se ve afectado. El usuario va a crear la cuenta en serper.dev y pegar la key cuando tenga tiempo ("después lo ajustamos"); no es bloqueante para el resto de Espartaco.

## Decisión tomada en esta sesión: se evaluó y descartó reemplazar Serper por un "MCP de Google"

El usuario preguntó si en vez de pagar por Serper se podía usar el MCP de Google (el mismo tipo de conexión que ya se ve en esta sesión de Claude Code, ej. el MCP de Google Drive). Se descartó por dos razones, no una sola:

1. **MCP no es una fuente de búsqueda gratis** — es solo el protocolo de transporte. Cualquier MCP server de búsqueda (de Google o de otro) igual necesita por debajo una API key real de alguien (Google Custom Search, Bing, Brave, etc.), con su propio costo/cuota. No existe un "buscar en Google gratis sin key" que no sea scrapear el HTML de resultados directo, lo cual viola los términos de Google y se cae con CAPTCHAs — no es viable para un bot en producción.
2. **Espartaco no es un cliente MCP.** El MCP de Google Drive disponible ahora es de esta sesión de Claude Code, no de Espartaco. Espartaco es un proceso Python aparte (`main.py`/`brain.py`) que llama a Groq directo vía OmniRoute con un loop manual de function-calling — conectar un MCP ahí requeriría agregar una librería cliente MCP y adaptar el loop, bastante más código que las llamadas directas por `httpx` que ya están en `web_tools.py`.

**Alternativa real si el costo llega a preocupar más adelante:** Google Custom Search JSON API (oficial, 100 búsquedas/día gratis para siempre, sin tarjeta). Cubriría `buscar_en_internet` (búsqueda web genérica, ej. precios), pero no tiene datos de negocios locales (teléfono/dirección) como el endpoint `/places` de Serper — `buscar_negocios_locales` perdería precisión con ese cambio (el carpintero del ejemplo se buscaría por resultados web genéricos en vez de listado de Google Maps).

**Nota técnica para cuando se retome:** `web_tools.py` ya quedó desacoplado del proveedor (las funciones solo devuelven texto plano), así que migrar de Serper a Google CSE más adelante es un cambio aislado a ese archivo, sin tocar `brain.py`.

## Próximo paso

Usuario consigue la key de serper.dev y la pega en `.env` cuando pueda. Ahí se prueba en vivo el flujo completo (precio de medicamento + cotización de carpintero con confirmación, ver checklist en `CLAUDE.md`), y se revisa si vale la pena migrar a Google CSE una vez se vea el consumo real.
