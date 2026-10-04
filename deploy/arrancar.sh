#!/usr/bin/env bash
# Levanta (o vuelve a levantar) el contenedor de Espartaco en la VM.
#
# Lo corre espartaco.service en cada arranque de la VM. Tambien se usa a mano
# para desplegar una imagen nueva (cambiar /opt/espartaco/imagen y correr
# `sudo systemctl restart espartaco`).
#
# Los secretos se leen de Secret Manager en cada arranque, asi que una version
# nueva de un secreto entra con un simple restart.
set -euo pipefail

BASE=/opt/espartaco
DATA="$BASE/data"               # disco persistente: token.json, historial, estado
CONFIG="$BASE/config.env"       # configuracion NO secreta (ALLOWED_CHAT_ID, hoja, pestanas...)
IMAGEN="$(cat "$BASE/imagen")"  # ej. us-east1-docker.pkg.dev/<proyecto>/espartaco/bot:v2
NOMBRE=espartaco
UID_BOT=10001                   # el usuario del Dockerfile

PROYECTO="$(curl -sf -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/project/project-id)"

secreto() {
    gcloud secrets versions access latest --secret="$1" --project="$PROYECTO" --quiet
}

[[ -f "$DATA/token.json" ]] || { echo "Falta $DATA/token.json (copialo desde el PC)." >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "Falta $CONFIG (generalo con deploy/generar_config_env.py)." >&2; exit 1; }
chown -R "$UID_BOT" "$DATA"

# Asignacion aparte y no dentro del echo: asi, si Secret Manager falla, set -e
# corta aca en vez de crear el contenedor con la variable vacia.
telegram_token="$(secreto telegram-token)"
deepseek_api_key="$(secreto deepseek-api-key)"
# Opcional: sin este secreto, las busquedas en internet avisan que falta configurar.
serper_api_key="$(secreto serper-api-key 2>/dev/null || true)"

# Los secretos pasan por un archivo en /run (RAM, nunca toca el disco) solo
# mientras se crea el contenedor.
install -d -m 700 /run/espartaco
SECRETOS=/run/espartaco/secretos.env
trap 'rm -f "$SECRETOS"' EXIT
(
    umask 077
    {
        echo "TELEGRAM_TOKEN=$telegram_token"
        echo "DEEPSEEK_API_KEY=$deepseek_api_key"
        [[ -n "$serper_api_key" ]] && echo "SERPER_API_KEY=$serper_api_key"
        true
    } > "$SECRETOS"
)

gcloud auth configure-docker "${IMAGEN%%/*}" --quiet >/dev/null 2>&1
# Pull antes de borrar el contenedor viejo: si la descarga falla, el bot que
# ya estaba corriendo sigue atendiendo.
docker pull "$IMAGEN"
docker rm -f "$NOMBRE" >/dev/null 2>&1 || true

# --restart=always hace de watchdog: main.py termina el proceso a proposito
# (os._exit) cuando deja de escuchar a Telegram, y Docker lo vuelve a levantar.
docker run -d --name "$NOMBRE" --restart=always \
    --env-file "$CONFIG" --env-file "$SECRETOS" \
    -e DATA_DIR=/data \
    -e GOOGLE_TOKEN_PATH=/data/token.json \
    -e CONTEXTO_FAMILIA_PATH=/data/contexto_familia.md \
    -e LOG_TO_FILE=0 \
    -v "$DATA":/data \
    --log-driver json-file --log-opt max-size=10m --log-opt max-file=5 \
    "$IMAGEN"

echo "Espartaco arriba con $IMAGEN"
