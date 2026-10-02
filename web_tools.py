"""
web_tools.py - Herramientas de Espartaco para salir a buscar informacion real
en internet (Fase 7): precios, negocios locales (ej. carpinteros) y el
contenido de una pagina especifica.

Mismo patron que google_services.py: cada funcion publica es una tool de
function calling (firma tipada + docstring), nunca lanza al modelo -- si algo
falla (falta la API key, error de red, timeout) devuelve un texto de aviso en
espanol en vez de una excepcion.

Usa Serper.dev (https://serper.dev) como motor de busqueda: envuelve
resultados de Google, incluye un endpoint /places con negocios locales
(telefono, direccion, sitio web) que resuelve directo el caso de "buscar
carpinteros con contacto".
"""
import asyncio
import ipaddress
import logging
import os
import re
import socket

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("espartaco")

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
SERPER_UBICACION_DEFAULT = os.getenv("SERPER_UBICACION_DEFAULT", "Bogotá, Colombia").strip() or "Bogotá, Colombia"

_BASE_URL = "https://google.serper.dev"
_TIMEOUT_BUSQUEDA = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
_TIMEOUT_PAGINA = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_MAX_CHARS_PAGINA = 3000
_MAX_REDIRECCIONES = 5

# Heuristica, no exhaustiva: solo para destacar un posible contacto dentro del
# texto de una pagina. El contacto confiable de un negocio viene de
# buscar_negocios_locales (campo estructurado de Serper), esto es un respaldo.
_PATRON_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PATRON_TELEFONO = re.compile(r"(?:\+57[\s.-]?)?(?:3\d{2}|\(?60\d\)?)[\s.-]?\d{3}[\s.-]?\d{4}")


