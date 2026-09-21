/**
 * Fallback en la nube de los recordatorios de medicamentos de Leidy (embarazo).
 *
 * Corre DENTRO de Google (vinculado a la hoja "CONTROL DE SPARTACUS", pestana
 * "Medicamentos"), asi que manda los avisos por Telegram aunque el PC de
 * Roberto (donde vive Espartaco / main.py) este apagado.
 *
 * Reimplementa la misma regla que google_services.calcular_recordatorios_medicamentos
 * en main.py: si se agrega/cambia un medicamento hay que actualizar los dos lados.
 *
 * Corre cada 15 min (ver instalarTrigger). El aviso "fino" lo da el job local de
 * main.py cada 25 min; esto es solo el respaldo para cuando el PC esta apagado.
 *
 * Solo manda el aviso (UrlFetchApp -> sendMessage). No escucha respuestas de
 * Telegram: si alguien toca el boton "Ya lo tome" de un aviso mandado desde
 * aca, ese toque queda encolado en Telegram y lo procesa el bot real
 * (confirmar_medicamento_callback en main.py) en cuanto el PC vuelva a
 * prender -- un solo lugar de verdad para las confirmaciones.
 *
 * Setup (una sola vez):
 *   1. Abrir la hoja "CONTROL DE SPARTACUS" -> Extensiones -> Apps Script.
 *   2. Pegar este archivo completo.
 *   3. Configuracion del proyecto -> Propiedades del script -> agregar
 *      TELEGRAM_TOKEN y ALLOWED_CHAT_ID (mismos valores que el .env de main.py).
 *   4. Ejecutar una vez, a mano, la funcion instalarTrigger (boton "Ejecutar"
 *      en el editor, con instalarTrigger seleccionado en el dropdown de
 *      funciones) y autorizar los permisos que pida Google.
 */

var TAB_NAME = 'Medicamentos';
var ZONA_HORARIA = 'America/Bogota';
// Debe ser MAYOR que el intervalo del trigger, si no los avisos normales salen
// marcados "(atrasado)" y la etiqueta deja de significar nada: con el trigger
// cada 15 min, un aviso sano ya llega hasta 15 min despues de la hora
// programada. Con 35 solo se marca cuando se perdieron dos ciclos o mas, que es
// cuando de verdad hubo un hueco (PC apagado + trigger espaciado por Google).
// Si se cambia MINUTOS_ENTRE_REVISIONES, revisar tambien este numero.
var MINUTOS_ATRASO_PARA_AVISAR = 35;

function revisarMedicamentos() {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('TELEGRAM_TOKEN');
  var chatId = props.getProperty('ALLOWED_CHAT_ID');
  if (!token || !chatId) {
    console.error('Faltan TELEGRAM_TOKEN/ALLOWED_CHAT_ID en Propiedades del script.');
    return;
  }

  var hoja = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(TAB_NAME);
  if (!hoja) {
    console.error("No existe la pestana '" + TAB_NAME + "'.");
    return;
  }

  var datos = hoja.getDataRange().getValues(); // fila 0 = encabezado
  var ahora = new Date();
  var hoyStr = Utilities.formatDate(ahora, ZONA_HORARIA, 'yyyy-MM-dd');
  var minutosActuales = parseInt(Utilities.formatDate(ahora, ZONA_HORARIA, 'HH'), 10) * 60
      + parseInt(Utilities.formatDate(ahora, ZONA_HORARIA, 'mm'), 10);

  for (var fila = 1; fila < datos.length; fila++) {
    var id = datos[fila][0];
    var nombre = datos[fila][1];
    var horaStr = datos[fila][2];
    var notas = datos[fila][3];
    var activo = (datos[fila][4] || 'SI').toString().toUpperCase();
    // getValues() devuelve un objeto Date (no un string) para toda celda con
    // formato de fecha, y la columna F lo tiene. Comparar ese Date contra el
    // string hoyStr daba SIEMPRE false, asi que el "ya se aviso hoy" nunca
    // frenaba nada y el aviso se reenviaba cada 5 minutos durante todo el dia.
    // Normalizar antes de comparar es lo unico que vuelve fiable ese corte.
    var ultimoAviso = normalizarFechaISO(datos[fila][5]);

    if (!id || activo === 'NO') continue;
    if (ultimoAviso === hoyStr) continue; // ya se aviso hoy (por Apps Script o por main.py)

    var partesHora = normalizarHora(horaStr).split(':');
    if (partesHora.length !== 2) continue;
    var minutosProgramados = parseInt(partesHora[0], 10) * 60 + parseInt(partesHora[1], 10);
    if (isNaN(minutosProgramados) || minutosActuales < minutosProgramados) continue; // todavia no toca

    var atrasado = (minutosActuales - minutosProgramados) >= MINUTOS_ATRASO_PARA_AVISAR;
    enviarAvisoTelegram(token, chatId, id, nombre, notas, atrasado, hoyStr);
    hoja.getRange(fila + 1, 6).setValue(hoyStr); // columna F = UltimoAvisoEnviado (1-based)
  }
}

