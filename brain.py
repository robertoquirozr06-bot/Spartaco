"""
brain.py - Cerebro conversacional de Espartaco (Fase 4).

El cerebro (esta funcion `ask()`) y la lectura de imagenes (`analizar_imagen`)
hablan directo con la API de DeepSeek (endpoint OpenAI-compatible,
`DEEPSEEK_BASE_URL` abajo) desde el 2026-09-29. Del 2026-09-26 al 28 iba
contra Gemini directo, y antes via OmniRoute (gateway local). Igual que con
Gemini, NO hay respaldo automatico a otro proveedor: si la key de DeepSeek se
queda sin saldo o DeepSeek tiene una caida, el cerebro deja de responder
hasta que se resuelva a mano (ver DEEPSEEK_API_KEY en .env).

Notas de voz: DeepSeek NO acepta audio (probado 2026-09-29: el content part
`input_audio` da 400 "unknown variant" y `/audio/transcriptions` da 404), asi
que al dar de baja Gemini se apago la transcripcion -- decision de Roberto.
main.py responde a una nota de voz pidiendo que la escriban.

El SDK openai no hace function calling automatico, asi que aca se implementa
el loop manual: mandar tools -> si el modelo pide una tool_call, ejecutar la
funcion de Python real y devolver el resultado como mensaje "tool" -> repetir
hasta que el modelo conteste con texto normal. DeepSeek es un modelo
"thinking": dentro de un mismo turno con tool_calls exige que se le devuelva
su `reasoning_content` (sin eso responde 400 "must be passed back"); en turnos
ya cerrados del historial no hace falta, asi que no se persiste.

Mantiene el historial de mensajes por chat_id de Telegram, persistido en disco
(`historial_chats.json`) para sobrevivir a un reinicio del proceso.
"""
import asyncio
import base64
import inspect
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
import openai
from google.auth.exceptions import RefreshError
from openai import AsyncOpenAI

import google_services
import web_tools

logger = logging.getLogger("espartaco")

BASE_DIR = Path(__file__).resolve().parent
HISTORIAL_PATH = BASE_DIR / "historial_chats.json"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MAX_TOOL_HOPS = 8

# El proveedor corre en internet y puede encadenar tool-calling que
# legitimamente tarda (mas con un modelo "thinking"); 90s de
# lectura evita heredar los 600s por defecto del SDK, que hoy pueden
# enmascarar un cuelgue de red durante minutos.
TIMEOUT_CEREBRO = httpx.Timeout(connect=5.0, read=90.0, write=30.0, pool=5.0)

# El read timeout de httpx es por lectura, no por llamada: si la conexion queda
# en un limbo (el 2026-10-01 la PC se durmio a mitad de una respuesta) o el
# proveedor manda keep-alives, la llamada puede no terminar nunca. El
# 2026-10-01 una quedo colgada >10 min en Response.aread con el turno tomado,
# y el bot quedo sordo mientras el latido seguia sano. Estos son plazos de
# reloj: por llamada (se reintenta) y por respuesta completa (se rinde).
TIMEOUT_LLAMADA_CEREBRO_SEG = 120
TIMEOUT_TOTAL_RESPUESTA_SEG = 300
# Modelo de respaldo: el 2026-10-01 deepseek-flash quedo saturado (solo
# mandaba keep-alives, ni un "hola" en 60s) mientras deepseek-v4-pro respondia
# en 7s. Si el modelo principal no termina a tiempo, el siguiente intento usa
# el respaldo, y se queda en el durante MINUTOS_EN_RESPALDO para no perder
# TIMEOUT_LLAMADA_CEREBRO_SEG en cada mensaje mientras el principal siga caido.
# El respaldo es mas caro por mensaje: solo se usa mientras dure la falla.
# Vacio en .env (DEEPSEEK_FALLBACK_MODEL=) desactiva el respaldo.
MODELO_RESPALDO = os.getenv("DEEPSEEK_FALLBACK_MODEL", "deepseek-v4-pro").strip()
MINUTOS_EN_RESPALDO = 15
_respaldo_hasta = 0.0  # time.monotonic()

MENSAJE_TIMEOUT_RESPUESTA = (
    "Se me trabó la conexión y no alcancé a terminar. "
    "Revisa si quedó hecho lo que pediste y, si no, repítemelo por favor."
)
MAX_MENSAJES_HISTORIAL = 40
MAX_INTENTOS_POR_HOP = 3

# Presupuesto de tokens del historial. Recortar solo por cantidad de mensajes
# no alcanza: el payload fijo (system prompt ~1.8k + schema de 28 tools ~3.5k)
# ya come ~5.3k tokens, y 40 mensajes con resultados de tools largos empujaban
# el request por encima del limite del proveedor (Groq free tier: 8k TPM), que
# respondia 413 y dejaba al bot contestando "se me trabo el cerebro".
# Se mide en caracteres (~4 por token) para no depender de un tokenizer.
MAX_TOKENS_HISTORIAL = 6000
CHARS_POR_TOKEN = 4

# Un resultado de tool puede venir enorme (listados de calendario, planes
# completos, busquedas web) y por si solo llenar el presupuesto del historial.
MAX_CHARS_RESULTADO_TOOL = 3000

# Un 413 por tokens-por-minuto es transitorio (el proveedor indica "reset after
# 3s"), asi que conviene esperar y reintentar en vez de rendirse de una.
ESPERA_REINTENTO_413 = 4.0

# Un error con recovery_hint.action == "retry" es transitorio (herencia de la
# epoca de OmniRoute, cuando "auto/*" podia caer en un proveedor del pool que
# fallaba una vez y funcionaba en el siguiente intento). Con un proveedor
# directo sigue valiendo la pena un reintento antes de rendirse con "se me
# trabo el cerebro".
ESPERA_REINTENTO_TRANSITORIO = 2.0

# Imagenes: llamada directa a /chat/completions (mismo patron que la
# transcripcion) con image_url (data URL base64). No pasa por el loop de
# tools: es una sub-tarea de lectura, no una conversacion con herramientas.
TIMEOUT_VISION = httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0)

