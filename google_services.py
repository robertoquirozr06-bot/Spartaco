"""
google_services.py - Herramientas de Espartaco sobre Calendar, Tasks y Sheets (Fase 4).

Cada funcion publica esta pensada para ser usada como "tool" de function
calling de Gemini: firma tipada + docstring en estilo Google, que es lo que
el SDK google-genai envia como descripcion de la funcion al modelo.

Las llamadas bloqueantes a googleapiclient corren en un hilo aparte
(asyncio.to_thread) para no congelar el loop de Telegram mientras esperan
respuesta de la API de Google.
"""
import asyncio
import logging
import os
import re
from calendar import monthrange as _monthrange
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google_auth import get_credentials

logger = logging.getLogger("espartaco")

CALENDAR_ID = os.getenv("CALENDAR_ID", "primary").strip() or "primary"
TASKLIST_ID = os.getenv("TASKLIST_ID", "").strip()
FINANCE_SHEET_ID = os.getenv("FINANCE_SHEET_ID", "").strip()
FINANCE_SHEET_TAB = os.getenv("FINANCE_SHEET_TAB", "Sheet1").strip() or "Sheet1"
PAGOS_RECURRENTES_TAB = os.getenv("PAGOS_RECURRENTES_TAB", "Pagos Recurrentes").strip() or "Pagos Recurrentes"
MEDICAMENTOS_TAB = os.getenv("MEDICAMENTOS_TAB", "Medicamentos").strip() or "Medicamentos"
NOTAS_TAB = os.getenv("NOTAS_TAB", "Notas").strip() or "Notas"
INICIATIVAS_TAB = os.getenv("INICIATIVAS_TAB", "Iniciativas").strip() or "Iniciativas"
HABITOS_TAB = os.getenv("HABITOS_TAB", "Habitos").strip() or "Habitos"

# "borrador_email" se agrega en la Fase 4 (integracion de Gmail) -- todavia no
# hay ninguna herramienta que lo ejecute, asi que no se ofrece como opcion.
TIPOS_INICIATIVA = ("ideas_regalo", "recordatorio_generico")
DIAS_RETENCION_INICIATIVAS = 90

ETIQUETAS_TAREA = ("[Agente]", "[Persona 1]", "[Persona 2]")
ZONA_HORARIA = ZoneInfo("America/Bogota")

# El job que detecta medicamentos/habitos pendientes ahora revisa cada 25 min
# (antes cada 5 min, ver main.py TIMEOUT/interval), asi que el umbral de
# "atrasado" tiene que ser bastante mayor a ese intervalo -- si no, una toma a
# tiempo detectada 20-25 min despues (por el propio ciclo del poll, no por
# retraso real) se marcaria como atrasada por error.
MINUTOS_ATRASO_UMBRAL = 60

_calendar = None
_tasks = None
_sheets = None

_DIAS_SEMANA = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def formatear_hora_legible(momento: datetime) -> str:
    """Hora en formato de 12h ('3:00 p.m.'), sin depender de que el sistema
    tenga instalado el locale es-CO (en Windows no siempre esta disponible)."""
    hora12 = momento.strftime("%I:%M").lstrip("0") or "12:00"
    return f"{hora12} {'a.m.' if momento.hour < 12 else 'p.m.'}"


def _etiqueta_dia(momento: datetime, referencia: date | None = None) -> str:
    """'hoy', 'mañana', o 'miercoles 10 de septiembre' segun que tan lejos
    este el dia de la referencia (por defecto, hoy)."""
    referencia = referencia or datetime.now(ZONA_HORARIA).date()
    dia = momento.date()
    if dia == referencia:
        return "hoy"
    if dia == referencia + timedelta(days=1):
        return "mañana"
    return f"{_DIAS_SEMANA[dia.weekday()]} {dia.day} de {_MESES[dia.month - 1]}"


def formatear_momento_legible(momento: datetime, todo_el_dia: bool = False, referencia: date | None = None) -> str:
    """Fecha/hora legible en español para mostrarle al usuario, ej.
    'mañana, 3:00 p.m.' o 'sabado 12 de septiembre (todo el dia)'."""
    dia = _etiqueta_dia(momento, referencia)
    if todo_el_dia:
        return f"{dia} (todo el dia)"
    return f"{dia}, {formatear_hora_legible(momento)}"


def _authorized_http(creds):
    """httplib2.Http() sin timeout puede colgarse indefinidamente un socket
    durante un corte de red, agotando el ThreadPoolExecutor compartido de
    asyncio.to_thread. 15s cubre con margen una lectura/escritura normal de
    Sheets/Calendar/Tasks (tipicamente <2s)."""
    return AuthorizedHttp(creds, http=httplib2.Http(timeout=15))


def _client_apis():
    """Construye (una sola vez) los clientes de Calendar/Tasks/Sheets."""
    global _calendar, _tasks, _sheets
    if _calendar is None:
        creds = get_credentials()
        _calendar = build("calendar", "v3", http=_authorized_http(creds))
        _tasks = build("tasks", "v1", http=_authorized_http(creds))
        _sheets = build("sheets", "v4", http=_authorized_http(creds))
    return _calendar, _tasks, _sheets


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def _insertar_evento_sync(calendar, body: dict) -> dict:
    # Sin num_retries: un insert reintentado a ciegas podria crear el evento
    # duplicado si la escritura anterior si llego a aplicarse.
    return calendar.events().insert(calendarId=CALENDAR_ID, body=body).execute()


async def crear_evento(
    titulo: str, inicio_iso: str, fin_iso: str, descripcion: str = "", recurrencia: list[str] | None = None
) -> str:
    """Crea un evento en el calendario del grupo.

    Args:
      titulo: Titulo corto del evento.
      inicio_iso: Fecha y hora de inicio en formato ISO 8601 con zona horaria,
        por ejemplo '2026-09-10T15:00:00-05:00'.
      fin_iso: Fecha y hora de fin, mismo formato que inicio_iso.
      descripcion: Notas adicionales del evento (opcional).
      recurrencia: Reglas RRULE de la API de Calendar (opcional), por ejemplo
        ['RRULE:FREQ=MONTHLY;BYMONTHDAY=5'] para repetir cada mes el dia 5.
        No se expone al modelo conversacional, solo para uso interno.

    Returns:
      Texto confirmando la creacion del evento, con el link para verlo.
    """
    calendar, _, _ = _client_apis()
    body = {
        "summary": titulo,
        "description": descripcion,
        "start": {"dateTime": inicio_iso},
        "end": {"dateTime": fin_iso},
    }
    if recurrencia:
        body["recurrence"] = recurrencia

    evento = await asyncio.to_thread(_insertar_evento_sync, calendar, body)
    logger.info("Evento creado: %r (%s)", titulo, evento.get("id"))
    cuando = formatear_momento_legible(datetime.fromisoformat(inicio_iso))
    return f"Evento '{titulo}' creado para {cuando}. Link: {evento.get('htmlLink')}"


async def listar_eventos(desde_iso: str = "", hasta_iso: str = "") -> str:
    """Lista los eventos del calendario del grupo en un rango de fechas.

    Args:
      desde_iso: Fecha/hora ISO 8601 desde la que buscar. Si se deja vacio,
        se usa el momento actual.
      hasta_iso: Fecha/hora ISO 8601 hasta la que buscar. Si se deja vacio,
        se usan los 7 dias siguientes a desde_iso.

    Returns:
      Texto con la lista de eventos encontrados (titulo y horario), o un
      aviso si no hay eventos en ese rango.
    """
    inicio = datetime.fromisoformat(desde_iso) if desde_iso else datetime.now().astimezone()
    fin = datetime.fromisoformat(hasta_iso) if hasta_iso else inicio + timedelta(days=7)

    eventos = await listar_eventos_estructurados(inicio.isoformat(), fin.isoformat())
    if not eventos:
        return "No hay eventos en ese rango de fechas."
    referencia = inicio.date()
    lineas = []
    for ev in eventos:
        cuando = formatear_momento_legible(ev["inicio"], ev["todo_el_dia"], referencia)
        lineas.append(f"• {ev['titulo']} — {cuando}")
    return "\n".join(lineas)


