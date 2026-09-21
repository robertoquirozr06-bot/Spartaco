"""
main.py - Nucleo asincrono de Espartaco (Fase 4: Telegram + cerebro via OmniRoute + Google tools)
"""
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, time as dt_time, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

ZONA_HORARIA = ZoneInfo("America/Bogota")
HORA_ALERTA_CALENDARIO = dt_time(hour=8, minute=0, tzinfo=ZONA_HORARIA)
HORA_ALERTA_PAGOS = dt_time(hour=8, minute=5, tzinfo=ZONA_HORARIA)
HORA_ALERTA_EVENTOS_MANANA = dt_time(hour=20, minute=0, tzinfo=ZONA_HORARIA)
VENTANA_RECORDATORIO_2H = timedelta(hours=2)

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Prueba de vida para el watchdog. La escribe el job `heartbeat` SOLO si el
# polling de Telegram sigue activo, asi que su antiguedad distingue un bot sano
# de uno congelado. Que el proceso exista no prueba que este escuchando.
LATIDO_PATH = BASE_DIR / "latido.json"

# Margen antes de que el proceso decida suicidarse al arrancar, por si el job
# llegara a correr antes que el updater. Hoy no pasa (run_polling levanta el
# updater antes que el job queue), pero un cambio de version no deberia poder
# meter al bot en un bucle de reinicios.
GRACIA_ARRANQUE_SEG = 90
_ARRANQUE = datetime.now(ZONA_HORARIA)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            LOG_DIR / "espartaco.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("espartaco")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

try:
    load_dotenv(dotenv_path=ENV_PATH, encoding="utf-8")
except UnicodeDecodeError as exc:
    logger.critical(
        "No se pudo leer .env como UTF-8. Verifica que este guardado en UTF-8 "
        "SIN BOM (no UTF-16). Detalle: %s", exc
    )
    sys.exit(1)

# brain/google_services leen variables de entorno (TASKLIST_ID, FINANCE_SHEET_ID,
# etc.) al importarse, asi que deben importarse DESPUES de load_dotenv().
import brain
import google_services

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OMNIROUTE_BASE_URL = os.getenv("OMNIROUTE_BASE_URL", "http://localhost:20128/v1").strip()
OMNIROUTE_API_KEY = os.getenv("OMNIROUTE_API_KEY", "").strip()
OMNIROUTE_MODEL = os.getenv("OMNIROUTE_MODEL", "auto/best-chat").strip()
OMNIROUTE_TRANSCRIPTION_MODEL = os.getenv("OMNIROUTE_TRANSCRIPTION_MODEL", "whisper-large-v3").strip()
# Modelo con vision para leer imagenes (ver brain.analizar_imagen). El modelo
# de vision de Groq (llama-4-scout) no esta disponible hoy en el catalogo
# "live" de OmniRoute para ese proveedor; gemini-3.1-flash-lite via Google AI
# Studio si respondio bien en pruebas reales.
OMNIROUTE_VISION_MODEL = os.getenv("OMNIROUTE_VISION_MODEL", "gemini/gemini-3.1-flash-lite").strip()

_raw_chat_id = os.getenv("ALLOWED_CHAT_ID", "").strip()
ALLOWED_CHAT_ID = int(_raw_chat_id) if _raw_chat_id else None

if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "PON_AQUI_TU_TOKEN_DE_BOTFATHER":
    logger.critical(
        "TELEGRAM_TOKEN no esta definido (o esta vacio) en %s. "
        "Completa el valor real obtenido de @BotFather y vuelve a arrancar.", ENV_PATH
    )
    sys.exit(1)

if not OMNIROUTE_API_KEY:
    logger.critical(
        "OMNIROUTE_API_KEY no esta definido (o esta vacio) en %s. "
        "Espartaco necesita el cerebro (via OmniRoute) para funcionar.", ENV_PATH
    )
    sys.exit(1)

logger.info(
    "Restriccion de chat: %s",
    ALLOWED_CHAT_ID if ALLOWED_CHAT_ID is not None else "ninguna (respondera en cualquier chat)"
)