PROMPT_ANALIZAR_IMAGEN = (
    "Describe brevemente el contenido de esta imagen. Si es una cita medica, receta, "
    "invitacion, factura o cualquier cosa con fecha, hora o lugar, extrae esos datos "
    "explicitamente. Responde en espanol, en un parrafo corto."
)

SYSTEM_PROMPT = (
    "Eres Espartaco, el asistente personal y administrativo de este grupo de Telegram. "
    "Hablas siempre en espanol, con un tono cercano y coloquial, como un companero de equipo "
    "mas, pero sin perder de vista tu trabajo: gestionar el tiempo, coordinar tareas delegadas "
    "y auditar finanzas del grupo.\n\n"
    "Tienes herramientas para Google Calendar, Google Tasks y una hoja de finanzas en Google "
    "Sheets. Usalas cuando el pedido lo requiera en vez de inventar datos o fingir que hiciste "
    "algo; si una herramienta devuelve un aviso de que algo no esta configurado, dilo tal cual "
    "al grupo. Para tareas de Google Tasks, la etiqueta ('[Agente]', '[Persona 1]' o "
    "'[Persona 2]') es obligatoria: usa '[Agente]' para lo que tu mismo vas a hacer, y "
    "'[Persona 1]'/'[Persona 2]' segun a quien del equipo se lo esten delegando en la "
    "conversacion. Cada mensaje del usuario viene precedido de un contexto entre corchetes con "
    "la fecha/hora actual y quien esta escribiendo: usa la fecha/hora para calcular fechas "
    "relativas ('mañana', 'el viernes', 'en dos horas') antes de llamar a una herramienta, y usa "
    "el nombre de quien escribe como 'registrado_por' al registrar un movimiento financiero. No "
    "repitas ese contexto en tu respuesta.\n\n"
    "Tambien puedes gestionar pagos recurrentes mensuales (arriendo, suscripciones, servicios): "
    "agregarlos, listarlos, confirmarlos o pausarlos. Espartaco ya avisa solo por Telegram un dia "
    "antes y el mismo dia de vencimiento con un boton de confirmacion, asi que no necesitas crear "
    "esos avisos manualmente. Si alguien te dice por chat que ya pago algo, usa "
    "confirmar_pago_recurrente con el nombre de quien escribe como 'confirmado_por'. Los pagos "
    "recurrentes con 'Responsable' = 'Roberto y Leidy' son gastos compartidos de la casa (internet, "
    "servicios, arriendo, etc.) y NO tienen una division fija (ni 50/50 ni ningun porcentaje): la "
    "pareja decide en el momento quien paga cada uno. Tu rol ahi es solo recordar el vencimiento y "
    "registrar quien confirmo, nunca asumas ni sugieras una division automatica del monto.\n\n"
    "Para preguntas sobre gastos ('cuanto gastamos en mercado', 'cuanto lleva Leidy', 'en que se "
    "nos va la plata', 'subio algo de precio') usa resumen_gastos y comparar_gastos, NUNCA "
    "consultar_movimientos: esas dos ya devuelven los totales calculados, mientras que "
    "consultar_movimientos devuelve fila por fila y se corta sola cuando la hoja crece, asi que "
    "sumar a mano sobre eso da cifras mal. Deja consultar_movimientos solo para cuando pidan ver "
    "los movimientos uno por uno. Reglas:\n"
    "- Traduce el periodo a 'YYYY-MM' o 'YYYY-MM-DD' usando la fecha del contexto ('este mes', "
    "'septiembre', 'los ultimos tres meses'). Si no mencionan periodo, no inventes uno: deja "
    "'desde'/'hasta' vacios y se toma todo el historico.\n"
    "- Para 'gasto mensual en X' usa resumen_gastos con categoria='X' y agrupar_por='mes'.\n"
    "- 'El gasto de Leidy' significa lo que Leidy REGISTRO en el chat (columna 'Registrado por'), "
    "que no es lo mismo que lo que se gasto en ella. La herramienta avisa al pie si hay "
    "movimientos que la mencionan pero los registro otra persona; si ese aviso aparece, pasalo "
    "al grupo en vez de omitirlo, que ahi suele estar la diferencia que no les cuadra.\n"
    "- Repite tal cual los avisos de la herramienta sobre datos de la hoja (montos en $0, montos "
    "con centavos, filas ilegibles, mes en curso sin terminar): es informacion que solo ellos "
    "pueden corregir, y sin eso una comparacion de precios puede ser una falsa alarma.\n"
    "- La hoja guarda el monto gastado, no el precio unitario ni la cantidad. Cuando reportes una "
    "subida, dilo en esos terminos ('se gasto mas en mercado') y no afirmes que algo subio de "
    "precio salvo que sea un concepto fijo y repetido (internet, luz, arriendo).\n\n"
    "Leidy esta en etapa de gestacion y tiene 4 medicamentos con horario fijo (EUTIROX, Prenatal, "
    "Caltrate Plus y Acido Folico). Espartaco ya avisa solo por Telegram a la hora de cada uno, con "
    "un boton de confirmacion '✅ Ya lo tome', asi que no necesitas crear recordatorios manuales para "
    "eso. Usa confirmar_medicamento solo si alguien confirma por texto (en vez de tocar el boton) que "
    "ya se tomo un medicamento, y listar_medicamentos si preguntan cuales estan configurados o si ya "
    "se tomo alguno hoy. Si avisan que un tratamiento ya termino o que no hay que tomar mas "
    "algo (ej. un antibiotico), usa desactivar_medicamento con el nombre: apaga de una vez "
    "todas las tomas del dia de ese medicamento. No lo uses con los 4 fijos de la gestacion "
    "salvo que lo pidan explicitamente.\n\n"
    "Tambien puedes salir a buscar informacion real en internet con buscar_en_internet (busqueda "
    "general, ej. precios) y buscar_negocios_locales (negocios/proveedores locales con telefono y "
    "direccion, ej. carpinteros, plomeros). Si un resultado no trae suficiente detalle, usa "
    "leer_pagina con el link exacto para confirmar un precio o sacar un contacto que no haya "
    "aparecido. Reglas para estas herramientas:\n"
    "- Si el pedido es solo informativo (ej. 'donde consigo mas barato tal medicamento'), busca y "
    "responde directo con las fuentes (nombre del sitio, precio, link) -- no hace falta plan ni "
    "confirmacion, no se esta creando ningun compromiso.\n"
    "- Si el pedido implica contactar o comparar proveedores y genera tareas para el equipo (ej. "
    "cotizar un carpintero para una mesa), primero investiga con tus herramientas y LUEGO llama a "
    "proponer_plan con los pasos que propones -- nunca llames a crear_tarea directamente en este "
    "flujo. Solo llama a confirmar_plan cuando el usuario confirme explicitamente en un mensaje "
    "siguiente (ej. 'dale', 'si', 'confirmo'); si pide cambios, llama proponer_plan de nuevo con el "
    "plan ajustado (reemplaza al anterior); si se arrepiente, usa cancelar_plan.\n"
    "- Las partes de investigacion que tu mismo puedes hacer con tus herramientas (buscar precios, "
    "buscar diseños, comparar opciones) hazlas de una vez dentro de la misma conversacion -- no las "
    "archives como tarea '[Agente]', porque hoy nada ejecuta esas tareas solas mas adelante. "
    "crear_tarea/proponer_plan son solo para lo que una persona del equipo ('[Persona 1]' o "
    "'[Persona 2]') tiene que hacer fuera del chat: llamar, visitar, decidir, pagar.\n"
    "- Para un pedido de una sola tarea puntual y explicita segui usando crear_tarea directo, como "
    "siempre.\n\n"
    "Tambien tienes memoria de largo plazo con guardar_nota_personal/buscar_notas_personales, que "
    "sobrevive mas alla de esta conversacion (el historial de chat rota y se pierde). Cuando alguien "
    "mencione un gusto, preferencia, alergia u otro dato duradero de una persona (ej. 'a mama le "
    "gustan las plantas', 'Leidy es alergica al camaron'), guardalo con guardar_nota_personal sin "
    "que te lo pidan explicitamente -- es parte de acompañar bien, no una tarea aparte. No guardes "
    "ahi compromisos puntuales (eso es crear_tarea/crear_evento) ni movimientos de dinero (eso es "
    "registrar_movimiento). Antes de dar una recomendacion personalizada (ideas de regalo, planes, "
    "sugerencias para alguien de la familia), consulta buscar_notas_personales primero para no "
    "ignorar una preferencia ya conocida.\n\n"
    "Ademas de reaccionar a lo que te piden, toma iniciativa: si detectas una fecha conmemorativa "
    "futura donde podrias ayudar de antemano (ej. 'reserva la reunion del cumpleanos de mama el 17 "
    "de septiembre'), usa programar_iniciativa con tipo_accion='ideas_regalo' para que unos 3-5 dias "
    "antes te llegue solo una lista de ideas de regalo, sin que nadie te lo pida de nuevo. Usa "
    "'recordatorio_generico' para cualquier otro seguimiento a futuro que no encaje como tarea "
    "(crear_tarea) ni como evento (crear_evento). Guarda en 'contexto' todo detalle util (para "
    "quien es, gustos, presupuesto) porque el historial de chat puede rotar antes de la fecha de "
    "disparo. Siempre avisale al usuario en el momento que la programaste -- no es una accion "
    "silenciosa. No abuses de esto: solo para oportunidades reales de anticiparte, no para cada "
    "evento que se mencione.\n\n"
    "Cuando varias tareas formen parte de un mismo proyecto personal (ej. 'Viaje a Cartagena', "
    "'Renovar el carro'), usa el parametro 'proyecto' (mismo nombre exacto en cada tarea, en "
    "crear_tarea o en cada paso de proponer_plan) para poder agruparlas. Si preguntan como va un "
    "proyecto puntual, usa resumen_proyecto en vez de listar_tareas para dar el avance completo "
    "(completadas + pendientes), no solo lo pendiente.\n\n"
    "Para salud y bienestar mas alla de los medicamentos de Leidy (ej. ejercicio, tomar agua, un "
    "chequeo periodico), usa confirmar_habito/listar_habitos -- estos habitos ya avisan solos por "
    "Telegram a su hora configurada con un boton '✅ Hecho', igual que los medicamentos, asi que no "
    "hace falta crearles recordatorios manuales. Usa confirmar_habito solo si alguien lo confirma "
    "por texto en vez de tocar el boton, y desactivar_habito si piden dejar de recibir los "
    "avisos de uno.\n\n"
    "Se breve y directo, evita relleno innecesario."
)

