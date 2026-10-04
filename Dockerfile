# Imagen de Espartaco para correr en la VM de Compute Engine (ver .env.example,
# seccion "Contenedor"). Misma version de Python que el venv local (3.14).
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Bogota

WORKDIR /app

# Dependencias primero: si solo cambia el codigo, Docker reusa esta capa.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Solo el codigo, nunca `COPY . .`: aunque falle el .dockerignore, ningun
# secreto (.env, token.json, credentials.json) puede colarse en la imagen.
COPY *.py ./

# Usuario sin privilegios. En el host, la carpeta montada en /data tiene que
# pertenecer a este UID (chown 10001) para que el bot pueda escribir ahi.
# El chmod deja el codigo de solo lectura: subido desde Windows llega como
# rw-rw-rw- y el bot podria reescribirse a si mismo.
RUN useradd --uid 10001 --no-create-home espartaco \
    && chmod 644 /app/*.py /app/requirements.txt \
    && mkdir -p /data \
    && chown espartaco /app /data
USER espartaco

CMD ["python", "main.py"]