def is_chat_allowed(update: Update) -> bool:
    if ALLOWED_CHAT_ID is None:
        return True
    return update.effective_chat is not None and update.effective_chat.id == ALLOWED_CHAT_ID


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    logger.info("/start recibido - chat_id=%s chat_type=%s", chat.id, chat.type)
    if not is_chat_allowed(update):
        logger.warning("Chat no autorizado (chat_id=%s) uso /start. Ignorado.", chat.id)
        return
    await update.message.reply_text(
        "Espartaco listo. Sistema operativo.\nEscribe /menu para ver las funciones disponibles, "
        "o cuentame directo que necesitas."
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not is_chat_allowed(update):
        logger.warning("Chat no autorizado (chat_id=%s) uso /reset. Ignorado.", chat.id)
        return
    brain.reset_session(chat.id)
    logger.info("Memoria de conversacion reiniciada - chat_id=%s", chat.id)
    await update.message.reply_text("Listo, borre lo que teniamos hablado y empiezo de cero.")


async def _responder_con_cerebro(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, texto: str, remitente: str
) -> None:
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        respuesta = await brain.ask(chat_id, texto, remitente=remitente)
    except Exception:
        logger.exception("Fallo al pedirle respuesta al cerebro - chat_id=%s", chat_id)
        await context.bot.send_message(chat_id=chat_id, text="Se me trabo el cerebro un segundo, intenta de nuevo.")
        return

    # send_message en vez de message.reply_text: al reconectar despues de estar
    # offline (drop_pending_updates=False) se procesan mensajes atrasados cuyo
    # "reply" ya no siempre es valido para Telegram ("Message to be replied not
    # found"), lo que tumbaba la respuesta aunque el cerebro/tool ya hubiera
    # corrido bien.
    await context.bot.send_message(chat_id=chat_id, text=respuesta)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    if message is None or message.text is None:
        return
    logger.info("Mensaje recibido - chat_id=%s texto=%r", chat.id, message.text)
    if not is_chat_allowed(update):
        logger.debug("Mensaje ignorado, chat no autorizado: %s", chat.id)
        return

    remitente = update.effective_user.first_name if update.effective_user else ""
    context.application.create_task(_revisar_pendientes_por_actividad(context))
    await _responder_con_cerebro(context, chat.id, message.text, remitente)


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    if message is None or message.voice is None:
        return
    if not is_chat_allowed(update):
        logger.debug("Nota de voz ignorada, chat no autorizado: %s", chat.id)
        return

    logger.info("Nota de voz recibida - chat_id=%s duracion=%ss", chat.id, message.voice.duration)
    remitente = update.effective_user.first_name if update.effective_user else ""

    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    try:
        archivo = await context.bot.get_file(message.voice.file_id)
        audio_bytes = bytes(await archivo.download_as_bytearray())
        texto = await brain.transcribir_audio(audio_bytes)
    except Exception:
        logger.exception("Fallo transcribiendo la nota de voz - chat_id=%s", chat.id)
        await context.bot.send_message(
            chat_id=chat.id,
            text="No pude escuchar bien la nota de voz, intenta de nuevo o escribeme el mensaje.",
        )
        return

    if not texto:
        await context.bot.send_message(
            chat_id=chat.id, text="No detecte voz en esa nota, intenta grabarla de nuevo."
        )
        return

    # Muestro lo que entendi antes de responder: si la transcripcion sale mal
    # (acento, ruido de fondo), se nota de inmediato en vez de que Espartaco
    # responda algo raro sin dar pistas de por que.
    await context.bot.send_message(chat_id=chat.id, text=f'🎙️ Escuche: "{texto}"')

    context.application.create_task(_revisar_pendientes_por_actividad(context))
    await _responder_con_cerebro(context, chat.id, texto, remitente)


# Descripcion de la ultima imagen recibida por chat, mientras se espera a que
# el usuario elija que hacer con ella (mismo patron que _planes_pendientes en
# google_services.py). Una imagen nueva reemplaza a la pendiente anterior.
_imagenes_pendientes: dict[int, str] = {}

_OPCIONES_IMAGEN = {
    "cita": ("📅 Agendar cita", "Agenda un evento en el calendario"),
    "recordatorio": ("⏰ Crear recordatorio", "Crea una tarea/recordatorio"),
    "nada": ("🚫 No hacer nada", ""),
}


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    if message is None or not message.photo:
        return
    if not is_chat_allowed(update):
        logger.debug("Imagen ignorada, chat no autorizado: %s", chat.id)
        return

    logger.info("Imagen recibida - chat_id=%s", chat.id)
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    try:
        # photo[-1]: Telegram manda varias resoluciones de la misma foto, la
        # ultima es la de mayor calidad.
        archivo = await context.bot.get_file(message.photo[-1].file_id)
        imagen_bytes = bytes(await archivo.download_as_bytearray())
        descripcion = await brain.analizar_imagen(imagen_bytes)
    except Exception:
        logger.exception("Fallo analizando la imagen recibida - chat_id=%s", chat.id)
        await context.bot.send_message(
            chat_id=chat.id, text="No pude leer bien esa imagen, intenta de nuevo."
        )
        return

    _imagenes_pendientes[chat.id] = descripcion
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(etiqueta, callback_data=f"imagen:{clave}")]
        for clave, (etiqueta, _) in _OPCIONES_IMAGEN.items()
    ])
    await context.bot.send_message(
        chat_id=chat.id,
        text=f"🖼️ Esto es lo que veo en la imagen:\n{descripcion}\n\n¿Qué quieres que haga?",
        reply_markup=teclado,
    )