async def listar_eventos_estructurados(desde_iso: str, hasta_iso: str) -> list[dict]:
    """Version estructurada de listar_eventos: en vez de texto, devuelve por
    cada evento su id, titulo, inicio (datetime con zona horaria), si es de
    todo el dia, y los flags de recordatorio (avisoManana/aviso2h) ya
    guardados en extendedProperties.private del evento.

    Uso interno de los jobs de recordatorios en main.py -- no se registra
    como tool del modelo conversacional.
    """
    calendar, _, _ = _client_apis()
    inicio = datetime.fromisoformat(desde_iso)
    fin = datetime.fromisoformat(hasta_iso)

    def _listar():
        return calendar.events().list(
            calendarId=CALENDAR_ID,
            timeMin=inicio.isoformat(),
            timeMax=fin.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute(num_retries=2)

    resultado = await asyncio.to_thread(_listar)
    eventos = []
    for ev in resultado.get("items", []):
        inicio_raw = ev["start"].get("dateTime")
        todo_el_dia = inicio_raw is None
        if todo_el_dia:
            inicio_evento = datetime.combine(
                date.fromisoformat(ev["start"]["date"]), dt_time.min, tzinfo=ZONA_HORARIA
            )
        else:
            inicio_evento = datetime.fromisoformat(inicio_raw)
        propiedades = ev.get("extendedProperties", {}).get("private", {})
        eventos.append({
            "id": ev["id"],
            "titulo": ev.get("summary", "(sin titulo)"),
            "inicio": inicio_evento,
            "todo_el_dia": todo_el_dia,
            "aviso_manana": propiedades.get("avisoManana"),
            "aviso_2h": propiedades.get("aviso2h") == "1",
        })
    return eventos


async def marcar_evento_recordatorio(event_id: str, campo: str, valor: str) -> None:
    """Marca un flag de recordatorio ("avisoManana" o "aviso2h") en
    extendedProperties.private del evento, mezclando con lo que ya hubiera
    (read-modify-write) para no pisar el otro flag.

    Uso interno de los jobs de main.py -- no se registra como tool del
    modelo conversacional. El fallback en la nube (Apps Script) marca el
    mismo campo sobre el mismo evento, asi que cualquiera de los dos lados
    que corra primero evita que el otro reenvie el aviso.
    """
    calendar, _, _ = _client_apis()

    def _actualizar():
        evento = calendar.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute(num_retries=2)
        propiedades = dict(evento.get("extendedProperties", {}).get("private", {}))
        propiedades[campo] = valor
        calendar.events().patch(
            calendarId=CALENDAR_ID,
            eventId=event_id,
            body={"extendedProperties": {"private": propiedades}},
        ).execute(num_retries=2)

    await asyncio.to_thread(_actualizar)


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

async def crear_tarea(
    titulo: str, etiqueta: str, notas: str = "", vencimiento_iso: str = "", proyecto: str = ""
) -> str:
    """Crea una tarea en la lista dedicada de Espartaco (no la lista personal).

    Args:
      titulo: Descripcion corta de la tarea, sin incluir la etiqueta.
      etiqueta: A quien corresponde. Debe ser exactamente uno de: '[Agente]'
        (Espartaco la ejecuta solo), '[Persona 1]' o '[Persona 2]' (miembros
        del equipo).
      notas: Detalles adicionales de la tarea (opcional).
      vencimiento_iso: Fecha limite en formato ISO 8601, solo fecha, por
        ejemplo '2026-09-10' (opcional).
      proyecto: Si esta tarea es parte de un proyecto personal con varios
        pasos (ej. 'Viaje a Cartagena'), agrupala aca para poder consultar el
        avance despues con resumen_proyecto. Dejar vacio si es una tarea
        suelta, sin proyecto.

    Returns:
      Texto confirmando la creacion de la tarea.
    """
    if etiqueta not in ETIQUETAS_TAREA:
        return f"Etiqueta invalida '{etiqueta}'. Debe ser una de: {', '.join(ETIQUETAS_TAREA)}."
    if not TASKLIST_ID:
        return "No hay una lista de tareas configurada todavia (falta TASKLIST_ID en .env)."

    _, tasks, _ = _client_apis()
    prefijo_proyecto = f" [Proyecto: {proyecto}]" if proyecto else ""
    titulo_completo = f"{etiqueta}{prefijo_proyecto} {titulo}"

    def _insertar():
        body = {"title": titulo_completo, "notes": notas}
        if vencimiento_iso:
            body["due"] = f"{vencimiento_iso}T00:00:00.000Z"
        # Sin num_retries, mismo motivo que en _insertar_evento_sync: podria duplicar la tarea.
        return tasks.tasks().insert(tasklist=TASKLIST_ID, body=body).execute()

    tarea = await asyncio.to_thread(_insertar)
    logger.info("Tarea creada: %r (%s)", titulo_completo, tarea.get("id"))
    return f"Tarea creada: {titulo_completo}"


async def listar_tareas(etiqueta: str = "", proyecto: str = "") -> str:
    """Lista las tareas pendientes (sin completar) de la lista de Espartaco.

    Args:
      etiqueta: Si se especifica ('[Agente]', '[Persona 1]' o '[Persona 2]'),
        solo lista las tareas de esa etiqueta. Si se deja vacio, lista todas.
      proyecto: Si se especifica, solo lista las tareas de ese proyecto (tal
        como se uso en crear_tarea). Se puede combinar con etiqueta.

    Returns:
      Texto con las tareas pendientes, incluyendo su id interno (necesario
      para completarlas despues), o un aviso si no hay tareas.
    """
    if not TASKLIST_ID:
        return "No hay una lista de tareas configurada todavia (falta TASKLIST_ID en .env)."

    _, tasks, _ = _client_apis()

    def _listar():
        return tasks.tasks().list(tasklist=TASKLIST_ID, showCompleted=False).execute(num_retries=2)

    resultado = await asyncio.to_thread(_listar)
    items = resultado.get("items", [])
    if etiqueta:
        items = [t for t in items if t.get("title", "").startswith(etiqueta)]
    if proyecto:
        tag = f"[proyecto: {proyecto}]".lower()
        items = [t for t in items if tag in t.get("title", "").lower()]
    if not items:
        return "No hay tareas pendientes."

    def _mostrar(t: dict) -> str:
        titulo = t.get("title", "")
        if proyecto:
            # Ya se filtro por este proyecto: repetir el tag en cada linea
            # solo ensucia la lista, no agrega informacion nueva.
            titulo = _quitar_tag_proyecto(titulo, proyecto)
        return f"• {titulo} (id: {t['id']})"

    return "\n".join(_mostrar(t) for t in items)


def _quitar_tag_proyecto(titulo: str, proyecto: str) -> str:
    """Quita '[Proyecto: X]' del titulo de una tarea para no repetirlo en
    cada linea cuando ya se esta listando/resumiendo ese proyecto puntual."""
    patron = re.compile(r"\[proyecto:\s*" + re.escape(proyecto) + r"\]\s*", re.IGNORECASE)
    return patron.sub("", titulo).strip()


async def resumen_proyecto(proyecto: str) -> str:
    """Resume el avance de un proyecto personal (tareas creadas con ese
    proyecto en crear_tarea/proponer_plan): cuantas van completadas y cuales
    quedan pendientes.

    Args:
      proyecto: Nombre del proyecto, tal como se uso al crear las tareas
        (ej. 'Viaje a Cartagena').

    Returns:
      Texto con el conteo de avance y el detalle de lo pendiente, o un aviso
      si no hay tareas para ese proyecto.
    """
    if not TASKLIST_ID:
        return "No hay una lista de tareas configurada todavia (falta TASKLIST_ID en .env)."

    _, tasks, _ = _client_apis()
    tag = f"[proyecto: {proyecto}]".lower()

    def _listar():
        # showCompleted+showHidden=True: sin esto, Google Tasks puede ocultar
        # tareas ya completadas y el resumen subestimaria el avance real.
        return tasks.tasks().list(
            tasklist=TASKLIST_ID, showCompleted=True, showHidden=True
        ).execute(num_retries=2)

    resultado = await asyncio.to_thread(_listar)
    items = [t for t in resultado.get("items", []) if tag in t.get("title", "").lower()]
    if not items:
        return f"No encontre tareas para el proyecto '{proyecto}'."

    completadas = [t for t in items if t.get("status") == "completed"]
    pendientes = [t for t in items if t.get("status") != "completed"]
    lineas = [f"📁 {proyecto} — {len(completadas)}/{len(items)} tareas completadas"]
    if pendientes:
        lineas.append("")
        lineas.append("Pendientes:")
        lineas.extend(f"• {_quitar_tag_proyecto(t.get('title', ''), proyecto)}" for t in pendientes)
    return "\n".join(lineas)


async def completar_tarea(tarea_id: str) -> str:
    """Marca una tarea de la lista de Espartaco como completada.

    Args:
      tarea_id: El id interno de la tarea, tal como lo devuelve listar_tareas.

    Returns:
      Texto confirmando que la tarea se marco como completada, o un aviso
      si no se encontro esa tarea.
    """
    if not TASKLIST_ID:
        return "No hay una lista de tareas configurada todavia (falta TASKLIST_ID en .env)."

    _, tasks, _ = _client_apis()

    def _completar():
        return tasks.tasks().patch(
            tasklist=TASKLIST_ID, task=tarea_id, body={"status": "completed"}
        ).execute(num_retries=2)

    try:
        tarea = await asyncio.to_thread(_completar)
    except Exception:
        logger.exception("No se pudo completar la tarea %s", tarea_id)
        return "No encontre esa tarea. Revisa el id con listar_tareas."
    return f"Tarea completada: {tarea.get('title')}"


# --------------------------------------------------------------------------
# Planes de tareas (freno de codigo para pedidos de varios pasos)
#
# proponer_plan solo guarda los pasos en memoria y los muestra -- nunca toca
# la API de Tasks. confirmar_plan es la unica funcion que realmente crea las
# tareas (reusando crear_tarea), y solo actua sobre lo que quedo guardado por
# proponer_plan. Asi un pedido con compromisos reales (ej. cotizar un
# carpintero) no puede terminar en tareas creadas sin que el usuario haya
# confirmado explicitamente, sin importar que tan seguro "suene" el modelo.
# --------------------------------------------------------------------------

_planes_pendientes: dict[int, list[dict]] = {}


async def proponer_plan(pasos: list[dict], chat_id: int) -> str:
    """Arma y muestra un plan de tareas propuesto, sin crear nada todavia.

    Args:
      pasos: Lista de pasos propuestos, cada uno con 'titulo' (str),
        'etiqueta' (una de '[Agente]', '[Persona 1]', '[Persona 2]'),
        'notas' (opcional), 'vencimiento_iso' (opcional, solo fecha ISO 8601)
        y 'proyecto' (opcional -- usa el mismo nombre en todos los pasos que
        pertenezcan al mismo proyecto personal, para poder consultar el
        avance despues con resumen_proyecto).
      chat_id: Id del chat de Telegram que pidio el plan (lo inyecta el
        dispatcher, no lo controla el modelo).

    Returns:
      El plan formateado en texto, listo para mostrarle al usuario y pedirle
      confirmacion, o un aviso si algun paso tiene una etiqueta invalida.
    """
    for paso in pasos:
        if paso.get("etiqueta") not in ETIQUETAS_TAREA:
            return (
                f"Etiqueta invalida '{paso.get('etiqueta')}' en el paso '{paso.get('titulo')}'. "
                f"Debe ser una de: {', '.join(ETIQUETAS_TAREA)}."
            )

    _planes_pendientes[chat_id] = pasos
    lineas = ["Plan propuesto (todavia no se creo nada):"]
    for i, paso in enumerate(pasos, start=1):
        proyecto = f" [Proyecto: {paso['proyecto']}]" if paso.get("proyecto") else ""
        notas = f" — {paso['notas']}" if paso.get("notas") else ""
        vencimiento = f" (vence {paso['vencimiento_iso']})" if paso.get("vencimiento_iso") else ""
        lineas.append(f"{i}. {paso['etiqueta']}{proyecto} {paso['titulo']}{notas}{vencimiento}")
    return "\n".join(lineas)


async def confirmar_plan(chat_id: int) -> str:
    """Crea de verdad, en Google Tasks, el ultimo plan propuesto para este chat.

    Args:
      chat_id: Id del chat de Telegram (lo inyecta el dispatcher).

    Returns:
      Texto confirmando cada tarea creada, o un aviso si no hay ningun plan
      pendiente para confirmar.
    """
    pasos = _planes_pendientes.pop(chat_id, None)
    if not pasos:
        return "No hay ningun plan pendiente para confirmar. Pidele que arme uno primero."

    resultados = []
    for paso in pasos:
        resultado = await crear_tarea(
            titulo=paso["titulo"],
            etiqueta=paso["etiqueta"],
            notas=paso.get("notas", ""),
            vencimiento_iso=paso.get("vencimiento_iso", ""),
            proyecto=paso.get("proyecto", ""),
        )
        resultados.append(resultado)
    return "\n".join(resultados)


async def cancelar_plan(chat_id: int) -> str:
    """Descarta el ultimo plan propuesto para este chat sin crear nada.

    Args:
      chat_id: Id del chat de Telegram (lo inyecta el dispatcher).

    Returns:
      Texto confirmando que se descarto, o un aviso si no habia ninguno.
    """
    if _planes_pendientes.pop(chat_id, None) is None:
        return "No habia ningun plan pendiente."
    return "Listo, descarte el plan. No se creo ninguna tarea."


# --------------------------------------------------------------------------
# Sheets (finanzas)
# --------------------------------------------------------------------------

async def registrar_movimiento(
    fecha_iso: str, descripcion: str, categoria: str, monto: float, tipo: str, registrado_por: str = ""
) -> str:
    """Registra un ingreso o gasto en la hoja de finanzas del grupo.

    Args:
      fecha_iso: Fecha del movimiento, formato ISO 8601 solo fecha, por
        ejemplo '2026-09-04'.
      descripcion: Breve descripcion del movimiento.
      categoria: Categoria del gasto o ingreso, por ejemplo 'Comida' o 'Sueldo'.
      monto: Monto del movimiento, siempre en positivo.
      tipo: Debe ser exactamente 'ingreso' o 'gasto'.
      registrado_por: Nombre de quien reporto el movimiento en el chat. Usa el
        nombre que te llega en el contexto del mensaje, no lo inventes.

    Returns:
      Texto confirmando el registro, o un aviso si la hoja no esta configurada.
    """
    if not FINANCE_SHEET_ID:
        return "No hay una hoja de finanzas configurada todavia (falta FINANCE_SHEET_ID en .env)."

    _, _, sheets = _client_apis()
    fila = [[fecha_iso, descripcion, categoria, monto, tipo, registrado_por]]

    def _agregar():
        # Sin num_retries: un append es "agregar fila", no "fijar un valor" -- reintentarlo
        # a ciegas podria duplicar el movimiento si el POST anterior si llego a escribir
        # pero se perdio la respuesta. Mejor fallar una vez y que el usuario reintente.
        return sheets.spreadsheets().values().append(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{FINANCE_SHEET_TAB}!A:F",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": fila},
        ).execute()

    await asyncio.to_thread(_agregar)
    logger.info("Movimiento registrado: %s %s en %s (por %s)", tipo, monto, categoria, registrado_por)
    return f"Registrado: {tipo} de {monto} en '{categoria}' ({descripcion})."


