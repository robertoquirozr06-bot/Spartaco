"""Genera el config.env de la VM a partir del .env local, SIN secretos.

Uso (desde asistente_ia/):
    venv\\Scripts\\python.exe deploy\\generar_config_env.py [salida]

Copia solo las variables de esta lista (allowlist): si el .env tiene claves
viejas o secretos que no estan aca, no se cuelan. Los secretos los pone
arrancar.sh desde Secret Manager, y DATA_DIR/GOOGLE_TOKEN_PATH/... los fija
arrancar.sh para el contenedor.

El formato es el de `docker run --env-file`: KEY=valor, sin comillas (Docker
no las quita, quedarian como parte del valor).
"""
import sys
from pathlib import Path

from dotenv import dotenv_values

NO_SECRETAS = [
    "ALLOWED_CHAT_ID",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_FALLBACK_MODEL",
    "CALENDAR_ID",
    "TASKLIST_ID",
    "FINANCE_SHEET_ID",
    "FINANCE_SHEET_TAB",
    "PAGOS_RECURRENTES_TAB",
    "MEDICAMENTOS_TAB",
    "HABITOS_TAB",
    "NOTAS_TAB",
    "INICIATIVAS_TAB",
    "SERPER_UBICACION_DEFAULT",
]

RAIZ = Path(__file__).resolve().parent.parent


def main() -> None:
    salida = Path(sys.argv[1]) if len(sys.argv) > 1 else RAIZ / "config.env"
    valores = dotenv_values(RAIZ / ".env", encoding="utf-8")

    lineas = []
    for clave in NO_SECRETAS:
        valor = (valores.get(clave) or "").strip()
        if not valor:
            continue
        if "\n" in valor:
            sys.exit(f"{clave} tiene un salto de linea: --env-file no lo soporta.")
        lineas.append(f"{clave}={valor}")

    # newline="\n": el archivo va a Linux.
    salida.write_text("\n".join(lineas) + "\n", encoding="utf-8", newline="\n")

    omitidas = sorted(k for k in valores if k not in NO_SECRETAS)
    print(f"Escrito {salida} con: {', '.join(l.split('=', 1)[0] for l in lineas)}")
    print(f"Omitidas (secretos o no aplican): {', '.join(omitidas) or 'ninguna'}")
    if "ALLOWED_CHAT_ID" not in {l.split("=", 1)[0] for l in lineas}:
        print("OJO: sin ALLOWED_CHAT_ID el bot no responde en ningun chat.")


if __name__ == "__main__":
    main()