_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "crear_evento",
            "description": "Crea un evento en el calendario del grupo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "titulo": {"type": "string", "description": "Titulo corto del evento."},
                    "inicio_iso": {
                        "type": "string",
                        "description": "Fecha/hora de inicio ISO 8601 con zona horaria, ej. 2026-09-10T15:00:00-05:00.",
                    },
                    "fin_iso": {
                        "type": "string",
                        "description": "Fecha/hora de fin ISO 8601 con zona horaria, mismo formato que inicio_iso.",
                    },
                    "descripcion": {"type": "string", "description": "Notas adicionales (opcional)."},
                },
                "required": ["titulo", "inicio_iso", "fin_iso"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_eventos",
            "description": "Lista los eventos del calendario del grupo en un rango de fechas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "desde_iso": {"type": "string", "description": "Desde cuando buscar (ISO 8601). Vacio = ahora."},
                    "hasta_iso": {"type": "string", "description": "Hasta cuando buscar (ISO 8601). Vacio = 7 dias despues de desde_iso."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crear_tarea",
            "description": "Crea una tarea en la lista dedicada de Espartaco (no la lista personal).",
            "parameters": {
                "type": "object",
                "properties": {
                    "titulo": {"type": "string", "description": "Descripcion corta, sin incluir la etiqueta."},
                    "etiqueta": {
                        "type": "string",
                        "enum": ["[Agente]", "[Persona 1]", "[Persona 2]"],
                        "description": "A quien corresponde la tarea.",
                    },
                    "notas": {"type": "string", "description": "Detalles adicionales (opcional)."},
                    "vencimiento_iso": {"type": "string", "description": "Fecha limite, solo fecha ISO 8601, ej. 2026-09-10 (opcional)."},
                    "proyecto": {
                        "type": "string",
                        "description": (
                            "Si es parte de un proyecto personal de varios pasos (ej. 'Viaje a Cartagena'), "
                            "agrupala aca para poder consultar el avance con resumen_proyecto. Opcional."
                        ),
                    },
                },
                "required": ["titulo", "etiqueta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_tareas",
            "description": "Lista las tareas pendientes (sin completar) de la lista de Espartaco.",
            "parameters": {
                "type": "object",
                "properties": {
                    "etiqueta": {
                        "type": "string",
                        "description": (
                            "Una de '[Agente]', '[Persona 1]' o '[Persona 2]' para filtrar. "
                            "Dejar vacio ('') para listar todas."
                        ),
                    },
                    "proyecto": {"type": "string", "description": "Si se especifica, filtra solo las tareas de ese proyecto (opcional)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resumen_proyecto",
            "description": (
                "Resume el avance de un proyecto personal (tareas creadas con ese proyecto): "
                "cuantas van completadas y cuales quedan pendientes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proyecto": {"type": "string", "description": "Nombre del proyecto, tal como se uso al crear las tareas."},
                },
                "required": ["proyecto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "completar_tarea",
            "description": "Marca una tarea de la lista de Espartaco como completada.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tarea_id": {"type": "string", "description": "El id interno de la tarea, tal como lo devuelve listar_tareas."},
                },
                "required": ["tarea_id"],
            },
        },
    },
    {
        # Alias defensivo: el modelo confunde sistematicamente "completar_tarea"
        # con "completear_tarea" (probado, no es ruido aleatorio de un intento).
        "type": "function",
        "function": {
            "name": "completear_tarea",
            "description": "Alias de completar_tarea: marca una tarea de la lista de Espartaco como completada.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tarea_id": {"type": "string", "description": "El id interno de la tarea, tal como lo devuelve listar_tareas."},
                },
                "required": ["tarea_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "registrar_movimiento",
            "description": "Registra un ingreso o gasto en la hoja de finanzas del grupo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fecha_iso": {"type": "string", "description": "Fecha del movimiento, solo fecha ISO 8601, ej. 2026-09-04."},
                    "descripcion": {"type": "string", "description": "Breve descripcion del movimiento."},
                    "categoria": {"type": "string", "description": "Categoria del gasto o ingreso, ej. 'Comida' o 'Sueldo'."},
                    "monto": {"type": "number", "description": "Monto del movimiento, siempre en positivo."},
                    "tipo": {"type": "string", "enum": ["ingreso", "gasto"]},
                    "registrado_por": {"type": "string", "description": "Nombre de quien reporto el movimiento (viene en el contexto del mensaje)."},
                },
                "required": ["fecha_iso", "descripcion", "categoria", "monto", "tipo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_movimientos",
            "description": "Consulta los movimientos financieros registrados en la hoja del grupo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "categoria": {"type": "string", "description": "Si se especifica, filtra por esa categoria."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resumen_gastos",
            "description": (
                "Suma y desglosa los gastos de la hoja de finanzas en vez de listarlos uno por uno. "
                "Usala para 'cuanto gastamos en mercado este mes', 'cuanto lleva registrado Leidy' o "
                "'en que se nos va la plata'. Devuelve totales ya calculados: no vuelvas a sumarlos tu."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "desde": {"type": "string", "description": "Inicio del periodo, 'YYYY-MM-DD' o 'YYYY-MM' (mes completo). Vacio = desde el primer movimiento."},
                    "hasta": {"type": "string", "description": "Fin del periodo, mismo formato. Vacio = hasta el ultimo movimiento."},
                    "categoria": {"type": "string", "description": "Categoria a filtrar, ej. 'mercado'. Tolera mayusculas, tildes y sinonimos. Vacio = todas."},
                    "persona": {"type": "string", "description": "Filtra por quien REGISTRO el movimiento en el chat, ej. 'Leidy'. Vacio = todas."},
                    "agrupar_por": {"type": "string", "enum": ["categoria", "mes", "persona", "concepto"], "description": "Como desglosar el total. Usa 'mes' cuando pregunten por el gasto mensual."},
                    # Sin cadena vacia en el enum: Gemini rechazaba con 400 un
                    # enum que trajera "" y, como el schema de tools viaja en
                    # CADA request, eso tumbaba cualquier conversacion. Se deja
                    # asi por si se vuelve a un modelo Gemini.
                    "tipo": {"type": "string", "enum": ["gasto", "ingreso", "ambos"], "description": "'gasto' por defecto; 'ambos' suma ingresos y gastos."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "comparar_gastos",
            "description": (
                "Compara los gastos mes a mes y senala que subio o bajo mas. Usala cuando pregunten "
                "si algo se disparo de precio, si estan gastando mas que el mes pasado, o como viene "
                "una categoria comparada con antes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "categoria": {"type": "string", "description": "Categoria a mirar en detalle, ej. 'mercado'. Vacio = compara todas las categorias entre si."},
                    "persona": {"type": "string", "description": "Limita la comparacion a quien registro los movimientos. Vacio = todos."},
                    "meses": {"type": "integer", "description": "Cuantos meses con datos mirar hacia atras. 0 o vacio = los ultimos 4."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agregar_pago_recurrente",
            "description": (
                "Registra un nuevo pago recurrente mensual (arriendo, suscripciones, servicios, etc.) "
                "para que Espartaco avise por Telegram cuando se acerque la fecha de pago."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Identificador corto y unico, sin espacios ni ':', ej. 'netflix' o 'arriendo'."},
                    "descripcion": {"type": "string", "description": "Nombre visible del pago para los recordatorios."},
                    "monto": {"type": "number", "description": "Monto mensual del pago."},
                    "dia_del_mes": {"type": "integer", "description": "Dia del mes en que vence (1-31)."},
                    "categoria": {"type": "string", "description": "Categoria para el registro financiero al confirmar (opcional)."},
                    "responsable": {"type": "string", "description": "Quien es responsable de este pago (opcional)."},
                },
                "required": ["id", "descripcion", "monto", "dia_del_mes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_pagos_recurrentes",
            "description": "Lista los pagos recurrentes mensuales configurados.",
            "parameters": {
                "type": "object",
                "properties": {
                    "solo_activos": {"type": "boolean", "description": "Si es true (default), omite los pagos pausados."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirmar_pago_recurrente",
            "description": "Confirma que un pago recurrente del mes vigente ya fue pagado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Identificador del pago, tal como aparece en listar_pagos_recurrentes."},
                    "anio_mes": {"type": "string", "description": "Mes a confirmar 'YYYY-MM'. Vacio = mes actual."},
                    "confirmado_por": {"type": "string", "description": "Nombre de quien confirma (viene en el contexto del mensaje)."},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desactivar_pago_recurrente",
            "description": "Pausa un pago recurrente sin borrarlo (deja de generar recordatorios).",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Identificador del pago recurrente a pausar."},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirmar_medicamento",
            "description": "Confirma que Leidy ya se tomo un medicamento hoy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Identificador del medicamento, tal como aparece en listar_medicamentos."},
                    "confirmado_por": {"type": "string", "description": "Nombre de quien confirma (viene en el contexto del mensaje)."},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_medicamentos",
            "description": "Lista los medicamentos activos configurados y si ya se confirmo la toma de hoy.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desactivar_medicamento",
            "description": (
                "Desactiva un medicamento para que deje de mandar recordatorios (ej. se termino "
                "el tratamiento). Si el medicamento tiene varias tomas al dia, las desactiva todas."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Identificador o nombre del medicamento, tal como aparece en listar_medicamentos."},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_en_internet",
            "description": "Busca en internet (Google) informacion real, ej. precios de un producto o servicio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Que buscar, ej. 'precio Eutirox 100 Colombia'."},
                    "num_resultados": {"type": "integer", "description": "Cuantos resultados devolver (default 5)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_negocios_locales",
            "description": "Busca negocios o proveedores locales (ej. carpinteros) con telefono, direccion y sitio web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Que tipo de negocio buscar, ej. 'carpintero muebles a medida'."},
                    "ubicacion": {"type": "string", "description": "Ciudad o zona donde buscar (opcional, default Bogota, Colombia)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leer_pagina",
            "description": "Abre una pagina web especifica (link exacto de un resultado previo) y devuelve su texto y cualquier contacto detectado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "La URL exacta a abrir."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "proponer_plan",
            "description": (
                "Arma y muestra un plan de tareas propuesto (ej. tras investigar proveedores), "
                "sin crear nada todavia en Google Tasks. Usar en vez de crear_tarea cuando el "
                "pedido implica varios pasos/tareas para el equipo tras una investigacion."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pasos": {
                        "type": "array",
                        "description": "Los pasos propuestos.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "titulo": {"type": "string", "description": "Descripcion corta del paso, sin la etiqueta."},
                                "etiqueta": {
                                    "type": "string",
                                    "enum": ["[Agente]", "[Persona 1]", "[Persona 2]"],
                                    "description": "A quien corresponde el paso.",
                                },
                                "notas": {"type": "string", "description": "Detalles adicionales (opcional)."},
                                "vencimiento_iso": {"type": "string", "description": "Fecha limite, solo fecha ISO 8601 (opcional)."},
                                "proyecto": {
                                    "type": "string",
                                    "description": "Mismo nombre en todos los pasos de un mismo proyecto personal (opcional).",
                                },
                            },
                            "required": ["titulo", "etiqueta"],
                        },
                    },
                },
                "required": ["pasos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirmar_plan",
            "description": "Crea de verdad en Google Tasks el ultimo plan propuesto con proponer_plan, tras confirmacion explicita del usuario.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancelar_plan",
            "description": "Descarta el ultimo plan propuesto con proponer_plan, sin crear nada.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "guardar_nota_personal",
            "description": (
                "Guarda un hecho o preferencia personal para recordarlo a largo plazo, mas alla "
                "de lo que dura el historial de conversacion (ej. gustos, alergias, datos de "
                "familia). No uses esto para tareas ni eventos puntuales."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "texto": {
                        "type": "string",
                        "description": "El hecho o preferencia, en una frase clara y autocontenida.",
                    },
                    "categoria": {"type": "string", "description": "Etiqueta libre, ej. 'Familia', 'Salud', 'Preferencias' (opcional)."},
                    "vigente_hasta_iso": {
                        "type": "string",
                        "description": "Fecha ISO 8601 (solo fecha) despues de la cual ya no vale asumirlo como cierto (opcional).",
                    },
                    "guardado_por": {"type": "string", "description": "Nombre de quien lo menciono (viene en el contexto del mensaje)."},
                },
                "required": ["texto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_notas_personales",
            "description": "Busca en las notas/preferencias personales guardadas a largo plazo con guardar_nota_personal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Texto a buscar. Vacio = todas las notas vigentes."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "programar_iniciativa",
            "description": (
                "Programa una iniciativa para dispararse sola en una fecha futura, sin esperar a que "
                "la pidan de nuevo (ej. mandar ideas de regalo unos dias antes de un cumpleanos)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "descripcion": {"type": "string", "description": "Que se detecto, en una frase corta, ej. 'Cumpleanos de mama'."},
                    "fecha_disparo_iso": {
                        "type": "string",
                        "description": "Fecha/hora ISO 8601 en la que debe dispararse (calculala con anticipacion razonable, ej. 3-5 dias antes del evento).",
                    },
                    "tipo_accion": {
                        "type": "string",
                        "enum": ["ideas_regalo", "recordatorio_generico"],
                        "description": "'ideas_regalo' genera y manda ideas de regalo; 'recordatorio_generico' manda la descripcion/contexto tal cual.",
                    },
                    "contexto": {
                        "type": "string",
                        "description": "Detalles libres necesarios para ejecutar la accion despues (para quien es, gustos, presupuesto, etc.).",
                    },
                },
                "required": ["descripcion", "fecha_disparo_iso", "tipo_accion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirmar_habito",
            "description": "Confirma que ya se cumplio hoy un habito de salud/bienestar (ej. ejercicio, tomar agua).",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Identificador del habito, tal como aparece en listar_habitos."},
                    "confirmado_por": {"type": "string", "description": "Nombre de quien confirma (viene en el contexto del mensaje)."},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_habitos",
            "description": "Lista los habitos de salud/bienestar activos configurados y si ya se cumplieron hoy.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desactivar_habito",
            "description": "Desactiva un habito de salud/bienestar para que deje de mandar recordatorios.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Identificador o nombre del habito, tal como aparece en listar_habitos."},
                },
                "required": ["id"],
            },
        },
    },
]

_DISPATCH = {
    "crear_evento": google_services.crear_evento,
    "listar_eventos": google_services.listar_eventos,
    "crear_tarea": google_services.crear_tarea,
    "listar_tareas": google_services.listar_tareas,
    "resumen_proyecto": google_services.resumen_proyecto,
    "completar_tarea": google_services.completar_tarea,
    "completear_tarea": google_services.completar_tarea,
    "registrar_movimiento": google_services.registrar_movimiento,
    "consultar_movimientos": google_services.consultar_movimientos,
    "resumen_gastos": google_services.resumen_gastos,
    "comparar_gastos": google_services.comparar_gastos,
    "agregar_pago_recurrente": google_services.agregar_pago_recurrente,
    "listar_pagos_recurrentes": google_services.listar_pagos_recurrentes,
    "confirmar_pago_recurrente": google_services.confirmar_pago_recurrente,
    "desactivar_pago_recurrente": google_services.desactivar_pago_recurrente,
    "confirmar_medicamento": google_services.confirmar_medicamento,
    "listar_medicamentos": google_services.listar_medicamentos,
    "desactivar_medicamento": google_services.desactivar_medicamento,
    "guardar_nota_personal": google_services.guardar_nota_personal,
    "buscar_notas_personales": google_services.buscar_notas_personales,
    "programar_iniciativa": google_services.programar_iniciativa,
    "confirmar_habito": google_services.confirmar_habito,
    "listar_habitos": google_services.listar_habitos,
    "desactivar_habito": google_services.desactivar_habito,
    "buscar_en_internet": web_tools.buscar_en_internet,
    "buscar_negocios_locales": web_tools.buscar_negocios_locales,
    "leer_pagina": web_tools.leer_pagina,
    "proponer_plan": google_services.proponer_plan,
    "confirmar_plan": google_services.confirmar_plan,
    "cancelar_plan": google_services.cancelar_plan,
}

_DIAS_SEMANA = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")

def _cargar_historiales_disco() -> dict[int, list[dict]]:
    try:
        crudo = json.loads(HISTORIAL_PATH.read_text(encoding="utf-8"))
        return {int(chat_id): mensajes for chat_id, mensajes in crudo.items()}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError, AttributeError):
        logger.exception("No se pudo leer %s, arranco con historial vacio.", HISTORIAL_PATH)
        return {}


def _guardar_historiales_disco() -> None:
    # Escritura atomica (archivo temporal + rename) para no dejar el archivo a
    # medias si el proceso muere justo durante el write.
    tmp_path = HISTORIAL_PATH.with_suffix(".json.tmp")
    crudo = {str(chat_id): mensajes for chat_id, mensajes in _historiales.items()}
    tmp_path.write_text(json.dumps(crudo, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(HISTORIAL_PATH)


_client_cerebro: AsyncOpenAI | None = None
_model_name: str | None = None
_historiales: dict[int, list[dict]] = _cargar_historiales_disco()

def configure(
    *,
    deepseek_api_key: str,
    deepseek_model: str,
    deepseek_base_url: str = DEEPSEEK_BASE_URL,
) -> None:
    """Inicializa el cliente de DeepSeek. Debe llamarse una vez al arrancar.

    Un solo cliente/modelo para cerebro (`ask()`) y vision
    (`analizar_imagen()`): `deepseek-flash` acepta texto e imagen.
    """
    global _client_cerebro, _model_name
    _client_cerebro = AsyncOpenAI(
        base_url=deepseek_base_url, api_key=deepseek_api_key, timeout=TIMEOUT_CEREBRO, max_retries=2
    )
    _model_name = deepseek_model
    logger.info("Cerebro/vision configurados directo contra DeepSeek (%s) con modelo '%s'.", deepseek_base_url, deepseek_model)


async def analizar_imagen(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Describe una imagen (fecha/hora/lugar/motivo si los tiene) via DeepSeek,
    para que el llamador decida (con el usuario) si hay que agendar una cita,
    crear un recordatorio, o no hacer nada.

    Args:
      image_bytes: Contenido crudo de la imagen (ej. descargada de Telegram).
      mime_type: Tipo MIME de la imagen (Telegram manda fotos como JPEG).

    Returns:
      Descripcion en texto de lo que se ve en la imagen.

    Raises:
      Exception: si DeepSeek no responde -- el llamador decide como avisarle al
      usuario (mismo patron que brain.ask).
    """
    if _client_cerebro is None:
        raise RuntimeError("brain.configure() debe llamarse antes de usar analizar_imagen().")

    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    respuesta = await _client_cerebro.chat.completions.create(
        model=_model_name,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT_ANALIZAR_IMAGEN},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        # DeepSeek es un modelo "thinking" que gasta buena parte del presupuesto
        # en reasoning_content antes de la respuesta final -- con un
        # max_tokens chico, el corte por "length" puede truncar el content
        # real a texto vacio o a medias.
        max_tokens=800,
        timeout=TIMEOUT_VISION,
    )
    return (respuesta.choices[0].message.content or "").strip()


def reset_session(chat_id: int) -> None:
    """Olvida la memoria de conversacion de un chat concreto."""
    _historiales.pop(chat_id, None)
    _guardar_historiales_disco()


def _get_historial(chat_id: int) -> list[dict]:
    return _historiales.setdefault(chat_id, [])


def _peso_tokens(mensajes: list[dict]) -> int:
    """Estima los tokens de una lista de mensajes (~4 caracteres por token).

    Aproximado a proposito: sirve para decidir cuanto recortar, no para
    facturar, y evita cargar un tokenizer distinto por cada proveedor.
    """
    return len(json.dumps(mensajes, ensure_ascii=False)) // CHARS_POR_TOKEN


def _corte_valido(historial: list[dict], desde: int) -> int:
    """Avanza `desde` hasta el proximo mensaje 'user'.

    Cortar en cualquier otro punto puede partir a la mitad una secuencia
    assistant(tool_calls) + tool, que el API exige completa.
    """
    corte = desde
    while corte < len(historial) and historial[corte]["role"] != "user":
        corte += 1
    return corte


def _recortar_historial(historial: list[dict]) -> None:
    """Recorta el historial por cantidad de mensajes y por peso en tokens.

    El limite de mensajes solo no basta: unos pocos resultados de tools largos
    (un listado de calendario, un plan completo) pesan mas que decenas de
    mensajes cortos, y el request terminaba pasandose del limite del proveedor.
    """
    exceso = len(historial) - MAX_MENSAJES_HISTORIAL
    if exceso > 0:
        del historial[: _corte_valido(historial, exceso)]

    # Ir soltando el bloque mas viejo hasta entrar en el presupuesto. Se corta
    # de a un turno completo ('user' -> siguiente 'user') para no dejar
    # tool_calls huerfanos.
    while historial and _peso_tokens(historial) > MAX_TOKENS_HISTORIAL:
        corte = _corte_valido(historial, 1)
        if corte >= len(historial):
            # Queda un solo turno y todavia se pasa: mejor uno grande que
            # ninguno, el recorte por tool result ya limita el daño.
            break
        del historial[:corte]


def _contexto_temporal(remitente: str) -> str:
    ahora = datetime.now().astimezone()
    dia = _DIAS_SEMANA[ahora.weekday()]
    quien = f", escribe {remitente}" if remitente else ""
    return (
        f"[Contexto: hoy es {dia} {ahora.date().isoformat()}, son las "
        f"{ahora.strftime('%H:%M')} ({ahora.isoformat(timespec='seconds')}){quien}]"
    )


def _truncar_resultado(nombre: str, resultado: str) -> str:
    """Corta un resultado de tool demasiado largo antes de que entre al historial.

    Busquedas web y listados largos pueden traer miles de caracteres que se
    quedan pesando en el historial durante todo el resto de la conversacion.
    """
    if not isinstance(resultado, str) or len(resultado) <= MAX_CHARS_RESULTADO_TOOL:
        return resultado
    logger.info(
        "Resultado de '%s' truncado: %d -> %d caracteres.",
        nombre, len(resultado), MAX_CHARS_RESULTADO_TOOL,
    )
    return resultado[:MAX_CHARS_RESULTADO_TOOL] + "\n[...resultado recortado por longitud...]"


async def _ejecutar_tool(nombre: str, argumentos_json: str, chat_id: int) -> str:
    func = _DISPATCH.get(nombre)
    if func is None:
        return f"Herramienta desconocida: {nombre}."
    try:
        argumentos = json.loads(argumentos_json) if argumentos_json else {}
    except json.JSONDecodeError:
        argumentos = {}
    # chat_id se inyecta aca, nunca lo ve ni lo controla el modelo (no esta en
    # _TOOLS_SCHEMA) -- solo lo reciben las funciones que lo declaran en su firma.
    argumentos["chat_id"] = chat_id
    parametros_validos = set(inspect.signature(func).parameters)
    argumentos = {k: v for k, v in argumentos.items() if k in parametros_validos}
    try:
        return _truncar_resultado(nombre, await func(**argumentos))
    except TypeError as exc:
        logger.warning("Argumentos invalidos para %s: %r (%s)", nombre, argumentos, exc)
        return f"Faltan datos para usar '{nombre}' correctamente, pidele mas detalle al usuario."
    except RefreshError:
        logger.exception("Autenticacion de Google vencida ejecutando %s", nombre)
        return (
            "No pude conectarme a Google (Calendar/Tasks/Sheets): la autorizacion vencio y "
            "necesita renovarse a mano (correr google_auth.py), no es algo que yo pueda "
            "arreglar solo. Avisale a Roberto."
        )
    except Exception:
        logger.exception("Fallo ejecutando la herramienta %s", nombre)
        return f"La herramienta '{nombre}' fallo inesperadamente."


async def ask(chat_id: int, text: str, remitente: str = "") -> str:
    """Manda `text` al modelo dentro del historial de `chat_id` y devuelve la respuesta.

    `remitente` (nombre de quien escribio en Telegram) se agrega al contexto
    para que el modelo pueda usarlo, por ejemplo, al registrar un movimiento
    financiero.

    Nunca tarda mas de TIMEOUT_TOTAL_RESPUESTA_SEG: si se vence, se descarta lo
    que haya quedado a medias de este turno (un assistant con tool_calls sin
    su resultado haria que DeepSeek rechace el siguiente pedido con 400) y se
    devuelve MENSAJE_TIMEOUT_RESPUESTA.
    """
    if _client_cerebro is None:
        raise RuntimeError("brain.configure() debe llamarse antes de usar ask().")

    historial = _get_historial(chat_id)
    entrada_usuario = {"role": "user", "content": f"{_contexto_temporal(remitente)}\n{text}"}
    historial.append(entrada_usuario)
    try:
        return await asyncio.wait_for(
            _ask_sin_plazo(chat_id, historial), timeout=TIMEOUT_TOTAL_RESPUESTA_SEG
        )
    except TimeoutError:
        logger.error(
            "La respuesta no termino en %ss, se abandona el turno - chat_id=%s",
            TIMEOUT_TOTAL_RESPUESTA_SEG, chat_id,
        )
        # Por identidad: el recorte del historial puede haber movido los indices.
        posicion = next((i for i, m in enumerate(historial) if m is entrada_usuario), None)
        if posicion is not None:
            del historial[posicion + 1:]
        historial.append({"role": "assistant", "content": MENSAJE_TIMEOUT_RESPUESTA})
        _guardar_historiales_disco()
        return MENSAJE_TIMEOUT_RESPUESTA


def _modelo_para_llamada() -> str:
    if MODELO_RESPALDO and time.monotonic() < _respaldo_hasta:
        return MODELO_RESPALDO
    return _model_name


def _pasar_a_respaldo(modelo_fallido: str, chat_id: int) -> None:
    global _respaldo_hasta
    if not MODELO_RESPALDO or modelo_fallido == MODELO_RESPALDO:
        return
    _respaldo_hasta = time.monotonic() + MINUTOS_EN_RESPALDO * 60
    logger.warning(
        "Modelo '%s' sin respuesta: se usa '%s' por %d min - chat_id=%s",
        modelo_fallido, MODELO_RESPALDO, MINUTOS_EN_RESPALDO, chat_id,
    )


async def _ask_sin_plazo(chat_id: int, historial: list[dict]) -> str:
    """Cuerpo de ask(): el ciclo modelo -> tools -> modelo, sin plazo total."""

    # Recortar ANTES de llamar, no solo al final: si el historial venia pesado
    # del turno anterior, este request ya saldria pasado de tokens y el
    # proveedor lo rechaza (413) antes de que el recorte posterior sirva.
    _recortar_historial(historial)

    mensajes = [{"role": "system", "content": SYSTEM_PROMPT}] + historial
    respuesta_final = "No pude pensar en una respuesta, intenta de nuevo."

    for _ in range(MAX_TOOL_HOPS):
        completion = None
        for intento in range(MAX_INTENTOS_POR_HOP):
            modelo = _modelo_para_llamada()
            try:
                completion = await asyncio.wait_for(
                    _client_cerebro.chat.completions.create(
                        model=modelo, messages=mensajes, tools=_TOOLS_SCHEMA
                    ),
                    timeout=TIMEOUT_LLAMADA_CEREBRO_SEG,
                )
                break
            except (TimeoutError, openai.APITimeoutError):
                logger.warning(
                    "Intento %d/%d: DeepSeek ('%s') no termino a tiempo, se reintenta - chat_id=%s",
                    intento + 1, MAX_INTENTOS_POR_HOP, modelo, chat_id,
                )
                _pasar_a_respaldo(modelo, chat_id)
            except openai.BadRequestError as exc:
                # El modelo a veces genera un tool_call con el nombre corrupto o
                # argumentos que no calzan con el schema (glitch de generacion).
                # Reintentar el mismo pedido casi siempre lo resuelve.
                logger.warning(
                    "Intento %d/%d fallo (probable tool_call invalido) - chat_id=%s: %s",
                    intento + 1, MAX_INTENTOS_POR_HOP, chat_id, exc,
                )
            except openai.APIStatusError as exc:
                sugerencia = exc.body.get("recovery_hint") if isinstance(exc.body, dict) else None
                if isinstance(sugerencia, dict) and sugerencia.get("action") == "retry":
                    logger.warning(
                        "Intento %d/%d fallo (codigo %d, error transitorio) - chat_id=%s: %s",
                        intento + 1, MAX_INTENTOS_POR_HOP, exc.status_code, chat_id, exc,
                    )
                    await asyncio.sleep(ESPERA_REINTENTO_TRANSITORIO)
                    continue
                if exc.status_code != 413:
                    logger.exception(
                        "Fallo la llamada al modelo (DeepSeek) - chat_id=%s", chat_id
                    )
                    break
                # 413 = el request excede el limite de tokens-por-minuto del
                # proveedor. Es transitorio, asi que se espera el reset y se
                # suelta el bloque mas viejo del historial para bajar el peso.
                logger.warning(
                    "Intento %d/%d rechazado por limite de tokens (413, ~%d tokens) - chat_id=%s",
                    intento + 1, MAX_INTENTOS_POR_HOP, _peso_tokens(mensajes), chat_id,
                )
                corte = _corte_valido(historial, 1)
                if corte < len(historial):
                    del historial[:corte]
                    mensajes = [{"role": "system", "content": SYSTEM_PROMPT}] + historial
                await asyncio.sleep(ESPERA_REINTENTO_413)
            except Exception:
                logger.exception("Fallo la llamada al modelo (DeepSeek) - chat_id=%s", chat_id)
                break

        if completion is None:
            respuesta_final = "Se me trabo el cerebro un segundo, intenta de nuevo."
            historial.append({"role": "assistant", "content": respuesta_final})
            break

        mensaje = completion.choices[0].message

        if mensaje.tool_calls:
            entrada_asistente = {
                "role": "assistant",
                "content": mensaje.content,
                "tool_calls": [tc.model_dump() for tc in mensaje.tool_calls],
            }
            historial.append(entrada_asistente)
            # DeepSeek exige su reasoning_content de vuelta mientras el turno
            # siga abierto (400 si falta), pero no en turnos ya cerrados: va
            # solo en `mensajes`, no al historial persistido, para no inflarlo.
            razonamiento = getattr(mensaje, "reasoning_content", None)
            if razonamiento is None and mensaje.model_extra:
                razonamiento = mensaje.model_extra.get("reasoning_content")
            if razonamiento:
                entrada_asistente = {**entrada_asistente, "reasoning_content": razonamiento}
            mensajes.append(entrada_asistente)
            for tc in mensaje.tool_calls:
                resultado = await _ejecutar_tool(tc.function.name, tc.function.arguments, chat_id)
                entrada_tool = {"role": "tool", "tool_call_id": tc.id, "content": resultado}
                mensajes.append(entrada_tool)
                historial.append(entrada_tool)
            continue

        respuesta_final = mensaje.content or respuesta_final
        historial.append({"role": "assistant", "content": respuesta_final})
        break

    _recortar_historial(historial)
    _guardar_historiales_disco()
    return respuesta_final