/**
 * Devuelve 'yyyy-MM-dd' venga la celda como Date (celda con formato de fecha)
 * o como texto ya escrito en ese formato. Sin esto, el anti-duplicado de
 * revisarMedicamentos compara Date contra string y nunca corta.
 */
function normalizarFechaISO(valor) {
  if (valor === '' || valor === null || valor === undefined) return '';
  if (valor instanceof Date) return Utilities.formatDate(valor, ZONA_HORARIA, 'yyyy-MM-dd');
  return valor.toString().trim();
}

/**
 * Devuelve 'HH:mm'. La columna Hora hoy es texto, pero si alguien le aplica
 * formato de hora en la hoja pasaria a llegar como Date y el split(':') daria
 * 3 partes -> la fila se saltaria en silencio y el medicamento dejaria de
 * avisarse, que en un tratamiento de embarazo es peor que un aviso de mas.
 */
function normalizarHora(valor) {
  if (valor === '' || valor === null || valor === undefined) return '';
  if (valor instanceof Date) return Utilities.formatDate(valor, ZONA_HORARIA, 'HH:mm');
  return valor.toString().trim();
}


function enviarAvisoTelegram(token, chatId, id, nombre, notas, atrasado, hoyStr) {
  var texto = '💊 Hora de: ' + nombre;
  if (notas) texto += ' (' + notas + ')';
  if (atrasado) texto += ' (atrasado)';

  var payload = {
    chat_id: chatId,
    text: texto,
    reply_markup: {
      inline_keyboard: [[{ text: '✅ Ya lo tome', callback_data: 'medicamento:' + id + ':' + hoyStr }]],
    },
  };

  UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
}

/**
 * Ejecutar a mano: instala el trigger (reemplaza uno previo si existia).
 *
 * OJO: esta funcion BORRA el trigger que exista de revisarMedicamentos, asi que
 * correrla pisa cualquier ajuste hecho a mano desde la UI de activadores. Si se
 * cambia el intervalo por la UI, cambiarlo tambien aca o la proxima corrida lo
 * revierte en silencio. El intervalo vive en MINUTOS_ENTRE_REVISIONES.
 *
 * everyMinutes() solo acepta 1, 5, 10, 15 o 30. Ojo con bajarlo a 1: son 1440
 * corridas/dia contra un tope de 90 min/dia de ejecucion de triggers (cuenta
 * gmail.com), o sea ~3.8 s por corrida cuando una real toma 1-3 s. Si la cuota
 * se agota, Google deja de correr el trigger el resto del dia EN SILENCIO, que
 * para un recordatorio de medicamentos es el peor fallo posible.
 */
var MINUTOS_ENTRE_REVISIONES = 15;

function instalarTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'revisarMedicamentos') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  ScriptApp.newTrigger('revisarMedicamentos').timeBased().everyMinutes(MINUTOS_ENTRE_REVISIONES).create();
  console.log('Trigger instalado: revisarMedicamentos cada ' + MINUTOS_ENTRE_REVISIONES + ' minutos.');
}