async def imagen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_chat_allowed(update):
        await query.answer()
        return

    _, clave = query.data.split(":", 1)
    opcion = _OPCIONES_IMAGEN.get(clave)
    await query.answer()
    if opcion is None:
        return

    chat = update.effective_chat
    descripcion = _imagenes_pendientes.pop(chat.id, None)
    if descripcion is None:
        await query.edit_message_text("Esa imagen ya no esta disponible, mandala de nuevo si quieres que la revise.")
        return

    etiqueta, intencion = opcion
    if clave == "nada":
        await query.edit_message_text(f"{query.message.text}\n\n{etiqueta}, listo.", reply_markup=None)
        return

    await query.edit_message_text(f"{query.message.text}\n\n{etiqueta}...", reply_markup=None)
    remitente = query.from_user.first_name if query.from_user else ""
    prompt = (
        f"[Imagen recibida] El usuario confirmo que esta imagen es para: {intencion.lower()}. "
        f"Esto es lo que se detecto en la imagen:\n{descripcion}\n"
        "Si falta un dato imprescindible (ej. la fecha u hora), pregunta antes de crear nada."
    )
    context.application.create_task(_revisar_pendientes_por_actividad(context))
    await _responder_con_cerebro(context, chat.id, prompt, remitente)


_ULTIMA_REVISION_ACTIVIDAD: datetime | None = None
INTERVALO_MIN_ENTRE_REVISIONES_ACTIVIDAD = 120  # segundos