async def consultar_movimientos(categoria: str = "") -> str:
    """Consulta los movimientos financieros registrados en la hoja del grupo.

    Args:
      categoria: Si se especifica, solo devuelve movimientos de esa categoria
        (comparacion sin importar mayusculas/minusculas). Si se deja vacio,
        devuelve todos los movimientos.

    Returns:
      Texto con los movimientos encontrados y el balance total (ingresos
      menos gastos), o un aviso si la hoja no esta configurada o no hay datos.
    """
    if not FINANCE_SHEET_ID:
        return "No hay una hoja de finanzas configurada todavia (falta FINANCE_SHEET_ID en .env)."

    _, _, sheets = _client_apis()

    def _leer():
        return sheets.spreadsheets().values().get(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{FINANCE_SHEET_TAB}!A:F",
        ).execute(num_retries=2)

    resultado = await asyncio.to_thread(_leer)
    filas = resultado.get("values", [])[1:]  # la fila 1 son encabezados
    lineas = []
    balance = 0.0
    for fila in filas:
        if len(fila) < 5:
            continue
        fecha, descripcion, cat, monto, tipo = fila[:5]
        registrado_por = fila[5] if len(fila) > 5 else ""
        if categoria and cat.strip().lower() != categoria.strip().lower():
            continue
        try:
            monto_num = _parsear_monto(monto)
        except ValueError:
            continue
        balance += monto_num if tipo.strip().lower() == "ingreso" else -monto_num
        quien = f" | {registrado_por}" if registrado_por else ""
        lineas.append(f"- {fecha} | {descripcion} | {cat} | {tipo} | {monto}{quien}")

    if not lineas:
        return "No hay movimientos registrados para ese filtro."
    lineas.append(f"Balance total: {balance:.2f}")
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# Pagos recurrentes (recordatorios mensuales con confirmacion)
# --------------------------------------------------------------------------
# Columnas de la pestana "Pagos Recurrentes": ID, Descripcion, Monto,
# DiaDelMes, Categoria, Responsable, Activo, UltimoMesPagado,
# UltimoAvisoEnviado, CalendarEventId (A-J). Se parsea siempre por posicion
# de columna (nunca por encabezado) para que el usuario pueda editar la hoja
# a mano sin romper el parseo.

def _celda(fila: list, idx: int, default: str = "") -> str:
    return fila[idx] if idx < len(fila) and fila[idx] != "" else default


# Orden a proposito: primero el formato que la hoja usa hoy, despues el local
# (d/m/Y) que es lo que Sheets pondria si alguien reformatea la columna.
_FORMATOS_FECHA = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d")


def _celda_fecha_iso(fila: list, idx: int) -> str:
    """Lee una celda de fecha y la normaliza a 'YYYY-MM-DD'.

    Las columnas de control (UltimoAvisoEnviado / UltimaTomaConfirmada) se
    comparan contra la fecha de hoy en ISO para no repetir un aviso. Esa
    comparacion es texto contra texto, asi que depende del formato con que la
    hoja muestre la celda: hoy es 'yyyy-mm-dd' y calza, pero basta con que
    alguien reformatee la columna a d/m/aaaa para que deje de calzar y el aviso
    se repita en cada pasada del poll. Normalizar aca corta esa dependencia.
    """
    texto = str(_celda(fila, idx)).strip()
    if not texto:
        return ""
    for formato in _FORMATOS_FECHA:
        try:
            return datetime.strptime(texto, formato).date().isoformat()
        except ValueError:
            continue
    return texto


def _parsear_monto(valor) -> float:
    """Convierte a float un monto tal como lo teclea una persona en la hoja.

    Las celdas llegan en cualquier forma: '92280', '$78,101', '$1,200,000.00',
    '1500,50'. Antes se hacia un float(str(valor).replace(",", ".")) directo,
    que revienta con el simbolo de moneda y con los separadores de miles; como
    el ValueError descarta la fila entera, ningun pago recurrente con monto
    formateado llegaba a generar recordatorio.

    Con los dos separadores presentes, el de mas a la derecha es el decimal
    (da igual la convencion). Con uno solo, tres digitos detras significan
    separador de miles ('$78,101' son 78101 pesos, no 78.101).

    Raises:
      ValueError: si la celda no tiene ningun numero utilizable (vacia, 'na').
    """
    texto = re.sub(r"[^\d,.\-]", "", str(valor)).strip()
    if not texto:
        raise ValueError(f"monto sin digitos: {valor!r}")

    ultimo = max(texto.rfind(","), texto.rfind("."))
    if ultimo == -1:
        return float(texto)

    hay_ambos = "," in texto and "." in texto
    decimales = len(texto) - ultimo - 1
    if hay_ambos or decimales != 3:
        return float(f"{re.sub(r'[,.]', '', texto[:ultimo])}.{texto[ultimo + 1:]}")
    return float(re.sub(r"[,.]", "", texto))