async def _motivo_url_bloqueada(url: httpx.URL) -> str | None:
    """Devuelve por que no se debe abrir `url`, o None si apunta a internet publico.

    El texto de una pagina web le llega al modelo, y una pagina maliciosa
    puede pedirle que abra otra URL. Sin este filtro leer_pagina podria
    terminar consultando la red local (router, 127.0.0.1, otros equipos),
    asi que solo se permiten http/https hacia IPs publicas. No cubre DNS
    rebinding (el nombre se resuelve de nuevo al conectar), que para este
    uso es un riesgo aceptado.
    """
    if url.scheme not in ("http", "https"):
        return f"solo se abren links http/https (vino {url.scheme or 'sin esquema'})"
    if not url.host:
        return "el link no trae dominio"
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            url.host, url.port or (443 if url.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return f"no existe el dominio {url.host}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not ip.is_global:
            return f"{url.host} apunta a una direccion privada o local ({ip})"
    return None


def _falta_api_key() -> str | None:
    if not SERPER_API_KEY:
        return "No puedo buscar en internet todavia: falta configurar SERPER_API_KEY en .env."
    return None


async def buscar_en_internet(query: str, num_resultados: int = 5) -> str:
    """Busca en internet (via Google, Serper.dev) y devuelve los resultados.

    Args:
      query: Que buscar, en lenguaje natural, ej. 'precio acetaminofen 500 mg Colombia'.
      num_resultados: Cuantos resultados devolver (default 5, maximo util ~10).

    Returns:
      Texto con titulo, link y fragmento de cada resultado encontrado (el
      fragmento a veces ya trae un precio), o un aviso si no hay resultados.
    """
    aviso = _falta_api_key()
    if aviso:
        return aviso

    body = {"q": query, "num": max(1, min(num_resultados, 10)), "gl": "co", "hl": "es"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_BUSQUEDA) as client:
            resp = await client.post(
                f"{_BASE_URL}/search",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("buscar_en_internet: timeout buscando %r", query)
        return "La busqueda tardo demasiado y se cancelo, intenta de nuevo."
    except Exception:
        logger.exception("buscar_en_internet fallo buscando %r", query)
        return "No pude completar la busqueda en internet ahora mismo."

    organicos = data.get("organic", [])
    if not organicos:
        return f"No encontre resultados para '{query}'."

    lineas = [f"Resultados para '{query}':"]
    for r in organicos[:num_resultados]:
        titulo = r.get("title", "(sin titulo)")
        link = r.get("link", "")
        snippet = r.get("snippet", "")
        lineas.append(f"- {titulo}\n  {link}\n  {snippet}")
    return "\n".join(lineas)


async def buscar_negocios_locales(query: str, ubicacion: str = "") -> str:
    """Busca negocios/proveedores locales (ej. carpinteros, plomeros) con su
    contacto, via Google Places (Serper.dev).

    Args:
      query: Que tipo de negocio buscar, ej. 'carpintero muebles a medida'.
      ubicacion: Ciudad/zona donde buscar (opcional, default Bogota, Colombia).

    Returns:
      Texto con nombre, direccion, telefono y sitio web de cada negocio
      encontrado, o un aviso si no hay resultados.
    """
    aviso = _falta_api_key()
    if aviso:
        return aviso

    body = {"q": query, "location": ubicacion.strip() or SERPER_UBICACION_DEFAULT, "gl": "co", "hl": "es"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_BUSQUEDA) as client:
            resp = await client.post(
                f"{_BASE_URL}/places",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("buscar_negocios_locales: timeout buscando %r", query)
        return "La busqueda tardo demasiado y se cancelo, intenta de nuevo."
    except Exception:
        logger.exception("buscar_negocios_locales fallo buscando %r", query)
        return "No pude completar la busqueda de negocios locales ahora mismo."

    lugares = data.get("places", [])
    if not lugares:
        return f"No encontre negocios locales para '{query}'."

    lineas = [f"Negocios encontrados para '{query}':"]
    for p in lugares:
        nombre = p.get("title", "(sin nombre)")
        direccion = p.get("address", "sin direccion")
        telefono = p.get("phoneNumber", "sin telefono")
        sitio = p.get("website", "")
        extra = f", {sitio}" if sitio else ""
        lineas.append(f"- {nombre} — {direccion} — tel: {telefono}{extra}")
    return "\n".join(lineas)


async def leer_pagina(url: str) -> str:
    """Abre una pagina web especifica y devuelve su texto (recortado), mas
    cualquier email/telefono que se detecte en ella.

    Usar cuando un resultado de buscar_en_internet o buscar_negocios_locales
    no trae suficiente detalle (ej. confirmar un precio exacto, o encontrar
    el contacto de un negocio que no aparecio en buscar_negocios_locales).

    Args:
      url: La URL exacta a abrir, tal como vino en un resultado de busqueda.

    Returns:
      Texto plano de la pagina (recortado a un tamano razonable), con
      cualquier contacto detectado destacado al inicio, o un aviso si la
      pagina no se pudo abrir.
    """
    try:
        # Las redirecciones se siguen a mano para validar cada salto: un sitio
        # publico podria redirigir a una IP de la red local.
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_PAGINA, follow_redirects=False, headers={"User-Agent": _USER_AGENT}
        ) as client:
            destino = httpx.URL(url)
            for _ in range(_MAX_REDIRECCIONES + 1):
                motivo = await _motivo_url_bloqueada(destino)
                if motivo:
                    logger.warning("leer_pagina: bloqueado %r (%s)", str(destino), motivo)
                    return f"No abri la pagina {url}: {motivo}."
                resp = await client.get(destino)
                if not resp.is_redirect:
                    break
                destino = resp.next_request.url
            else:
                return f"No pude abrir la pagina {url}: demasiadas redirecciones."
            resp.raise_for_status()
            html = resp.text
    except httpx.TimeoutException:
        logger.warning("leer_pagina: timeout abriendo %r", url)
        return f"La pagina {url} tardo demasiado en responder."
    except Exception:
        logger.exception("leer_pagina fallo abriendo %r", url)
        return f"No pude abrir la pagina {url}."

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    texto = re.sub(r"\n{2,}", "\n", soup.get_text(separator="\n", strip=True))

    emails = sorted(set(_PATRON_EMAIL.findall(texto)))
    telefonos = sorted(set(_PATRON_TELEFONO.findall(texto)))

    partes = [f"Contenido de {url}:"]
    if emails or telefonos:
        contacto = []
        if emails:
            contacto.append(f"email(s): {', '.join(emails)}")
        if telefonos:
            contacto.append(f"telefono(s): {', '.join(telefonos)}")
        partes.append("Contacto detectado: " + " | ".join(contacto))
    partes.append(texto[:_MAX_CHARS_PAGINA])
    return "\n\n".join(partes)
