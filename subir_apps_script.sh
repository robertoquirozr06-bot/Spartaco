#!/usr/bin/env bash
# Sube apps_script/*.gs al proyecto de Apps Script de Spartacus, sin pisar a
# ciegas lo que haya en Google: primero baja lo remoto y muestra el diff.
#
# Requiere (una sola vez):
#   clasp login --user spartacus     <- cuenta DUENA de la hoja CONTROL DE SPARTACUS
#                                       (--user evita borrar la sesion de latamdigitalmarketing)
#   ./subir_apps_script.sh
#
# OJO con los nombres: en Google el archivo se llama Code.gs, no
# RecordatorioMedicamentos.gs. Subirlo con el nombre local crearia un segundo
# archivo con las mismas funciones (dos revisarMedicamentos en el proyecto),
# asi que el mapeo local->remoto de MAPEO es obligatorio, no cosmetico.
set -euo pipefail
cd "$(dirname "$0")"

USER_CLASP="${USER_CLASP:-spartacus}"
# Proyecto Apps Script vinculado a la hoja CONTROL DE SPARTACUS
# (cuenta robertoquirozr06@gmail.com). Es container-bound, por eso NO aparece
# en `clasp list-scripts` y hay que traer el id a mano.
SCRIPT_ID="${SCRIPT_ID:-1GeCqOnNg8tfPDiEcth-CCaVjuccaijpcFLmGYNQOPZYQc2a8OJsRVxD9}"

# "archivo local:archivo remoto"
MAPEO="apps_script/RecordatorioMedicamentos.gs:Code.js"

TRABAJO="$(mktemp -d)"; trap 'rm -rf "$TRABAJO"' EXIT
printf '{"scriptId":"%s","rootDir":"."}\n' "$SCRIPT_ID" > "$TRABAJO/.clasp.json"

echo "==> Bajando lo que hay HOY en Google..."
(cd "$TRABAJO" && clasp pull --user "$USER_CLASP" >/dev/null)

echo "==> Diferencias (izquierda = Google, derecha = local):"
hubo_diff=0
for par in $MAPEO; do
  local="${par%%:*}"; remoto="$TRABAJO/${par##*:}"
  if [ ! -f "$remoto" ]; then echo "   [NUEVO] ${par##*:}  (no existe en Google)"; hubo_diff=1; continue; fi
  # --strip-trailing-cr: clasp baja con CRLF y si no todo el archivo sale como cambiado
  if diff -q --strip-trailing-cr "$remoto" "$local" >/dev/null; then echo "   [IGUAL] ${par##*:}"
  else echo "   [CAMBIA] ${par##*:} <- $local"; diff -u --strip-trailing-cr "$remoto" "$local" | sed -n '3,40p' | sed 's/^/      /'; hubo_diff=1; fi
done

if [ "$hubo_diff" -eq 0 ]; then echo "==> Google ya tiene exactamente esto. No hay nada que subir."; exit 0; fi

read -r -p "==> Subir estos cambios a Google? [s/N] " ok
[ "$ok" = "s" ] || { echo "Cancelado, no se subio nada."; exit 1; }

for par in $MAPEO; do cp "${par%%:*}" "$TRABAJO/${par##*:}"; done
(cd "$TRABAJO" && clasp push --user "$USER_CLASP" --force)
echo "==> Listo. El trigger existente sigue igual; no hace falta reinstalarlo."