def _fila_desde_rango(rango: str) -> int | None:
    """Extrae el numero de fila de un 'updatedRange' de Sheets, ej. "'Pagos Recurrentes'!A5:J5" -> 5."""
    if not rango:
        return None
    m = re.search(r"(\d+)", rango.split("!")[-1])
    return int(m.group(1)) if m else None


async def _leer_filas_pagos_recurrentes() -> tuple[list | None, str | None]:
    """Lee todas las filas (sin encabezado) de la pestana de pagos recurrentes.

    Devuelve (filas, None), o (None, aviso) si la hoja/pestana no esta
    configurada, para que las tools devuelvan un mensaje amigable en vez de
    propagar la excepcion.
    """
    if not FINANCE_SHEET_ID:
        return None, "No hay una hoja de finanzas configurada todavia (falta FINANCE_SHEET_ID en .env)."

    _, _, sheets = _client_apis()

    def _leer():
        return sheets.spreadsheets().values().get(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{PAGOS_RECURRENTES_TAB}!A:J",
        ).execute(num_retries=2)

    try:
        resultado = await asyncio.to_thread(_leer)
    except HttpError:
        logger.exception("No se pudo leer la pestana '%s'", PAGOS_RECURRENTES_TAB)
        return None, (
            f"No encontre la pestana '{PAGOS_RECURRENTES_TAB}' en la hoja de finanzas. "
            "Creala con encabezados (ID/Descripcion/Monto/DiaDelMes/Categoria/Responsable/"
            "Activo/UltimoMesPagado/UltimoAvisoEnviado/CalendarEventId) antes de usar pagos recurrentes."
        )
    return resultado.get("values", [])[1:], None


async def _crear_espejo_calendario(id: str, descripcion: str, dia_del_mes: int) -> str:
    """Crea (best-effort) un evento recurrente mensual en Calendar como espejo
    visual de un pago recurrente. Devuelve el event_id creado, o '' si falla."""
    calendar, _, _ = _client_apis()
    ahora = datetime.now(ZONA_HORARIA)
    anio, mes = ahora.year, ahora.month
    dia_efectivo = min(dia_del_mes, _monthrange(anio, mes)[1])
    primera_fecha = ahora.replace(day=dia_efectivo, hour=9, minute=0, second=0, microsecond=0)
    if primera_fecha < ahora:
        mes = mes % 12 + 1
        anio = anio + (1 if mes == 1 else 0)
        dia_efectivo = min(dia_del_mes, _monthrange(anio, mes)[1])
        primera_fecha = ahora.replace(year=anio, month=mes, day=dia_efectivo, hour=9, minute=0, second=0, microsecond=0)
    fin = primera_fecha + timedelta(minutes=30)

    body = {
        "summary": f"Pago: {descripcion}",
        "description": f"Pago recurrente ({id}), gestionado por Espartaco.",
        "start": {"dateTime": primera_fecha.isoformat()},
        "end": {"dateTime": fin.isoformat()},
        "recurrence": [f"RRULE:FREQ=MONTHLY;BYMONTHDAY={dia_del_mes}"],
    }
    evento = await asyncio.to_thread(_insertar_evento_sync, calendar, body)
    return evento.get("id", "")


async def agregar_pago_recurrente(
    id: str, descripcion: str, monto: float, dia_del_mes: int, categoria: str = "", responsable: str = ""
) -> str:
    """Registra un nuevo pago recurrente mensual (arriendo, suscripciones, etc.).

    Args:
      id: Identificador corto y unico, sin espacios ni ':', ej. 'netflix' o 'arriendo'.
      descripcion: Nombre visible del pago para los recordatorios.
      monto: Monto mensual del pago.
      dia_del_mes: Dia del mes en que vence (1-31). Si el mes tiene menos dias,
        se usa el ultimo dia real de ese mes.
      categoria: Categoria para el registro financiero al confirmar (opcional,
        usa descripcion si se deja vacio).
      responsable: Nombre de quien es responsable de este pago (opcional, solo informativo).

    Returns:
      Texto confirmando el registro, o un aviso si la pestana no esta configurada.
    """
    filas, aviso = await _leer_filas_pagos_recurrentes()
    if aviso:
        return aviso
    if not (1 <= dia_del_mes <= 31):
        return "El dia del mes debe estar entre 1 y 31."
    if any(_celda(f, 0).strip().lower() == id.strip().lower() for f in filas if f):
        return f"Ya existe un pago recurrente con el id '{id}'. Usa otro id o edita la fila existente en la hoja."

    _, _, sheets = _client_apis()
    fila_nueva = [id, descripcion, monto, dia_del_mes, categoria, responsable, "SI", "", "", ""]

    def _agregar():
        # Sin num_retries, mismo motivo que en registrar_movimiento: un append
        # reintentado a ciegas podria duplicar la fila.
        return sheets.spreadsheets().values().append(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{PAGOS_RECURRENTES_TAB}!A:J",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [fila_nueva]},
        ).execute()

    resultado = await asyncio.to_thread(_agregar)
    logger.info("Pago recurrente agregado: %s (%s, dia %s)", id, descripcion, dia_del_mes)

    try:
        fila_num = _fila_desde_rango(resultado.get("updates", {}).get("updatedRange", ""))
        event_id = await _crear_espejo_calendario(id, descripcion, dia_del_mes)
        if event_id and fila_num:
            def _guardar_event_id():
                sheets.spreadsheets().values().update(
                    spreadsheetId=FINANCE_SHEET_ID,
                    range=f"{PAGOS_RECURRENTES_TAB}!J{fila_num}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[event_id]]},
                ).execute(num_retries=2)
            await asyncio.to_thread(_guardar_event_id)
    except Exception:
        logger.exception("No se pudo crear el espejo de Calendar para el pago recurrente '%s'", id)

    return f"Pago recurrente '{descripcion}' creado, vence el dia {dia_del_mes} de cada mes."


async def listar_pagos_recurrentes(solo_activos: bool = True) -> str:
    """Lista los pagos recurrentes mensuales configurados.

    Args:
      solo_activos: Si es True (default), omite los pagos pausados (Activo='NO').

    Returns:
      Texto con la lista de pagos recurrentes, o un aviso si no hay ninguno
      o la pestana no esta configurada.
    """
    filas, aviso = await _leer_filas_pagos_recurrentes()
    if aviso:
        return aviso

    lineas = []
    for fila in filas:
        if not fila or not _celda(fila, 0):
            continue
        activo = _celda(fila, 6, "SI").strip().upper() != "NO"
        if solo_activos and not activo:
            continue
        id_, desc, monto, dia = _celda(fila, 0), _celda(fila, 1), _celda(fila, 2), _celda(fila, 3)
        responsable = _celda(fila, 5)
        estado = "" if activo else " (pausado)"
        quien = f" - {responsable}" if responsable else ""
        lineas.append(f"- ({id_}) {desc}: ${monto} el dia {dia} de cada mes{quien}{estado}")

    if not lineas:
        return "No hay pagos recurrentes configurados."
    return "\n".join(lineas)


async def calcular_recordatorios_pagos(fecha_referencia_iso: str = "") -> dict:
    """Calcula que pagos recurrentes vencen manana y cuales vencen hoy/estan atrasados.

    Uso interno del job diario de recordatorios de main.py -- no se registra
    como tool del modelo conversacional porque no debe invocarse fuera de ese
    contexto (podria disparar avisos duplicados).
    """
    filas, aviso = await _leer_filas_pagos_recurrentes()
    if aviso or not filas:
        return {"manana": [], "hoy": []}

    hoy = (
        datetime.fromisoformat(fecha_referencia_iso).date()
        if fecha_referencia_iso
        else datetime.now(ZONA_HORARIA).date()
    )
    manana = hoy + timedelta(days=1)
    mes_actual = hoy.strftime("%Y-%m")

    resultado = {"manana": [], "hoy": []}
    for idx, fila in enumerate(filas):
        if not fila or not _celda(fila, 0):
            continue
        activo = _celda(fila, 6, "SI").strip().upper() != "NO"
        if not activo:
            continue
        try:
            monto = _parsear_monto(_celda(fila, 2))
            dia_del_mes = int(_celda(fila, 3))
        except ValueError:
            logger.warning("Fila de pago recurrente con Monto/DiaDelMes invalido (fila %d): %r", idx + 2, fila)
            continue

        if not (1 <= dia_del_mes <= 31):
            logger.warning("Fila de pago recurrente con DiaDelMes fuera de rango (fila %d): %r", idx + 2, fila)
            continue

        id_ = _celda(fila, 0)
        descripcion = _celda(fila, 1) or id_
        categoria = _celda(fila, 4) or descripcion
        responsable = _celda(fila, 5)
        ultimo_mes_pagado = _celda(fila, 7)
        ultimo_aviso = _celda_fecha_iso(fila, 8)
        fila_num = idx + 2  # +1 por indice 0-based, +1 por la fila de encabezado

        if ultimo_mes_pagado == mes_actual:
            continue  # ya confirmado este mes
        if ultimo_aviso == hoy.isoformat():
            continue  # ya se aviso hoy (anti-duplicado del job diario)

        item = {
            "id": id_, "descripcion": descripcion, "monto": monto,
            "categoria": categoria, "responsable": responsable, "fila": fila_num,
        }

        dia_efectivo_manana = min(dia_del_mes, _monthrange(manana.year, manana.month)[1])
        if manana.day == dia_efectivo_manana:
            resultado["manana"].append(item)
            continue

        dia_efectivo_hoy = min(dia_del_mes, _monthrange(hoy.year, hoy.month)[1])
        if hoy.day >= dia_efectivo_hoy:
            resultado["hoy"].append({**item, "dias_atraso": hoy.day - dia_efectivo_hoy})

    return resultado


