"""
web_tools.py - Herramientas de Espartaco para salir a buscar informacion real
en internet (Fase 7): precios, negocios locales (ej. carpinteros) y el
contenido de una pagina especifica.

Mismo patron que google_services.py: cada funcion publica es una tool de
function calling (firma tipada + docstring), nunca lanza al modelo -- si algo
falla (falta la API key, error de red, timeout) devuelve un texto de aviso en
espanol en vez de una excepcion.

Dos proveedores:
- Tavily (https://tavily.com) para la busqueda web general: 1.000 creditos
  gratis al mes que se renuevan (una busqueda "basic" = 1 credito). Si falta
  su key o falla, buscar_en_internet cae a Serper /search como respaldo.
- Serper.dev (https://serper.dev) endpoint /maps para negocios y lugares:
  devuelve direccion, telefono, sitio web, calificacion y horario de atencion
  de Google Maps, asi resuelve tanto "buscar carpinteros con contacto" como
  "a que hora cierra tal lugar".
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
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

_BASE_URL = "https://google.serper.dev"
_TAVILY_URL = "https://api.tavily.com/search"
_MAX_CHARS_FRAGMENTO = 500
_MAX_LUGARES = 8
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


def _falta_serper() -> str | None:
    if not SERPER_API_KEY:
        return "No puedo buscar en Google Maps todavia: falta configurar SERPER_API_KEY en .env."
    return None


async def buscar_en_internet(query: str, num_resultados: int = 5) -> str:
    """Busca en internet (via Tavily, con Serper.dev de respaldo) y devuelve los resultados.

    Args:
      query: Que buscar, en lenguaje natural, ej. 'precio acetaminofen 500 mg Colombia'.
      num_resultados: Cuantos resultados devolver (default 5, maximo util ~10).

    Returns:
      Texto con titulo, link y fragmento de cada resultado encontrado (el
      fragmento a veces ya trae un precio), o un aviso si no hay resultados.
    """
    if not TAVILY_API_KEY and not SERPER_API_KEY:
        return "No puedo buscar en internet todavia: falta configurar TAVILY_API_KEY en .env."

    num_resultados = max(1, min(num_resultados, 10))
    if TAVILY_API_KEY:
        resultado = await _buscar_web_tavily(query, num_resultados)
        if resultado is not None:
            return resultado
        if not SERPER_API_KEY:
            return "No pude completar la busqueda en internet ahora mismo."
        logger.info("buscar_en_internet: Tavily fallo, se usa Serper de respaldo para %r", query)
    return await _buscar_web_serper(query, num_resultados)


async def _buscar_web_tavily(query: str, num_resultados: int) -> str | None:
    """Busqueda via Tavily. Devuelve None si fallo (red, timeout, cupo agotado)
    para que buscar_en_internet pruebe con Serper."""
    body = {
        "query": query,
        "max_results": num_resultados,
        "search_depth": "basic",  # 1 credito; "advanced" cuesta 2
        "topic": "general",
        "country": "colombia",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_BUSQUEDA) as client:
            resp = await client.post(
                _TAVILY_URL,
                headers={"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("buscar_en_internet: timeout de Tavily buscando %r", query)
        return None
    except httpx.HTTPStatusError as e:
        # 432/433 = cupo del plan agotado, 401 = key invalida.
        logger.warning("buscar_en_internet: Tavily respondio %s buscando %r", e.response.status_code, query)
        return None
    except Exception:
        logger.exception("buscar_en_internet: Tavily fallo buscando %r", query)
        return None

    resultados = data.get("results", [])
    if not resultados:
        return f"No encontre resultados para '{query}'."

    lineas = [f"Resultados para '{query}':"]
    for r in resultados[:num_resultados]:
        titulo = r.get("title") or "(sin titulo)"
        link = r.get("url", "")
        fragmento = " ".join((r.get("content") or "").split())[:_MAX_CHARS_FRAGMENTO]
        lineas.append(f"- {titulo}\n  {link}\n  {fragmento}")
    return "\n".join(lineas)


async def _buscar_web_serper(query: str, num_resultados: int) -> str:
    """Busqueda via Serper /search (respaldo de Tavily)."""
    body = {"q": query, "num": num_resultados, "gl": "co", "hl": "es"}
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
        logger.warning("buscar_en_internet: timeout de Serper buscando %r", query)
        return "La busqueda tardo demasiado y se cancelo, intenta de nuevo."
    except Exception:
        logger.exception("buscar_en_internet: Serper fallo buscando %r", query)
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


def _formatear_horario(horario) -> str:
    """openingHours de Serper /maps viene como dict dia -> horas; se tolera
    tambien lista o texto por si cambia el formato."""
    if isinstance(horario, dict):
        return "; ".join(f"{dia}: {horas}" for dia, horas in horario.items())
    if isinstance(horario, list):
        return "; ".join(str(h) for h in horario)
    return str(horario or "")


async def buscar_negocios_locales(query: str, ubicacion: str = "") -> str:
    """Busca en Google Maps (via Serper.dev) negocios o lugares, por tipo
    (ej. carpinteros, plomeros) o por nombre (ej. 'Exito Calle 80'), con su
    direccion, telefono, sitio web, calificacion y horario de atencion.

    Args:
      query: Que tipo de negocio o que lugar buscar, ej. 'carpintero muebles a medida'.
      ubicacion: Ciudad/zona donde buscar (opcional, default Bogota, Colombia).

    Returns:
      Texto con los datos de cada lugar encontrado, o un aviso si no hay resultados.
    """
    aviso = _falta_serper()
    if aviso:
        return aviso

    # /maps no recibe "location" (solo coordenadas en "ll"): la zona va en la consulta.
    consulta = f"{query} en {ubicacion.strip() or SERPER_UBICACION_DEFAULT}"
    body = {"q": consulta, "gl": "co", "hl": "es"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_BUSQUEDA) as client:
            resp = await client.post(
                f"{_BASE_URL}/maps",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("buscar_negocios_locales: timeout buscando %r", consulta)
        return "La busqueda tardo demasiado y se cancelo, intenta de nuevo."
    except Exception:
        logger.exception("buscar_negocios_locales fallo buscando %r", consulta)
        return "No pude completar la busqueda en Google Maps ahora mismo."

    lugares = data.get("places", [])
    if not lugares:
        return f"No encontre lugares en Google Maps para '{consulta}'."

    lineas = [f"Lugares encontrados en Google Maps para '{consulta}':"]
    for p in lugares[:_MAX_LUGARES]:
        nombre = p.get("title", "(sin nombre)")
        categoria = p.get("type", "")
        partes = [f"- {nombre}" + (f" ({categoria})" if categoria else "")]
        partes.append(f"  Direccion: {p.get('address') or 'sin dato'}")
        partes.append(f"  Tel: {p.get('phoneNumber') or 'sin dato'}")
        if p.get("website"):
            partes.append(f"  Web: {p['website']}")
        if p.get("rating"):
            resenas = f" ({p['ratingCount']} resenas)" if p.get("ratingCount") else ""
            partes.append(f"  Calificacion: {p['rating']}{resenas}")
        horario = _formatear_horario(p.get("openingHours"))
        partes.append(f"  Horario: {horario or 'sin dato'}")
        if p.get("cid"):
            partes.append(f"  Mapa: https://maps.google.com/?cid={p['cid']}")
        elif p.get("placeId"):
            partes.append(f"  Mapa: https://www.google.com/maps/place/?q=place_id:{p['placeId']}")
        lineas.append("\n".join(partes))
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
