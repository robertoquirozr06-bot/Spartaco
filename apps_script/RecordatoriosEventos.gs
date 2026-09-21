/**
 * Fallback en la nube de los recordatorios de eventos de Calendar: aviso
 * agrupado de "esto tienes manana" (8pm) y aviso puntual "en 2 horas" por
 * evento con hora.
 *
 * Corre DENTRO de Google (mismo proyecto de Apps Script que
 * RecordatorioMedicamentos.gs, vinculado a la hoja "CONTROL DE SPARTACUS"),
 * asi que manda los avisos por Telegram aunque el PC de Roberto (donde vive
 * Espartaco / main.py) este apagado.
 *
 * Reimplementa la misma regla que recordatorio_eventos_manana/
 * recordatorio_eventos_2h en main.py: si cambia el texto o la logica hay que
 * actualizar los dos lados.
 *
 * El anti-duplicado NO vive en esta hoja: vive en extendedProperties.private
 * del propio evento de Calendar (avisoManana / aviso2h), leido y escrito
 * tanto por este script como por google_services.py. Asi, sin importar cual
 * de los dos lados corre primero, el otro ve el flag ya puesto y no reenvia.
 *
 * Setup (una sola vez):
 *   1. Abrir la hoja "CONTROL DE SPARTACUS" -> Extensiones -> Apps Script
 *      (mismo proyecto donde ya esta RecordatorioMedicamentos.gs).
 *   2. Pegar este archivo completo como uno nuevo (+ -> Script).
 *   3. Servicios (icono +) -> agregar el servicio avanzado "Calendar API".
 *   4. Configuracion del proyecto -> Propiedades del script: ya deberian
 *      estar TELEGRAM_TOKEN y ALLOWED_CHAT_ID (compartidas con el fallback
 *      de medicamentos). Si el calendario de la familia no es el "primary"
 *      de la cuenta que autoriza el script, agregar tambien CALENDAR_ID con
 *      el mismo valor que el .env de main.py.
 *   5. Ejecutar una vez, a mano, la funcion instalarTriggersEventos (boton
 *      "Ejecutar" en el editor, con instalarTriggersEventos seleccionada en
 *      el dropdown de funciones) y autorizar los permisos que pida Google.
 */

var VENTANA_HORAS_2H = 2;

function _calendarId_() {
  return PropertiesService.getScriptProperties().getProperty('CALENDAR_ID') || 'primary';
}

function _credencialesTelegram_() {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('TELEGRAM_TOKEN');
  var chatId = props.getProperty('ALLOWED_CHAT_ID');
  if (!token || !chatId) {
    console.error('Faltan TELEGRAM_TOKEN/ALLOWED_CHAT_ID en Propiedades del script.');
    return null;
  }
  return { token: token, chatId: chatId };
}

function _horaBogota_(fechaIso) {
  return Utilities.formatDate(new Date(fechaIso), 'America/Bogota', 'HH:mm');
}

/** Read-modify-write de extendedProperties.private, igual que
 * google_services.marcar_evento_recordatorio -- no pisa el otro flag. */
function _marcarRecordatorio_(calendarId, eventId, campo, valor) {
  var evento = Calendar.Events.get(calendarId, eventId);
  var privadas = (evento.extendedProperties && evento.extendedProperties.private) || {};
  privadas[campo] = valor;
  Calendar.Events.patch({ extendedProperties: { private: privadas } }, calendarId, eventId);
}

function _enviarMensajeTelegram_(token, chatId, texto) {
  UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({ chat_id: chatId, text: texto }),
    muteHttpExceptions: true,
  });
}

function revisarEventosManana() {
  var credenciales = _credencialesTelegram_();
  if (!credenciales) return;

  var calendarId = _calendarId_();
  var ahora = new Date();
  var mananaStr = Utilities.formatDate(
    new Date(ahora.getTime() + 24 * 60 * 60 * 1000), 'America/Bogota', 'yyyy-MM-dd'
  );
  var inicioVentana = new Date(mananaStr + 'T00:00:00-05:00');
  var finVentana = new Date(inicioVentana.getTime() + 24 * 60 * 60 * 1000);

  var resultado = Calendar.Events.list(calendarId, {
    timeMin: inicioVentana.toISOString(),
    timeMax: finVentana.toISOString(),
    singleEvents: true,
    orderBy: 'startTime',
  });
  var items = resultado.items || [];

  var pendientes = [];
  for (var i = 0; i < items.length; i++) {
    var privadas = (items[i].extendedProperties && items[i].extendedProperties.private) || {};
    if (privadas.avisoManana !== mananaStr) pendientes.push(items[i]);
  }
  if (pendientes.length === 0) return; // nada nuevo (vacio, o ya avisado por este script o por main.py)

  var lineas = [];
  for (var j = 0; j < pendientes.length; j++) {
    var ev = pendientes[j];
    var titulo = ev.summary || '(sin titulo)';
    if (ev.start.dateTime) {
      lineas.push('- ' + titulo + ' a las ' + _horaBogota_(ev.start.dateTime));
    } else {
      lineas.push('- ' + titulo + ' (todo el dia)');
    }
  }
  _enviarMensajeTelegram_(
    credenciales.token, credenciales.chatId, '📅 Recordatorio: esto tienes manana:\n' + lineas.join('\n')
  );

  for (var k = 0; k < pendientes.length; k++) {
    _marcarRecordatorio_(calendarId, pendientes[k].id, 'avisoManana', mananaStr);
  }
}

function revisarEventos2h() {
  var credenciales = _credencialesTelegram_();
  if (!credenciales) return;

  var calendarId = _calendarId_();
  var ahora = new Date();
  var finVentana = new Date(ahora.getTime() + VENTANA_HORAS_2H * 60 * 60 * 1000);

  var resultado = Calendar.Events.list(calendarId, {
    timeMin: ahora.toISOString(),
    timeMax: finVentana.toISOString(),
    singleEvents: true,
    orderBy: 'startTime',
  });
  var items = resultado.items || [];

  for (var i = 0; i < items.length; i++) {
    var ev = items[i];
    if (!ev.start.dateTime) continue; // evento de todo el dia, no aplica
    var privadas = (ev.extendedProperties && ev.extendedProperties.private) || {};
    if (privadas.aviso2h === '1') continue;

    var titulo = ev.summary || '(sin titulo)';
    _enviarMensajeTelegram_(
      credenciales.token, credenciales.chatId, '⏰ En 2 horas: ' + titulo + ' (' + _horaBogota_(ev.start.dateTime) + ')'
    );
    _marcarRecordatorio_(calendarId, ev.id, 'aviso2h', '1');
  }
}

/** Ejecutar una sola vez a mano: instala los triggers (reemplaza previos si existian). */
function instalarTriggersEventos() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    var fn = triggers[i].getHandlerFunction();
    if (fn === 'revisarEventosManana' || fn === 'revisarEventos2h') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  ScriptApp.newTrigger('revisarEventosManana').timeBased().everyDays(1).atHour(20).nearMinute(0).create();
  ScriptApp.newTrigger('revisarEventos2h').timeBased().everyMinutes(10).create();
  console.log('Triggers instalados: revisarEventosManana (~8pm diario) y revisarEventos2h (cada 10 min).');
}