async def marcar_aviso_pago_enviado(fila: int, fecha_iso: str) -> None:
    """Marca (columna I) que ya se envio el aviso de un pago recurrente en esa fecha.

    Uso interno del job diario de main.py -- no se registra como tool del
    modelo conversacional.
    """
    _, _, sheets = _client_apis()

    def _actualizar():
        sheets.spreadsheets().values().update(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{PAGOS_RECURRENTES_TAB}!I{fila}",
            valueInputOption="USER_ENTERED",
            body={"values": [[fecha_iso]]},
        ).execute(num_retries=2)

    await asyncio.to_thread(_actualizar)


async def confirmar_pago_recurrente(id: str, anio_mes: str = "", confirmado_por: str = "") -> str:
    """Confirma que un pago recurrente del mes vigente ya fue pagado.

    Args:
      id: Identificador del pago recurrente, tal como aparece en listar_pagos_recurrentes.
      anio_mes: Mes a confirmar en formato 'YYYY-MM'. Si se deja vacio, usa el mes actual.
      confirmado_por: Nombre de quien confirma el pago (viene en el contexto del mensaje).

    Returns:
      Texto confirmando el registro, o un aviso si el pago no existe, ya
      estaba confirmado, o el mes indicado no es el mes vigente.
    """
    filas, aviso = await _leer_filas_pagos_recurrentes()
    if aviso:
        return aviso

    ahora = datetime.now(ZONA_HORARIA)
    mes_actual = ahora.strftime("%Y-%m")
    anio_mes = anio_mes.strip() or mes_actual
    if anio_mes != mes_actual:
        return "Ese aviso ya vencio (no corresponde al mes vigente). Revisa listar_pagos_recurrentes."

    fila_encontrada = None
    fila_num = None
    for idx, fila in enumerate(filas):
        if _celda(fila, 0).strip().lower() == id.strip().lower():
            fila_encontrada = fila
            fila_num = idx + 2
            break
    if fila_encontrada is None:
        return f"No encontre un pago recurrente con el id '{id}'."

    if _celda(fila_encontrada, 7) == mes_actual:
        return "Ese pago ya estaba confirmado este mes."

    descripcion = _celda(fila_encontrada, 1) or id
    try:
        monto = _parsear_monto(_celda(fila_encontrada, 2))
    except ValueError:
        monto = 0.0
    categoria = _celda(fila_encontrada, 4) or descripcion
    try:
        dia_del_mes = int(_celda(fila_encontrada, 3))
    except ValueError:
        dia_del_mes = ahora.day
    dia_efectivo = min(dia_del_mes, _monthrange(ahora.year, ahora.month)[1])
    fecha_vencimiento = ahora.replace(day=dia_efectivo).date().isoformat()

    _, _, sheets = _client_apis()

    def _marcar_pagado():
        sheets.spreadsheets().values().update(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{PAGOS_RECURRENTES_TAB}!H{fila_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [[mes_actual]]},
        ).execute(num_retries=2)

    await asyncio.to_thread(_marcar_pagado)
    await registrar_movimiento(
        fecha_iso=fecha_vencimiento,
        descripcion=f"{descripcion} (recurrente)",
        categoria=categoria,
        monto=monto,
        tipo="gasto",
        registrado_por=confirmado_por,
    )
    logger.info("Pago recurrente confirmado: %s (%s) por %s", id, mes_actual, confirmado_por)
    return f"Pago de '{descripcion}' confirmado para {mes_actual}."


async def desactivar_pago_recurrente(id: str) -> str:
    """Pausa un pago recurrente sin borrarlo (deja de generar recordatorios).

    Args:
      id: Identificador del pago recurrente a pausar.

    Returns:
      Texto confirmando la pausa, o un aviso si no se encontro el pago.
    """
    filas, aviso = await _leer_filas_pagos_recurrentes()
    if aviso:
        return aviso

    fila_num = None
    for idx, fila in enumerate(filas):
        if _celda(fila, 0).strip().lower() == id.strip().lower():
            fila_num = idx + 2
            break
    if fila_num is None:
        return f"No encontre un pago recurrente con el id '{id}'."

    _, _, sheets = _client_apis()

    def _pausar():
        sheets.spreadsheets().values().update(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{PAGOS_RECURRENTES_TAB}!G{fila_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [["NO"]]},
        ).execute(num_retries=2)

    await asyncio.to_thread(_pausar)
    return f"Pago recurrente '{id}' pausado. No generara mas recordatorios hasta que lo reactives en la hoja."


# --------------------------------------------------------------------------
# Medicamentos (recordatorios diarios de Leidy, embarazo, con confirmacion)
# --------------------------------------------------------------------------
# Columnas de la pestana "Medicamentos": ID, Nombre, Hora, Notas, Activo,
# UltimoAvisoEnviado, UltimaTomaConfirmada (A-G). Igual que pagos recurrentes,
# se parsea siempre por posicion de columna (nunca por encabezado). Este
# mismo estado (columna F) lo leen y escriben tanto el job de main.py como el
# fallback en Google Apps Script (ver apps_script/RecordatorioMedicamentos.gs)
# para no duplicar avisos entre los dos.

async def _leer_filas_medicamentos() -> tuple[list | None, str | None]:
    """Lee todas las filas (sin encabezado) de la pestana de medicamentos.

    Devuelve (filas, None), o (None, aviso) si la hoja/pestana no esta
    configurada, para que las tools devuelvan un mensaje amigable en vez de
    propagar la excepcion.
    """
    if not FINANCE_SHEET_ID:
        return None, "No hay una hoja de finanzas configurada todavia (falta FINANCE_SHEET_ID en .env)."

    _, _, sheets = _client_apis()

    def _leer():
        return sheets.spreadsheets().values().get(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{MEDICAMENTOS_TAB}!A:G",
        ).execute(num_retries=2)

    try:
        resultado = await asyncio.to_thread(_leer)
    except HttpError:
        logger.exception("No se pudo leer la pestana '%s'", MEDICAMENTOS_TAB)
        return None, (
            f"No encontre la pestana '{MEDICAMENTOS_TAB}' en la hoja de finanzas. "
            "Creala con encabezados (ID/Nombre/Hora/Notas/Activo/UltimoAvisoEnviado/"
            "UltimaTomaConfirmada) antes de usar los recordatorios de medicamentos."
        )
    return resultado.get("values", [])[1:], None


async def calcular_recordatorios_medicamentos(fecha_hora_referencia_iso: str = "") -> list[dict]:
    """Calcula que medicamentos activos ya deberian avisarse hoy y no se han avisado.

    Uso interno del job de polling de main.py (y equivalente al que corre el
    fallback de Apps Script) -- no se registra como tool del modelo
    conversacional porque no debe invocarse fuera de ese contexto (podria
    disparar avisos duplicados).
    """
    filas, aviso = await _leer_filas_medicamentos()
    if aviso or not filas:
        return []

    ahora = (
        datetime.fromisoformat(fecha_hora_referencia_iso)
        if fecha_hora_referencia_iso
        else datetime.now(ZONA_HORARIA)
    )
    hoy_iso = ahora.date().isoformat()

    pendientes = []
    for idx, fila in enumerate(filas):
        if not fila or not _celda(fila, 0):
            continue
        activo = _celda(fila, 4, "SI").strip().upper() != "NO"
        if not activo:
            continue

        hora_str = _celda(fila, 2)
        try:
            hora_programada = datetime.strptime(hora_str, "%H:%M").time()
        except ValueError:
            logger.warning("Fila de medicamento con Hora invalida (fila %d): %r", idx + 2, fila)
            continue

        ultimo_aviso = _celda_fecha_iso(fila, 5)
        if ultimo_aviso == hoy_iso:
            continue  # ya se aviso hoy (por el job local o por Apps Script)
        if ahora.time() < hora_programada:
            continue  # todavia no toca

        minutos_atraso = (
            (ahora.hour * 60 + ahora.minute) - (hora_programada.hour * 60 + hora_programada.minute)
        )
        pendientes.append({
            "id": _celda(fila, 0),
            "nombre": _celda(fila, 1) or _celda(fila, 0),
            "notas": _celda(fila, 3),
            "fila": idx + 2,  # +1 por indice 0-based, +1 por la fila de encabezado
            "atrasado": minutos_atraso >= MINUTOS_ATRASO_UMBRAL,
        })

    return pendientes


