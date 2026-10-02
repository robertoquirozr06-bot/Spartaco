"""Puente entre HEBE Agents y Espartaco (adaptador `webhook` de HEBE).

HEBE hace POST /hebe/espartaco con {runId, input: {mensaje}, callbackUrl, ...}.
El puente responde 202 al instante, Espartaco contesta en segundo plano y el
resultado se entrega en `callbackUrl` firmado con HMAC-SHA256 en la cabecera
`x-hebe-signature: sha256=<hex>`.

No reemplaza al bot de Telegram: pueden correr a la vez. Usa el .env y el
token.json de SPARTACO_DIR, pero un historial propio (historial_hebe.json) para
no pisar ni leer las conversaciones del grupo.

Arranque: copia .env.hebe.example a .env.hebe, completa los valores y corre
    <SPARTACO_DIR>\\venv\\Scripts\\python.exe hebe_webhook.py
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

AQUI = Path(__file__).resolve().parent
load_dotenv(AQUI / ".env.hebe", encoding="utf-8")

SPARTACO_DIR = Path(os.getenv("SPARTACO_DIR") or AQUI).resolve()
TOKEN = os.getenv("HEBE_AGENT_TOKEN", "").strip()
SECRETO = os.getenv("HEBE_WEBHOOK_SECRET", "").strip()
PUERTO = int(os.getenv("HEBE_PORT", "8787"))
ORIGENES_CALLBACK = {
    o.strip().rstrip("/") for o in os.getenv("HEBE_CALLBACK_ORIGINS", "http://localhost:3000").split(",") if o.strip()
}
RUTA = "/hebe/espartaco"
MAX_CUERPO = 100_000
MAX_MENSAJE = 8_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("hebe_webhook")

if not TOKEN or not SECRETO:
    sys.exit("Faltan HEBE_AGENT_TOKEN o HEBE_WEBHOOK_SECRET en .env.hebe.")

# google_auth abre token.json con ruta relativa y brain/google_services leen el
# entorno al importarse: hay que entrar a SPARTACO_DIR y cargar su .env antes.
os.chdir(SPARTACO_DIR)
sys.path.insert(0, str(SPARTACO_DIR))
load_dotenv(SPARTACO_DIR / ".env", encoding="utf-8")

import brain  # noqa: E402
import httpx  # noqa: E402

# brain carga al importar el historial del grupo y lo reescribe entero en cada
# respuesta. Se suelta de memoria y se apunta a un archivo propio.
brain._historiales.clear()
brain.HISTORIAL_PATH = AQUI / "historial_hebe.json"

brain.configure(
    deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
    deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip(),
)

LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, name="espartaco-loop", daemon=True).start()


def _firma(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()


def _origen(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


async def _entregar(callback_url: str, resultado: dict) -> None:
    cuerpo = json.dumps(resultado, ensure_ascii=False).encode("utf-8")
    cabeceras = {"content-type": "application/json", "x-hebe-signature": _firma(cuerpo)}
    async with httpx.AsyncClient(timeout=15) as cliente:
        for intento in range(1, 4):
            try:
                r = await cliente.post(callback_url, content=cuerpo, headers=cabeceras)
                if r.status_code < 500:
                    # 404 = HEBE ya cerró esa ejecución (p. ej. por tiempo límite): no se reintenta.
                    logger.info("Callback entregado (%s) %s", r.status_code, callback_url)
                    return
                logger.warning("Callback respondió %s (intento %s)", r.status_code, intento)
            except httpx.HTTPError as exc:
                logger.warning("Callback falló (intento %s): %s", intento, exc)
            await asyncio.sleep(2 * intento)
    logger.error("No se pudo entregar el resultado a %s", callback_url)


async def _atender(run_id: str, mensaje: str, callback_url: str) -> None:
    # Un chat por ejecución: cada tarea de HEBE arranca sin memoria de otras.
    chat_id = -int(hashlib.sha256(run_id.encode()).hexdigest()[:12], 16)
    try:
        respuesta = await brain.ask(chat_id, mensaje, remitente="HEBE")
        resultado = {"status": "succeeded", "output": respuesta}
    except Exception:
        logger.exception("Espartaco falló en la ejecución %s", run_id)
        resultado = {"status": "failed", "error": "Espartaco no pudo terminar la tarea. Intenta de nuevo."}
    finally:
        brain._historiales.pop(chat_id, None)
        brain._guardar_historiales_disco()
    await _entregar(callback_url, resultado)


class Puente(BaseHTTPRequestHandler):
    server_version = "EspartacoHEBE/1.0"

    def _json(self, estado: int, datos: dict) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(estado)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/salud":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "No existe."})

    def do_POST(self) -> None:
        if urlparse(self.path).path != RUTA:
            return self._json(404, {"error": "No existe."})
        if not hmac.compare_digest(self.headers.get("authorization", ""), f"Bearer {TOKEN}"):
            return self._json(401, {"error": "No autorizado."})

        largo = int(self.headers.get("content-length") or 0)
        if largo <= 0 or largo > MAX_CUERPO:
            return self._json(413, {"error": "Cuerpo vacío o demasiado grande."})
        try:
            datos = json.loads(self.rfile.read(largo))
            run_id = str(datos["runId"])
            mensaje = str(datos["input"]["mensaje"]).strip()
            callback_url = str(datos["callbackUrl"])
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "Faltan runId, input.mensaje o callbackUrl."})
        if not mensaje or len(mensaje) > MAX_MENSAJE:
            return self._json(400, {"error": "El mensaje está vacío o es demasiado largo."})
        # El resultado solo viaja a HEBE: nunca a una URL que traiga la petición.
        if _origen(callback_url) not in ORIGENES_CALLBACK:
            return self._json(400, {"error": "callbackUrl no permitido."})

        asyncio.run_coroutine_threadsafe(_atender(run_id, mensaje, callback_url), LOOP)
        logger.info("Tarea aceptada %s", run_id)
        self._json(202, {"accepted": True})

    def log_message(self, formato: str, *args) -> None:
        logger.info("%s %s", self.address_string(), formato % args)


if __name__ == "__main__":
    servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), Puente)
    logger.info("Puente HEBE ↔ Espartaco en http://127.0.0.1:%s%s (datos de %s)", PUERTO, RUTA, SPARTACO_DIR)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