async def _revisar_pendientes_por_actividad(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Corre los mismos jobs de recordatorios (cada uno con su propio anti-duplicado)
    disparados por un mensaje real en el chat, no solo por el poll de fondo -- asi,
    si el PC estuvo apagado justo a la hora de algo (o el poll, ahora mas espaciado,
    todavia no le tocaba), se pone al dia apenas alguien vuelve a escribir en vez de
    esperar al proximo ciclo.

    Debounce de INTERVALO_MIN_ENTRE_REVISIONES_ACTIVIDAD: en una racha de varios
    mensajes seguidos, solo el primero de la ventana dispara la revision real --
    evita que una conversacion activa genere mas llamadas a la API que el propio
    poll que se busca aliviar.
    """
    global _ULTIMA_REVISION_ACTIVIDAD
    ahora = datetime.now(ZONA_HORARIA)
    if (
        _ULTIMA_REVISION_ACTIVIDAD is not None
        and (ahora - _ULTIMA_REVISION_ACTIVIDAD).total_seconds() < INTERVALO_MIN_ENTRE_REVISIONES_ACTIVIDAD
    ):
        return
    _ULTIMA_REVISION_ACTIVIDAD = ahora

    for job in (
        alerta_calendario_diaria,
        recordatorio_pagos_diario,
        recordatorio_medicamentos,
        recordatorio_habitos,
        recordatorio_eventos_manana,
        recordatorio_eventos_2h,
        revisar_iniciativas_proactivas,
    ):
        try:
            await job(context)
        except Exception:
            logger.exception("Fallo el chequeo de '%s' disparado por actividad en el chat.", job.__name__)


_MENU_OPCIONES = {
    "gasto": ("💰 Registrar gasto", "Quiero registrar un gasto."),
    "cita": ("📅 Agendar cita", "Quiero agendar una cita."),
    "revisar": ("🗓️ Revisar citas", "¿Qué tengo agendado próximamente?"),
    "recordatorios": (
        "⏰ Ver recordatorios",
        "¿Qué recordatorios tengo activos (pagos, medicamentos, eventos)?",
    ),
}


def _teclado_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(etiqueta, callback_data=f"menu:{clave}")] for clave, (etiqueta, _) in _MENU_OPCIONES.items()]
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not is_chat_allowed(update):
        logger.warning("Chat no autorizado (chat_id=%s) uso /menu. Ignorado.", chat.id)
        return
    await update.message.reply_text(
        "¿Qué necesitas? Elige una opción o, si prefieres, escríbeme directo en texto libre.",
        reply_markup=_teclado_menu(),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_chat_allowed(update):
        await query.answer()
        return

    _, clave = query.data.split(":", 1)
    opcion = _MENU_OPCIONES.get(clave)
    await query.answer()
    if opcion is None:
        return

    chat = update.effective_chat
    remitente = query.from_user.first_name if query.from_user else ""
    _, texto = opcion
    context.application.create_task(_revisar_pendientes_por_actividad(context))
    await _responder_con_cerebro(context, chat.id, texto, remitente)


TIMEOUT_JOB_CALENDARIO = 45
TIMEOUT_JOB_PAGOS = 90
TIMEOUT_JOB_MEDICAMENTOS = 90
TIMEOUT_JOB_EVENTOS_MANANA = 45
TIMEOUT_JOB_EVENTOS_2H = 45
TIMEOUT_JOB_INICIATIVAS = 60
TIMEOUT_JOB_LIMPIEZA_INICIATIVAS = 60
DIAS_ENTRE_LIMPIEZAS_INICIATIVAS = 30
TIMEOUT_JOB_HABITOS = 90

# Antes cada uno de estos jobs revisaba la hoja cada 5 min (300s), 24/7, sin
# importar si de verdad habia algo por revisar. Se subio a 25 min para bajar
# el volumen de llamadas a la API de fondo; el respaldo real de "no te lo
# pierdas" ahora es _revisar_pendientes_por_actividad, disparada por cualquier
# mensaje real en el chat (ver handle_message), no el poll en si.
INTERVALO_POLL_VARIABLE = 1500

ESTADO_JOBS_PATH = BASE_DIR / "estado_jobs.json"


def _leer_estado_jobs() -> dict:
    try:
        return json.loads(ESTADO_JOBS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _guardar_estado_jobs(estado: dict) -> None:
    ESTADO_JOBS_PATH.write_text(json.dumps(estado), encoding="utf-8")


async def alerta_calendario_diaria(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.wait_for(_alerta_calendario_diaria_impl(context), timeout=TIMEOUT_JOB_CALENDARIO)
    except asyncio.TimeoutError:
        logger.error("alerta_calendario_diaria: timeout (%ss), se salta esta ejecucion.", TIMEOUT_JOB_CALENDARIO)


async def _alerta_calendario_diaria_impl(context: ContextTypes.DEFAULT_TYPE) -> None:
    if ALLOWED_CHAT_ID is None:
        logger.warning("ALLOWED_CHAT_ID no configurado; salto la alerta diaria de calendario.")
        return

    ahora = datetime.now(ZONA_HORARIA)
    if ahora.time() < HORA_ALERTA_CALENDARIO.replace(tzinfo=None):
        return  # todavia no toca hoy

    hoy_iso = ahora.date().isoformat()
    estado = _leer_estado_jobs()
    if estado.get("ultima_alerta_calendario") == hoy_iso:
        return  # ya se envio hoy (el job corre cada 5 min; esto evita duplicados)

    resumen = await google_services.listar_eventos(
        desde_iso=ahora.isoformat(), hasta_iso=(ahora + timedelta(hours=24)).isoformat()
    )
    if resumen.startswith("No hay eventos"):
        logger.info("Alerta diaria de calendario: no hay eventos en las proximas 24h.")
    else:
        logger.info("Alerta diaria de calendario enviada a chat_id=%s", ALLOWED_CHAT_ID)
        await context.bot.send_message(
            chat_id=ALLOWED_CHAT_ID, text=f"📅 Buenas! Esto es lo que tenemos para las proximas 24h:\n{resumen}"
        )

    estado["ultima_alerta_calendario"] = hoy_iso
    _guardar_estado_jobs(estado)


async def recordatorio_pagos_diario(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.wait_for(_recordatorio_pagos_diario_impl(context), timeout=TIMEOUT_JOB_PAGOS)
    except asyncio.TimeoutError:
        logger.error("recordatorio_pagos_diario: timeout (%ss), se salta esta ejecucion.", TIMEOUT_JOB_PAGOS)


async def _recordatorio_pagos_diario_impl(context: ContextTypes.DEFAULT_TYPE) -> None:
    if ALLOWED_CHAT_ID is None:
        logger.warning("ALLOWED_CHAT_ID no configurado; salto el recordatorio diario de pagos.")
        return

    ahora = datetime.now(ZONA_HORARIA)
    if ahora.time() < HORA_ALERTA_PAGOS.replace(tzinfo=None):
        return  # todavia no toca hoy

    hoy = ahora.date()
    recordatorios = await google_services.calcular_recordatorios_pagos(fecha_referencia_iso=hoy.isoformat())

    pagos_manana = recordatorios.get("manana", [])
    if pagos_manana:
        lineas = "\n".join(f"- {p['descripcion']}: ${p['monto']}" for p in pagos_manana)
        try:
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"Recordatorio: manana vence(n) este/estos pago(s):\n{lineas}",
            )
            for p in pagos_manana:
                await google_services.marcar_aviso_pago_enviado(p["fila"], hoy.isoformat())
        except Exception:
            logger.exception("Fallo enviando el recordatorio de pagos de manana.")

    for p in recordatorios.get("hoy", []):
        try:
            teclado = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Ya pague", callback_data=f"pago:{p['id']}:{hoy.strftime('%Y%m')}")]]
            )
            atraso = f" (atrasado {p['dias_atraso']} dia(s))" if p.get("dias_atraso") else ""
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"Hoy vence el pago de '{p['descripcion']}' (${p['monto']}){atraso}. Confirma cuando lo hagas:",
                reply_markup=teclado,
            )
            await google_services.marcar_aviso_pago_enviado(p["fila"], hoy.isoformat())
        except Exception:
            logger.exception("Fallo enviando el recordatorio de pago '%s'.", p.get("id"))


async def recordatorio_medicamentos(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.wait_for(_recordatorio_medicamentos_impl(context), timeout=TIMEOUT_JOB_MEDICAMENTOS)
    except asyncio.TimeoutError:
        logger.error("recordatorio_medicamentos: timeout (%ss), se salta esta ejecucion.", TIMEOUT_JOB_MEDICAMENTOS)


async def _recordatorio_medicamentos_impl(context: ContextTypes.DEFAULT_TYPE) -> None:
    if ALLOWED_CHAT_ID is None:
        logger.warning("ALLOWED_CHAT_ID no configurado; salto el recordatorio de medicamentos.")
        return

    hoy = datetime.now(ZONA_HORARIA).date().isoformat()
    pendientes = await google_services.calcular_recordatorios_medicamentos()

    for m in pendientes:
        try:
            teclado = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Ya lo tome", callback_data=f"medicamento:{m['id']}:{hoy}")]]
            )
            notas = f" ({m['notas']})" if m.get("notas") else ""
            atraso = " (atrasado)" if m.get("atrasado") else ""
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"💊 Hora de: {m['nombre']}{notas}{atraso}",
                reply_markup=teclado,
            )
            await google_services.marcar_medicamento_enviado(m["fila"], hoy)
        except Exception:
            logger.exception("Fallo enviando el recordatorio de medicamento '%s'.", m.get("id"))


async def recordatorio_habitos(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.wait_for(_recordatorio_habitos_impl(context), timeout=TIMEOUT_JOB_HABITOS)
    except asyncio.TimeoutError:
        logger.error("recordatorio_habitos: timeout (%ss), se salta esta ejecucion.", TIMEOUT_JOB_HABITOS)


async def _recordatorio_habitos_impl(context: ContextTypes.DEFAULT_TYPE) -> None:
    if ALLOWED_CHAT_ID is None:
        logger.warning("ALLOWED_CHAT_ID no configurado; salto el recordatorio de habitos.")
        return

    hoy = datetime.now(ZONA_HORARIA).date().isoformat()
    pendientes = await google_services.calcular_recordatorios_habitos()

    for h in pendientes:
        try:
            teclado = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Hecho", callback_data=f"habito:{h['id']}:{hoy}")]]
            )
            notas = f" ({h['notas']})" if h.get("notas") else ""
            atraso = " (atrasado)" if h.get("atrasado") else ""
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"🌱 Hora de: {h['nombre']}{notas}{atraso}",
                reply_markup=teclado,
            )
            await google_services.marcar_habito_enviado(h["fila"], hoy)
        except Exception:
            logger.exception("Fallo enviando el recordatorio de habito '%s'.", h.get("id"))


async def recordatorio_eventos_manana(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.wait_for(_recordatorio_eventos_manana_impl(context), timeout=TIMEOUT_JOB_EVENTOS_MANANA)
    except asyncio.TimeoutError:
        logger.error(
            "recordatorio_eventos_manana: timeout (%ss), se salta esta ejecucion.", TIMEOUT_JOB_EVENTOS_MANANA
        )


async def _recordatorio_eventos_manana_impl(context: ContextTypes.DEFAULT_TYPE) -> None:
    if ALLOWED_CHAT_ID is None:
        logger.warning("ALLOWED_CHAT_ID no configurado; salto el aviso de eventos de manana.")
        return

    ahora = datetime.now(ZONA_HORARIA)
    if ahora.time() < HORA_ALERTA_EVENTOS_MANANA.replace(tzinfo=None):
        return  # todavia no toca hoy

    manana = (ahora + timedelta(days=1)).date()
    manana_iso = manana.isoformat()
    inicio_ventana = datetime.combine(manana, dt_time.min, tzinfo=ZONA_HORARIA)
    fin_ventana = inicio_ventana + timedelta(days=1)

    eventos = await google_services.listar_eventos_estructurados(
        desde_iso=inicio_ventana.isoformat(), hasta_iso=fin_ventana.isoformat()
    )
    pendientes = [ev for ev in eventos if ev["aviso_manana"] != manana_iso]
    if not pendientes:
        return  # nada nuevo que avisar (vacio, o ya lo mando este proceso o el fallback de la nube)

    lineas = []
    for ev in pendientes:
        if ev["todo_el_dia"]:
            lineas.append(f"• {ev['titulo']} (todo el dia)")
        else:
            lineas.append(f"• {ev['titulo']} a las {google_services.formatear_hora_legible(ev['inicio'])}")

    try:
        await context.bot.send_message(
            chat_id=ALLOWED_CHAT_ID, text="📅 Recordatorio: esto tienes manana:\n" + "\n".join(lineas)
        )
        logger.info("Aviso de eventos de manana enviado (%d evento(s)).", len(pendientes))
    except Exception:
        logger.exception("Fallo enviando el aviso de eventos de manana.")
        return

    for ev in pendientes:
        try:
            await google_services.marcar_evento_recordatorio(ev["id"], "avisoManana", manana_iso)
        except Exception:
            logger.exception("Fallo marcando avisoManana en el evento '%s'.", ev["id"])


async def recordatorio_eventos_2h(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.wait_for(_recordatorio_eventos_2h_impl(context), timeout=TIMEOUT_JOB_EVENTOS_2H)
    except asyncio.TimeoutError:
        logger.error("recordatorio_eventos_2h: timeout (%ss), se salta esta ejecucion.", TIMEOUT_JOB_EVENTOS_2H)


async def _recordatorio_eventos_2h_impl(context: ContextTypes.DEFAULT_TYPE) -> None:
    if ALLOWED_CHAT_ID is None:
        logger.warning("ALLOWED_CHAT_ID no configurado; salto el recordatorio de eventos 2h antes.")
        return

    ahora = datetime.now(ZONA_HORARIA)
    eventos = await google_services.listar_eventos_estructurados(
        desde_iso=ahora.isoformat(), hasta_iso=(ahora + VENTANA_RECORDATORIO_2H).isoformat()
    )

    for ev in eventos:
        if ev["todo_el_dia"] or ev["aviso_2h"]:
            continue
        try:
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"⏰ En 2 horas: {ev['titulo']} ({google_services.formatear_hora_legible(ev['inicio'])})",
            )
            await google_services.marcar_evento_recordatorio(ev["id"], "aviso2h", "1")
        except Exception:
            logger.exception("Fallo enviando el recordatorio de 2h antes para el evento '%s'.", ev.get("id"))


async def _ejecutar_iniciativa(ini: dict) -> str:
    """Arma el mensaje a mandar para una iniciativa vencida, segun su tipo."""
    if ini["tipo_accion"] == "ideas_regalo":
        prompt = (
            f"[Iniciativa proactiva programada] Se acerca: {ini['descripcion']}. "
            f"Contexto guardado: {ini['contexto'] or 'sin detalles adicionales'}. "
            "Genera exactamente 10 ideas de regalo concretas y variadas (usa buscar_en_internet "
            "si te ayuda a dar opciones reales, con precio aproximado si es posible), en una lista "
            "corta y numerada. No preguntes nada, esto no es una conversacion en curso."
        )
        respuesta = await brain.ask(ini["chat_id"], prompt)
        return f"💡 {ini['descripcion']} — se me ocurrieron estas ideas:\n{respuesta}"

    # recordatorio_generico (y cualquier tipo futuro sin manejo especifico todavia)
    cuerpo = f"\n{ini['contexto']}" if ini["contexto"] else ""
    return f"📌 {ini['descripcion']}{cuerpo}"


async def revisar_iniciativas_proactivas(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.wait_for(_revisar_iniciativas_proactivas_impl(context), timeout=TIMEOUT_JOB_INICIATIVAS)
    except asyncio.TimeoutError:
        logger.error("revisar_iniciativas_proactivas: timeout (%ss), se salta esta ejecucion.", TIMEOUT_JOB_INICIATIVAS)


async def _revisar_iniciativas_proactivas_impl(context: ContextTypes.DEFAULT_TYPE) -> None:
    pendientes = await google_services.calcular_iniciativas_pendientes()
    for ini in pendientes:
        try:
            texto = await _ejecutar_iniciativa(ini)
            await context.bot.send_message(chat_id=ini["chat_id"], text=texto)
            await google_services.marcar_iniciativa_estado(ini["fila"], "enviado")
        except Exception:
            logger.exception("Fallo ejecutando la iniciativa '%s'.", ini.get("id"))


async def limpiar_iniciativas_antiguas_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.wait_for(
            _limpiar_iniciativas_antiguas_impl(context), timeout=TIMEOUT_JOB_LIMPIEZA_INICIATIVAS
        )
    except asyncio.TimeoutError:
        logger.error(
            "limpiar_iniciativas_antiguas_job: timeout (%ss), se salta esta ejecucion.",
            TIMEOUT_JOB_LIMPIEZA_INICIATIVAS,
        )


async def _limpiar_iniciativas_antiguas_impl(context: ContextTypes.DEFAULT_TYPE) -> None:
    estado = _leer_estado_jobs()
    ultima = estado.get("ultima_limpieza_iniciativas")
    hoy = datetime.now(ZONA_HORARIA).date()
    if ultima and (hoy - date.fromisoformat(ultima)).days < DIAS_ENTRE_LIMPIEZAS_INICIATIVAS:
        return  # ya se limpio hace menos de un mes

    borradas = await google_services.limpiar_iniciativas_antiguas()
    if borradas:
        logger.info("Limpieza de iniciativas: %d fila(s) borrada(s).", borradas)
    estado["ultima_limpieza_iniciativas"] = hoy.isoformat()
    _guardar_estado_jobs(estado)


def _morir_para_que_el_watchdog_reinicie(motivo: str) -> None:
    """Termina el proceso para que run_watchdog.bat lo relance limpio.

    Se usa cuando el proceso sigue vivo pero dejo de servir para algo, que en la
    practica significa "el polling de Telegram se cayo". Quedarse vivo en ese
    estado es PEOR que morir: el watchdog solo comprueba que exista un proceso
    main.py, asi que un bot sordo no se recupera nunca, mientras que una caida
    limpia se arregla sola en menos de 5 minutos.

    os._exit y no sys.exit: sys.exit lanza SystemExit, que el loop de asyncio de
    PTB atrapa y trata como un error mas del job, dejando el proceso vivo --
    justo lo que se quiere evitar.
    """
    logger.critical("Terminando el proceso: %s. El watchdog lo reiniciara.", motivo)
    logging.shutdown()  # vacia los handlers: os._exit no ejecuta los atexit
    os._exit(1)


async def heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Latido periodico que ademas comprueba que el bot siga ESCUCHANDO.

    Loguear "proceso vivo" no probaba nada: el updater puede haberse caido (por
    ejemplo tras un Conflict) dejando el proceso corriendo, los jobs de
    recordatorios andando y el bot completamente sordo en Telegram. Aca se mira
    el updater de verdad, y se deja constancia en disco para que el watchdog
    pueda detectar ademas un proceso congelado (que no llegaria ni a escribir).
    """
    updater = context.application.updater
    if updater is None or not updater.running:
        if (datetime.now(ZONA_HORARIA) - _ARRANQUE).total_seconds() < GRACIA_ARRANQUE_SEG:
            logger.warning("heartbeat: updater aun no arranca, dentro del margen de gracia.")
            return
        _morir_para_que_el_watchdog_reinicie("el polling de Telegram esta caido")
        return

    try:
        LATIDO_PATH.write_text(
            json.dumps({"ts": datetime.now(ZONA_HORARIA).isoformat(), "pid": os.getpid()}),
            encoding="utf-8",
        )
    except OSError:
        # Un fallo de escritura no justifica matar al bot: el polling esta vivo
        # y atendiendo, que es lo que importa. Solo se pierde la senal al watchdog.
        logger.exception("No se pudo escribir el latido en %s.", LATIDO_PATH)

    logger.info("heartbeat: proceso vivo y polling activo.")


async def _con_reintento_red(coro_fn, intentos: int = 2, espera_seg: float = 2.0):
    """Reintenta una llamada de red (Sheets/Calendar) ante cuelgues transitorios
    (DNS, conexion reiniciada), para que un boton de Telegram no quede
    respondiendo con un error silencioso por un hiccup puntual."""
    ultimo_error = None
    for intento in range(intentos):
        try:
            return await coro_fn()
        except (OSError, TimeoutError) as exc:
            ultimo_error = exc
            logger.warning("Reintento %d/%d tras error de red: %s", intento + 1, intentos, exc)
            if intento < intentos - 1:
                await asyncio.sleep(espera_seg)
    raise ultimo_error


async def confirmar_medicamento_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_chat_allowed(update):
        await query.answer()
        return

    _, id_medicamento, _fecha = query.data.split(":")
    remitente = query.from_user.first_name if query.from_user else ""

    try:
        resultado = await _con_reintento_red(
            lambda: google_services.confirmar_medicamento(id=id_medicamento, confirmado_por=remitente)
        )
    except Exception:
        logger.exception("Fallo confirmando el medicamento '%s' tras reintentos.", id_medicamento)
        await query.answer(
            text="No pude guardar la confirmacion (fallo de red). Intenta de nuevo en un momento.",
            show_alert=True,
        )
        return

    ahora = datetime.now(ZONA_HORARIA).strftime("%d/%m %H:%M")
    try:
        await query.answer(text=resultado[:200])
        await query.edit_message_text(
            text=f"{query.message.text}\n\n✅ Confirmado por {remitente}, {ahora}",
            reply_markup=None,
        )
    except Exception:
        logger.exception(
            "Confirmacion de '%s' guardada, pero fallo actualizando el mensaje en Telegram.", id_medicamento
        )


async def confirmar_habito_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_chat_allowed(update):
        await query.answer()
        return

    _, id_habito, _fecha = query.data.split(":")
    remitente = query.from_user.first_name if query.from_user else ""

    try:
        resultado = await _con_reintento_red(
            lambda: google_services.confirmar_habito(id=id_habito, confirmado_por=remitente)
        )
    except Exception:
        logger.exception("Fallo confirmando el habito '%s' tras reintentos.", id_habito)
        await query.answer(
            text="No pude guardar la confirmacion (fallo de red). Intenta de nuevo en un momento.",
            show_alert=True,
        )
        return

    ahora = datetime.now(ZONA_HORARIA).strftime("%d/%m %H:%M")
    try:
        await query.answer(text=resultado[:200])
        await query.edit_message_text(
            text=f"{query.message.text}\n\n✅ Confirmado por {remitente}, {ahora}",
            reply_markup=None,
        )
    except Exception:
        logger.exception(
            "Confirmacion de '%s' guardada, pero fallo actualizando el mensaje en Telegram.", id_habito
        )


async def confirmar_pago_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_chat_allowed(update):
        await query.answer()
        return

    _, id_pago, yyyymm = query.data.split(":")
    anio_mes = f"{yyyymm[:4]}-{yyyymm[4:]}"
    remitente = query.from_user.first_name if query.from_user else ""

    try:
        resultado = await _con_reintento_red(
            lambda: google_services.confirmar_pago_recurrente(
                id=id_pago, anio_mes=anio_mes, confirmado_por=remitente
            )
        )
    except Exception:
        logger.exception("Fallo confirmando el pago '%s' tras reintentos.", id_pago)
        await query.answer(
            text="No pude guardar la confirmacion (fallo de red). Intenta de nuevo en un momento.",
            show_alert=True,
        )
        return

    ahora = datetime.now(ZONA_HORARIA).strftime("%d/%m %H:%M")
    try:
        await query.answer(text=resultado[:200])
        await query.edit_message_text(
            text=f"{query.message.text}\n\n✅ {resultado} — {remitente}, {ahora}",
            reply_markup=None,
        )
    except Exception:
        logger.exception(
            "Confirmacion del pago '%s' guardada, pero fallo actualizando el mensaje en Telegram.", id_pago
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Conflict significa que otra peticion getUpdates se quedo con el polling de
    # este token (otra instancia de main.py, o una llamada manual a la API). PTB
    # da por terminado el loop del updater pero NO mata el proceso, o sea que el
    # bot queda vivo y sordo para siempre. Morir es la respuesta correcta: si la
    # otra instancia es legitima ella sigue atendiendo, y si no lo era, el
    # watchdog relanza esta con el polling limpio.
    if isinstance(context.error, Conflict):
        _morir_para_que_el_watchdog_reinicie(
            "Telegram devolvio Conflict (otra peticion getUpdates tomo el polling)"
        )
        return

    logger.error("Excepcion no manejada procesando un update.", exc_info=context.error)
    logger.error("Update que origino el error: %s", update)


async def _post_init(application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Verificar que Espartaco este activo"),
            BotCommand("menu", "Ver las funciones disponibles"),
            BotCommand("reset", "Borrar el historial de esta conversacion"),
        ]
    )


def main() -> None:
    logger.info("Iniciando Espartaco...")
    brain.configure(
        OMNIROUTE_API_KEY,
        OMNIROUTE_MODEL,
        base_url=OMNIROUTE_BASE_URL,
        transcription_model=OMNIROUTE_TRANSCRIPTION_MODEL,
        vision_model=OMNIROUTE_VISION_MODEL,
    )

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(CallbackQueryHandler(confirmar_pago_callback, pattern=r"^pago:"))
    application.add_handler(CallbackQueryHandler(confirmar_medicamento_callback, pattern=r"^medicamento:"))
    application.add_handler(CallbackQueryHandler(confirmar_habito_callback, pattern=r"^habito:"))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    application.add_handler(CallbackQueryHandler(imagen_callback, pattern=r"^imagen:"))
    application.add_error_handler(error_handler)

    # Jobs de UNA hora fija al dia: se disparan justo a esa hora (sin poll de
    # fondo). Si el PC estuvo apagado justo entonces, _revisar_pendientes_por_actividad
    # (disparada por cualquier mensaje real en el chat) hace de respaldo y los
    # manda atrasados en cuanto alguien escribe -- ver handle_message.
    application.job_queue.run_daily(alerta_calendario_diaria, time=HORA_ALERTA_CALENDARIO)
    logger.info(
        "Alerta diaria de calendario programada a las %s (sin poll; respaldo via actividad en el chat).",
        HORA_ALERTA_CALENDARIO,
    )

    application.job_queue.run_daily(recordatorio_pagos_diario, time=HORA_ALERTA_PAGOS)
    logger.info(
        "Recordatorio diario de pagos recurrentes programado a las %s (sin poll; respaldo via actividad en el chat).",
        HORA_ALERTA_PAGOS,
    )

    application.job_queue.run_daily(recordatorio_eventos_manana, time=HORA_ALERTA_EVENTOS_MANANA)
    logger.info(
        "Aviso de eventos de manana programado a las %s (sin poll; respaldo via actividad en el chat).",
        HORA_ALERTA_EVENTOS_MANANA,
    )

    # Jobs sin una sola hora fija (varias horas por fila, o fecha arbitraria):
    # se mantienen en poll, pero mucho menos seguido que antes (cada 5 min ->
    # cada 25 min) -- igual con respaldo inmediato via actividad en el chat.
    application.job_queue.run_repeating(recordatorio_medicamentos, interval=INTERVALO_POLL_VARIABLE, first=30)
    logger.info("Recordatorio de medicamentos programado (revision cada %d min, con catch-up).", INTERVALO_POLL_VARIABLE // 60)

    application.job_queue.run_repeating(recordatorio_habitos, interval=INTERVALO_POLL_VARIABLE, first=35)
    logger.info("Recordatorio de habitos de salud/bienestar programado (revision cada %d min, con catch-up).", INTERVALO_POLL_VARIABLE // 60)

    application.job_queue.run_repeating(recordatorio_eventos_2h, interval=INTERVALO_POLL_VARIABLE, first=60)
    logger.info("Recordatorio de eventos 2h antes programado (revision cada %d min).", INTERVALO_POLL_VARIABLE // 60)

    application.job_queue.run_repeating(revisar_iniciativas_proactivas, interval=INTERVALO_POLL_VARIABLE, first=70)
    logger.info("Revision de iniciativas proactivas programada (cada %d min).", INTERVALO_POLL_VARIABLE // 60)

    application.job_queue.run_repeating(limpiar_iniciativas_antiguas_job, interval=300, first=80)
    logger.info(
        "Limpieza de iniciativas antiguas programada (revision cada 5 min, corre cada %d dias).",
        DIAS_ENTRE_LIMPIEZAS_INICIATIVAS,
    )

    application.job_queue.run_repeating(heartbeat, interval=300, first=40)

    logger.info("Bot configurado. Iniciando polling...")
    # drop_pending_updates=False: al reconectar despues de estar apagado, procesa los
    # mensajes/confirmaciones que Telegram acumulo mientras tanto (hasta 24h), en vez
    # de descartarlos -- asi el bot "se pone al dia" con lo que paso en el chat.
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