async def marcar_medicamento_enviado(fila: int, fecha_iso: str) -> None:
    """Marca (columna F) que ya se envio el aviso de un medicamento en esa fecha.

    Uso interno del job diario de main.py -- no se registra como tool del
    modelo conversacional.
    """
    _, _, sheets = _client_apis()

    def _actualizar():
        sheets.spreadsheets().values().update(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{MEDICAMENTOS_TAB}!F{fila}",
            valueInputOption="USER_ENTERED",
            body={"values": [[fecha_iso]]},
        ).execute(num_retries=2)

    await asyncio.to_thread(_actualizar)


async def confirmar_medicamento(id: str, confirmado_por: str = "") -> str:
    """Confirma que Leidy ya se tomo un medicamento hoy.

    Args:
      id: Identificador del medicamento, tal como aparece en listar_medicamentos.
      confirmado_por: Nombre de quien confirma (viene en el contexto del mensaje).

    Returns:
      Texto confirmando el registro, o un aviso si el medicamento no existe.
    """
    filas, aviso = await _leer_filas_medicamentos()
    if aviso:
        return aviso

    fila_encontrada = None
    fila_num = None
    for idx, fila in enumerate(filas):
        if _celda(fila, 0).strip().lower() == id.strip().lower():
            fila_encontrada = fila
            fila_num = idx + 2
            break
    if fila_encontrada is None:
        return f"No encontre un medicamento con el id '{id}'."

    hoy_iso = datetime.now(ZONA_HORARIA).date().isoformat()
    nombre = _celda(fila_encontrada, 1) or id

    _, _, sheets = _client_apis()

    def _marcar_tomado():
        sheets.spreadsheets().values().update(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{MEDICAMENTOS_TAB}!G{fila_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [[hoy_iso]]},
        ).execute(num_retries=2)

    await asyncio.to_thread(_marcar_tomado)
    logger.info("Medicamento confirmado: %s (%s) por %s", id, hoy_iso, confirmado_por)
    return f"Registrado: {nombre} tomado hoy."


async def _desactivar_en_pestana(tab: str, filas: list, id: str, etiqueta: str) -> str:
    """Pone Activo=NO (columna E) en cada fila cuyo ID **o Nombre** coincida.

    Compartido por medicamentos y habitos: las dos pestanas tienen el mismo
    layout A-G con Activo en la columna E.

    Busca tambien por nombre a proposito: un tratamiento suele ocupar varias
    filas, una por toma del dia (nitrofurantoina_06/_12/_18/_00), y quien
    escribe pide "desactiva la nitrofurantoina", no el id de cada toma. Al
    coincidir por nombre se apagan todas las tomas de una vez.

    No borra nada: la fila queda con su historial de confirmaciones y se
    reactiva poniendo 'SI' en la columna Activo.
    """
    buscado = id.strip().lower()
    objetivo = []
    for idx, fila in enumerate(filas):
        if not fila or not _celda(fila, 0):
            continue
        if buscado not in (_celda(fila, 0).strip().lower(), _celda(fila, 1).strip().lower()):
            continue
        if _celda(fila, 4, "SI").strip().upper() == "NO":
            continue  # ya estaba desactivado
        objetivo.append((idx + 2, (_celda(fila, 1) or _celda(fila, 0)).strip(), _celda(fila, 2)))

    if not objetivo:
        return f"No encontre ningun {etiqueta} activo con el id o nombre '{id}'."

    _, _, sheets = _client_apis()

    def _desactivar():
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=FINANCE_SHEET_ID,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [{"range": f"{tab}!E{num}", "values": [["NO"]]} for num, _, _ in objetivo],
            },
        ).execute(num_retries=2)

    await asyncio.to_thread(_desactivar)
    detalle = ", ".join(f"{nombre} ({hora})" for _, nombre, hora in objetivo)
    logger.info("Desactivado(s) %d %s(s) por '%s': %s", len(objetivo), etiqueta, id, detalle)
    return (
        f"Listo: desactive {len(objetivo)} recordatorio(s) de {etiqueta}: {detalle}. "
        "Dejan de avisar desde ya. Para reactivarlos, pon 'SI' en la columna Activo de la hoja."
    )


async def desactivar_medicamento(id: str) -> str:
    """Desactiva un medicamento para que deje de mandar recordatorios.

    Para cuando se termina un tratamiento (ej. un antibiotico). No borra la
    fila ni el historial de tomas; solo deja de avisar.

    Args:
      id: Identificador o nombre del medicamento, tal como aparece en
        listar_medicamentos. Si el tratamiento tiene varias tomas al dia
        (varias filas con el mismo nombre), las desactiva todas.

    Returns:
      Texto con los recordatorios que se desactivaron, o un aviso si no se
      encontro ninguno activo con ese id o nombre.
    """
    filas, aviso = await _leer_filas_medicamentos()
    if aviso:
        return aviso
    return await _desactivar_en_pestana(MEDICAMENTOS_TAB, filas or [], id, "medicamento")


async def listar_medicamentos() -> str:
    """Lista los medicamentos activos configurados y si ya se confirmo la toma de hoy.

    Returns:
      Texto con los medicamentos activos, su hora y notas, o un aviso si no
      hay ninguno o la pestana no esta configurada.
    """
    filas, aviso = await _leer_filas_medicamentos()
    if aviso:
        return aviso

    hoy_iso = datetime.now(ZONA_HORARIA).date().isoformat()
    lineas = []
    for fila in filas:
        if not fila or not _celda(fila, 0):
            continue
        activo = _celda(fila, 4, "SI").strip().upper() != "NO"
        if not activo:
            continue
        id_, nombre, hora, notas = _celda(fila, 0), _celda(fila, 1), _celda(fila, 2), _celda(fila, 3)
        tomado_hoy = _celda_fecha_iso(fila, 6) == hoy_iso
        estado = "ya tomado hoy" if tomado_hoy else "pendiente hoy"
        notas_txt = f" ({notas})" if notas else ""
        lineas.append(f"- ({id_}) {nombre} a las {hora}{notas_txt}: {estado}")

    if not lineas:
        return "No hay medicamentos configurados."
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# Notas personales (memoria de largo plazo, mas alla del historial de chat)
# --------------------------------------------------------------------------

_AVISO_TAB_NOTAS_FALTANTE = (
    f"No encontre la pestana '{NOTAS_TAB}' en la hoja de finanzas. "
    "Creala con encabezados (Fecha/Categoria/Texto/GuardadoPor/VigenteHasta) antes de usar notas personales."
)


async def _leer_filas_notas() -> tuple[list | None, str | None]:
    """Lee todas las filas (sin encabezado) de la pestana de notas.

    Devuelve (filas, None), o (None, aviso) si la hoja/pestana no esta
    configurada, para que las tools devuelvan un mensaje amigable en vez de
    propagar la excepcion.
    """
    if not FINANCE_SHEET_ID:
        return None, "No hay una hoja configurada todavia (falta FINANCE_SHEET_ID en .env)."

    _, _, sheets = _client_apis()

    def _leer():
        return sheets.spreadsheets().values().get(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{NOTAS_TAB}!A:E",
        ).execute(num_retries=2)

    try:
        resultado = await asyncio.to_thread(_leer)
    except HttpError:
        logger.exception("No se pudo leer la pestana '%s'", NOTAS_TAB)
        return None, _AVISO_TAB_NOTAS_FALTANTE
    return resultado.get("values", [])[1:], None


async def guardar_nota_personal(
    texto: str, categoria: str = "", vigente_hasta_iso: str = "", guardado_por: str = ""
) -> str:
    """Guarda un hecho o preferencia personal para recordarlo a largo plazo,
    mas alla de lo que dura el historial de conversacion.

    Usala para hechos y preferencias que vale la pena recordar en el futuro
    (ej. "a mama le gustan las plantas", "Leidy es alergica al camaron"), no
    para tareas ni eventos puntuales -- esos van en crear_tarea/crear_evento.

    Args:
      texto: El hecho o preferencia a recordar, en una frase clara y
        autocontenida (sin depender del contexto de la conversacion en la que
        se dijo).
      categoria: Etiqueta libre para agrupar la nota, por ejemplo 'Familia',
        'Salud' o 'Preferencias'. Opcional.
      vigente_hasta_iso: Si el hecho tiene una fecha de vencimiento natural
        (ej. algo valido solo mientras Leidy esta en gestacion), fecha ISO
        8601 (solo fecha) despues de la cual ya no debes asumirlo como cierto.
        Dejar vacio si no vence.
      guardado_por: Nombre de quien lo menciono en el chat (viene en el
        contexto del mensaje).

    Returns:
      Texto confirmando que la nota quedo guardada, o un aviso si la hoja no
      esta configurada.
    """
    if not FINANCE_SHEET_ID:
        return "No hay una hoja configurada todavia (falta FINANCE_SHEET_ID en .env)."

    _, _, sheets = _client_apis()
    hoy_iso = datetime.now(ZONA_HORARIA).date().isoformat()
    fila = [[hoy_iso, categoria, texto, guardado_por, vigente_hasta_iso]]

    def _agregar():
        # Sin num_retries, mismo motivo que registrar_movimiento: un append
        # reintentado a ciegas podria duplicar la nota.
        return sheets.spreadsheets().values().append(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{NOTAS_TAB}!A:E",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": fila},
        ).execute()

    try:
        await asyncio.to_thread(_agregar)
    except HttpError:
        logger.exception("No se pudo escribir en la pestana '%s'", NOTAS_TAB)
        return _AVISO_TAB_NOTAS_FALTANTE
    logger.info("Nota personal guardada (%s): %s", categoria or "sin categoria", texto)
    return f"Guardado: {texto}"


async def buscar_notas_personales(query: str = "") -> str:
    """Busca en las notas/preferencias personales guardadas a largo plazo.

    Args:
      query: Texto a buscar (coincidencia parcial, sin importar mayusculas o
        minusculas) dentro del texto o la categoria de cada nota. Si se deja
        vacio, devuelve todas las notas vigentes.

    Returns:
      Texto con las notas encontradas (las que ya vencieron segun su
      vigente_hasta se ignoran), o un aviso si no hay ninguna o la hoja no
      esta configurada.
    """
    filas, aviso = await _leer_filas_notas()
    if aviso:
        return aviso

    hoy = datetime.now(ZONA_HORARIA).date()
    query_norm = query.strip().lower()
    lineas = []
    for fila in filas:
        if len(fila) < 3:
            continue
        fecha, categoria, texto = fila[0], fila[1], fila[2]
        guardado_por = fila[3] if len(fila) > 3 else ""
        vigente_hasta = fila[4] if len(fila) > 4 else ""

        if vigente_hasta:
            try:
                if date.fromisoformat(vigente_hasta) < hoy:
                    continue  # ya vencio, no la muestres como vigente
            except ValueError:
                pass

        if query_norm and query_norm not in texto.lower() and query_norm not in categoria.lower():
            continue

        etiqueta = f"[{categoria}] " if categoria else ""
        quien = f" (dijo {guardado_por})" if guardado_por else ""
        lineas.append(f"- {etiqueta}{texto}{quien}")

    if not lineas:
        return "No hay notas guardadas para ese filtro."
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# Iniciativas proactivas (acciones que se disparan solas en una fecha futura)
# --------------------------------------------------------------------------

_AVISO_TAB_INICIATIVAS_FALTANTE = (
    f"No encontre la pestana '{INICIATIVAS_TAB}' en la hoja de finanzas. "
    "Creala con encabezados (ID/ChatId/Descripcion/TipoAccion/FechaDisparo/Contexto/Estado) "
    "antes de programar iniciativas."
)


async def _leer_filas_iniciativas() -> tuple[list | None, str | None]:
    """Lee todas las filas (sin encabezado) de la pestana de iniciativas.

    Devuelve (filas, None), o (None, aviso) si la hoja/pestana no esta
    configurada, para que las tools devuelvan un mensaje amigable en vez de
    propagar la excepcion.
    """
    if not FINANCE_SHEET_ID:
        return None, "No hay una hoja configurada todavia (falta FINANCE_SHEET_ID en .env)."

    _, _, sheets = _client_apis()

    def _leer():
        return sheets.spreadsheets().values().get(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{INICIATIVAS_TAB}!A:G",
        ).execute(num_retries=2)

    try:
        resultado = await asyncio.to_thread(_leer)
    except HttpError:
        logger.exception("No se pudo leer la pestana '%s'", INICIATIVAS_TAB)
        return None, _AVISO_TAB_INICIATIVAS_FALTANTE
    return resultado.get("values", [])[1:], None


async def programar_iniciativa(
    descripcion: str, fecha_disparo_iso: str, tipo_accion: str, contexto: str = "", chat_id: int = 0
) -> str:
    """Programa una iniciativa proactiva para dispararse sola en una fecha
    futura, sin esperar a que alguien la pida de nuevo (ej. avisar con ideas
    de regalo unos dias antes de un cumpleanos mencionado en la conversacion).

    Args:
      descripcion: Que se detecto, en una frase corta (ej. "Cumpleanos de mama").
      fecha_disparo_iso: Fecha/hora ISO 8601 en la que debe dispararse.
        Calculala a partir de la fecha/hora del contexto actual y cuanto antes
        conviene avisar (ej. 3 a 5 dias antes del evento para dar tiempo a
        conseguir un regalo).
      tipo_accion: Una de 'ideas_regalo' (genera y manda ideas de regalo
        relacionadas con la descripcion/contexto) o 'recordatorio_generico'
        (manda la descripcion/contexto tal cual como recordatorio).
      contexto: Detalles libres que hagan falta para ejecutar la accion
        despues (ej. para quien es, presupuesto mencionado, gustos). Guarda
        aca todo lo relevante -- el historial de chat puede rotar antes de
        que llegue la fecha de disparo.
      chat_id: Se inyecta automaticamente, no lo pidas ni lo inventes.

    Returns:
      Texto confirmando que quedo programada -- dile esto al usuario en el
      momento, es una accion transparente, no silenciosa -- o un aviso de error.
    """
    if tipo_accion not in TIPOS_INICIATIVA:
        return f"tipo_accion debe ser uno de {TIPOS_INICIATIVA}."
    try:
        fecha_disparo = datetime.fromisoformat(fecha_disparo_iso)
    except ValueError:
        return "fecha_disparo_iso no es una fecha/hora ISO 8601 valida."
    if not FINANCE_SHEET_ID:
        return "No hay una hoja configurada todavia (falta FINANCE_SHEET_ID en .env)."

    _, _, sheets = _client_apis()
    id_nueva = "ini_" + datetime.now(ZONA_HORARIA).strftime("%Y%m%d%H%M%S")
    fila = [[id_nueva, str(chat_id), descripcion, tipo_accion, fecha_disparo.isoformat(), contexto, "pendiente"]]

    def _agregar():
        # Sin num_retries, mismo motivo que en registrar_movimiento: un append
        # reintentado a ciegas podria duplicar la iniciativa.
        return sheets.spreadsheets().values().append(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{INICIATIVAS_TAB}!A:G",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": fila},
        ).execute()

    try:
        await asyncio.to_thread(_agregar)
    except HttpError:
        logger.exception("No se pudo escribir en la pestana '%s'", INICIATIVAS_TAB)
        return _AVISO_TAB_INICIATIVAS_FALTANTE

    logger.info("Iniciativa programada (%s): %s -> %s", tipo_accion, descripcion, fecha_disparo.isoformat())
    return f"Listo, programado para el {fecha_disparo.date().isoformat()}: {descripcion}."


async def calcular_iniciativas_pendientes(fecha_referencia_iso: str = "") -> list[dict]:
    """Calcula que iniciativas ya deberian dispararse (FechaDisparo vencida y
    Estado='pendiente').

    Uso interno del job de polling de main.py -- no se registra como tool del
    modelo conversacional (podria disparar avisos duplicados si se invoca
    fuera de ese contexto).
    """
    filas, aviso = await _leer_filas_iniciativas()
    if aviso or not filas:
        return []

    ahora = (
        datetime.fromisoformat(fecha_referencia_iso) if fecha_referencia_iso else datetime.now(ZONA_HORARIA)
    )

    pendientes = []
    for idx, fila in enumerate(filas):
        if not fila or not _celda(fila, 0):
            continue
        if _celda(fila, 6, "pendiente") != "pendiente":
            continue

        try:
            fecha_disparo = datetime.fromisoformat(_celda(fila, 4))
        except ValueError:
            logger.warning("Iniciativa con FechaDisparo invalida (fila %d): %r", idx + 2, fila)
            continue
        if ahora < fecha_disparo:
            continue  # todavia no toca

        try:
            chat_id = int(_celda(fila, 1))
        except ValueError:
            logger.warning("Iniciativa con ChatId invalido (fila %d): %r", idx + 2, fila)
            continue

        pendientes.append({
            "fila": idx + 2,  # +1 por indice 0-based, +1 por la fila de encabezado
            "id": _celda(fila, 0),
            "chat_id": chat_id,
            "descripcion": _celda(fila, 2),
            "tipo_accion": _celda(fila, 3),
            "contexto": _celda(fila, 5),
        })
    return pendientes


async def marcar_iniciativa_estado(fila: int, estado: str) -> None:
    """Actualiza la columna Estado (G) de una iniciativa. Uso interno del job
    de main.py -- no se registra como tool del modelo conversacional.
    """
    _, _, sheets = _client_apis()

    def _actualizar():
        sheets.spreadsheets().values().update(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{INICIATIVAS_TAB}!G{fila}",
            valueInputOption="USER_ENTERED",
            body={"values": [[estado]]},
        ).execute(num_retries=2)

    await asyncio.to_thread(_actualizar)


async def limpiar_iniciativas_antiguas(dias_retencion: int = DIAS_RETENCION_INICIATIVAS) -> int:
    """Borra las filas de iniciativas ya 'enviado'/'cancelado' con mas de
    `dias_retencion` dias de antiguedad (desde su FechaDisparo), para que la
    pestana no crezca para siempre -- a diferencia de Pagos Recurrentes o
    Medicamentos, cada iniciativa es un evento unico que nunca se reutiliza.

    Uso interno del job mensual de main.py -- no se registra como tool del
    modelo conversacional.

    Returns:
      Cuantas filas se borraron.
    """
    filas, aviso = await _leer_filas_iniciativas()
    if aviso or not filas:
        return 0

    limite = datetime.now(ZONA_HORARIA) - timedelta(days=dias_retencion)
    filas_a_borrar = []
    for idx, fila in enumerate(filas):
        if not fila or not _celda(fila, 0):
            continue
        if _celda(fila, 6, "pendiente") not in ("enviado", "cancelado"):
            continue
        try:
            fecha_disparo = datetime.fromisoformat(_celda(fila, 4))
        except ValueError:
            continue
        if fecha_disparo < limite:
            filas_a_borrar.append(idx + 2)  # +1 por indice 0-based, +1 por encabezado

    if not filas_a_borrar:
        return 0

    _, _, sheets = _client_apis()

    def _borrar():
        meta = sheets.spreadsheets().get(
            spreadsheetId=FINANCE_SHEET_ID, fields="sheets.properties"
        ).execute(num_retries=2)
        sheet_id = next(
            (
                h["properties"]["sheetId"]
                for h in meta.get("sheets", [])
                if h.get("properties", {}).get("title") == INICIATIVAS_TAB
            ),
            None,
        )
        if sheet_id is None:
            logger.warning("No se encontro el sheetId de '%s', no se borra nada.", INICIATIVAS_TAB)
            return
        # De la fila mas alta a la mas baja para que borrar una no corra los
        # indices de las que todavia faltan por borrar.
        requests = [
            {
                "deleteDimension": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": fila - 1, "endIndex": fila}
                }
            }
            for fila in sorted(filas_a_borrar, reverse=True)
        ]
        sheets.spreadsheets().batchUpdate(spreadsheetId=FINANCE_SHEET_ID, body={"requests": requests}).execute()

    await asyncio.to_thread(_borrar)
    logger.info("Limpieza de iniciativas: %d fila(s) borrada(s) (mas de %d dias).", len(filas_a_borrar), dias_retencion)
    return len(filas_a_borrar)


# --------------------------------------------------------------------------
# Habitos de salud y bienestar (recordatorios configurables, mismo motor que
# Medicamentos pero sin limitarse a tomar pastillas -- ej. ejercicio, tomar
# agua, un chequeo periodico).
# --------------------------------------------------------------------------

_AVISO_TAB_HABITOS_FALTANTE = (
    f"No encontre la pestana '{HABITOS_TAB}' en la hoja de finanzas. "
    "Creala con encabezados (ID/Nombre/Hora/Notas/Activo/UltimoAvisoEnviado/UltimaConfirmacion) "
    "antes de usar habitos de salud y bienestar."
)


async def _leer_filas_habitos() -> tuple[list | None, str | None]:
    """Lee todas las filas (sin encabezado) de la pestana de habitos.

    Devuelve (filas, None), o (None, aviso) si la hoja/pestana no esta
    configurada, para que las tools devuelvan un mensaje amigable en vez de
    propagar la excepcion.
    """
    if not FINANCE_SHEET_ID:
        return None, "No hay una hoja configurada todavia (falta FINANCE_SHEET_ID en .env)."

    _, _, sheets = _client_apis()

    def _leer():
        return sheets.spreadsheets().values().get(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{HABITOS_TAB}!A:G",
        ).execute(num_retries=2)

    try:
        resultado = await asyncio.to_thread(_leer)
    except HttpError:
        logger.exception("No se pudo leer la pestana '%s'", HABITOS_TAB)
        return None, _AVISO_TAB_HABITOS_FALTANTE
    return resultado.get("values", [])[1:], None


async def calcular_recordatorios_habitos(fecha_hora_referencia_iso: str = "") -> list[dict]:
    """Calcula que habitos activos ya deberian avisarse hoy y no se han avisado.

    Uso interno del job de polling de main.py -- no se registra como tool del
    modelo conversacional (podria disparar avisos duplicados).
    """
    filas, aviso = await _leer_filas_habitos()
    if aviso or not filas:
        return []

    ahora = (
        datetime.fromisoformat(fecha_hora_referencia_iso)
        if fecha_hora_referencia_iso
        else datetime.now(ZONA_HORARIA)
    )
    hoy_iso = ahora.date().isoformat()

    pendientes = []
    for idx, fila in enumerate(filas):
        if not fila or not _celda(fila, 0):
            continue
        activo = _celda(fila, 4, "SI").strip().upper() != "NO"
        if not activo:
            continue

        hora_str = _celda(fila, 2)
        try:
            hora_programada = datetime.strptime(hora_str, "%H:%M").time()
        except ValueError:
            logger.warning("Fila de habito con Hora invalida (fila %d): %r", idx + 2, fila)
            continue

        ultimo_aviso = _celda_fecha_iso(fila, 5)
        if ultimo_aviso == hoy_iso:
            continue  # ya se aviso hoy
        if ahora.time() < hora_programada:
            continue  # todavia no toca

        minutos_atraso = (
            (ahora.hour * 60 + ahora.minute) - (hora_programada.hour * 60 + hora_programada.minute)
        )
        pendientes.append({
            "id": _celda(fila, 0),
            "nombre": _celda(fila, 1) or _celda(fila, 0),
            "notas": _celda(fila, 3),
            "fila": idx + 2,  # +1 por indice 0-based, +1 por la fila de encabezado
            "atrasado": minutos_atraso >= MINUTOS_ATRASO_UMBRAL,
        })

    return pendientes


async def marcar_habito_enviado(fila: int, fecha_iso: str) -> None:
    """Marca (columna F) que ya se envio el aviso de un habito en esa fecha.

    Uso interno del job diario de main.py -- no se registra como tool del
    modelo conversacional.
    """
    _, _, sheets = _client_apis()

    def _actualizar():
        sheets.spreadsheets().values().update(
            spreadsheetId=FINANCE_SHEET_ID,
            range=f"{HABITOS_TAB}!F{fila}",
            valueInputOption="USER_ENTERED",
            body={"values": [[fecha_iso]]},
        ).execute(num_retries=2)

    await asyncio.to_thread(_actualizar)


async def confirmar_habito(id: str, confirmado_por: str = "") -> str:
    """Confirma que ya se cumplio hoy un habito de salud/bienestar.

    Args:
      id: Identificador del habito, tal como aparece en listar_habitos.
      confirmado_por: Nombre de quien confirma (viene en el contexto del mensaje).

    Returns:
      Texto confirmando el registro, o un aviso si no se encuentra el habito.
    """
    filas, aviso = await _leer_filas_habitos()
    if aviso:
        return aviso

    hoy_iso = datetime.now(ZONA_HORARIA).date().isoformat()
    for idx, fila in enumerate(filas):
        if _celda(fila, 0).strip().lower() != id.strip().lower():
            continue
        nombre = _celda(fila, 1) or id
        _, _, sheets = _client_apis()

        def _marcar(fila_num=idx + 2):
            sheets.spreadsheets().values().update(
                spreadsheetId=FINANCE_SHEET_ID,
                range=f"{HABITOS_TAB}!G{fila_num}",
                valueInputOption="USER_ENTERED",
                body={"values": [[hoy_iso]]},
            ).execute(num_retries=2)

        await asyncio.to_thread(_marcar)
        logger.info("Habito confirmado: %s (%s) por %s", id, hoy_iso, confirmado_por)
        return f"Registrado: {nombre} cumplido hoy."

    return f"No encontre un habito con id '{id}'."


async def desactivar_habito(id: str) -> str:
    """Desactiva un habito de salud/bienestar para que deje de mandar recordatorios.

    No borra la fila ni el historial de confirmaciones; solo deja de avisar.

    Args:
      id: Identificador o nombre del habito, tal como aparece en
        listar_habitos. Si hay varias filas con el mismo nombre (una por cada
        hora del dia), las desactiva todas.

    Returns:
      Texto con los recordatorios que se desactivaron, o un aviso si no se
      encontro ninguno activo con ese id o nombre.
    """
    filas, aviso = await _leer_filas_habitos()
    if aviso:
        return aviso
    return await _desactivar_en_pestana(HABITOS_TAB, filas or [], id, "habito")


async def listar_habitos() -> str:
    """Lista los habitos de salud/bienestar activos configurados y si ya se
    cumplieron hoy.

    Returns:
      Texto con los habitos activos, su hora y notas, o un aviso si no hay
      ninguno o la pestana no esta configurada.
    """
    filas, aviso = await _leer_filas_habitos()
    if aviso:
        return aviso

    hoy_iso = datetime.now(ZONA_HORARIA).date().isoformat()
    lineas = []
    for fila in filas:
        if not fila or not _celda(fila, 0):
            continue
        activo = _celda(fila, 4, "SI").strip().upper() != "NO"
        if not activo:
            continue
        id_, nombre, hora, notas = _celda(fila, 0), _celda(fila, 1), _celda(fila, 2), _celda(fila, 3)
        hecho_hoy = _celda_fecha_iso(fila, 6) == hoy_iso
        estado = "ya cumplido hoy" if hecho_hoy else "pendiente hoy"
        notas_txt = f" ({notas})" if notas else ""
        lineas.append(f"- ({id_}) {nombre} a las {hora}{notas_txt}: {estado}")

    if not lineas:
        return "No hay habitos de salud/bienestar configurados."
    return "\n".join(lineas)
