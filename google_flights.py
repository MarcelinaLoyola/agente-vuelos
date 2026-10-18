"""
Lectura de precios en vivo de Google Flights (no oficial, sin clave de API).

Reutiliza de `fast-flights` solo la construccion de la consulta; la respuesta se lee con un
parser propio y tolerante, porque el de la libreria falla en rutas domesticas (AQP-LIM).
Si Google cambia el formato o bloquea la IP, `vuelos` lanza GoogleError y el agente usa
el cache de Travelpayouts como respaldo.
"""
import json
import os
import tempfile
import time
from datetime import date
from pathlib import Path

from fast_flights import FlightQuery, Passengers, create_query
from primp import Client
from selectolax.lexbor import LexborHTMLParser

URL = "https://www.google.com/travel/flights"
# Sin esta cookie, Google responde con la pagina "Antes de continuar" (consentimiento).
COOKIES = {"SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg", "CONSENT": "YES+"}
PAUSA_S = 1.0          # espera entre consultas reales, para no saturar a Google
REINTENTOS = 2
CONSULTAS_POR_CONEXION = 8

# Cache en disco: el cliente HTTP nativo a veces mata el proceso entero cuando Google corta
# la conexion. Cada resultado se guarda al instante para que un relanzamiento continue donde
# quedo. Una consulta que quedo "en_curso" tras una caida se da por fallida.
CACHE_ARCHIVO = Path(tempfile.gettempdir()) / "agente_vuelos_google.json"
VIGENCIA_S = 3600      # pasado este tiempo, los precios guardados se descartan

_cliente = None
_usos = 0
_cache = {}
stats = {"consultas": 0, "fallos": 0}


def _leer_disco():
    try:
        return json.loads(CACHE_ARCHIVO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar_disco(datos):
    tmp = CACHE_ARCHIVO.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos), encoding="utf-8")
    os.replace(tmp, CACHE_ARCHIVO)


class GoogleError(Exception):
    """Google no devolvio resultados utilizables (consentimiento, bloqueo o formato nuevo)."""


def _get_cliente():
    global _cliente, _usos
    # Google corta las conexiones HTTP/2 que acumulan muchas consultas: se renueva seguido.
    if _cliente is not None and _usos >= CONSULTAS_POR_CONEXION:
        _cliente = None
    if _cliente is None:
        _usos = 0
        _cliente = Client(impersonate="chrome_145", impersonate_os="macos",
                          referer=True, cookie_store=True)
        _cliente.set_cookies("https://www.google.com", COOKIES)
    _usos += 1
    return _cliente


def _ofertas(payload):
    """Extrae las ofertas del payload; descarta las que no se pueden leer."""
    out = []
    for i in (2, 3):  # 2 = "mejores vuelos", 3 = resto de vuelos
        try:
            grupo = payload[i][0]
        except (IndexError, TypeError):
            continue
        for k in grupo or []:
            try:
                vuelo, precio = k[0], k[1][0][1]
                if not isinstance(precio, (int, float)):
                    continue
                tramos = vuelo[2]
                out.append({
                    "precio": precio,
                    "aerolineas": vuelo[1],
                    "escalas": len(tramos) - 1,
                    "origen": vuelo[3], "destino": vuelo[6],
                    "salida": vuelo[4], "hora_salida": vuelo[5],
                    "duracion_min": vuelo[9],
                })
            except (IndexError, TypeError):
                continue
    return sorted(out, key=lambda o: o["precio"])


def vuelos(origen, destino, ida, vuelta=None, moneda="EUR"):
    """Ofertas ordenadas por precio. Con `vuelta`, el precio es el de ida y vuelta."""
    tramos = [FlightQuery(date=ida.isoformat(), from_airport=origen, to_airport=destino)]
    if vuelta:
        tramos.append(FlightQuery(date=vuelta.isoformat(), from_airport=destino,
                                  to_airport=origen))
    q = create_query(flights=tramos, seat="economy",
                     trip="round-trip" if vuelta else "one-way",
                     passengers=Passengers(adults=1), language="es", currency=moneda)
    ultimo = None
    for intento in range(REINTENTOS + 1):
        try:
            html = _get_cliente().get(URL, params=q.params()).text
            nodo = LexborHTMLParser(html).css_first(r"script.ds\:1")
            if nodo is None:
                raise GoogleError("la respuesta no trae datos de vuelos")
            datos = nodo.text().split("data:", 1)[1].rsplit(",", 1)[0]
            if datos.endswith("errorHasStatus: true"):
                return []
            return _ofertas(json.loads(datos))
        except Exception as e:
            ultimo = e
            global _cliente
            _cliente = None  # conexion nueva: tras un error de Google la anterior suele quedar inservible
            time.sleep(PAUSA_S * (intento + 2))
    raise GoogleError(f"{origen}-{destino} {ida}: {ultimo}")


def mas_barato(origen, destino, ida, vuelta=None, moneda="EUR"):
    """Oferta mas barata (o None si no hay vuelos). Cada consulta se hace una sola vez."""
    clave = (origen, destino, ida, vuelta, moneda)
    if clave in _cache:
        return _cache[clave]
    k = "|".join(str(x) for x in clave)
    disco = _leer_disco()
    e = disco.get(k)
    if e and time.time() - e["t"] < VIGENCIA_S:
        if e["estado"] == "ok":
            _cache[clave] = e["v"]
            return e["v"]
        if e["estado"] == "en_curso":  # el proceso murio durante esta consulta
            disco[k] = {"t": time.time(), "estado": "fallo"}
            _guardar_disco(disco)
        stats["fallos"] += 1
        raise GoogleError(f"{origen}-{destino} {ida}: consulta fallida")
    stats["consultas"] += 1
    disco[k] = {"t": time.time(), "estado": "en_curso"}
    _guardar_disco(disco)
    try:
        ofertas = vuelos(origen, destino, ida, vuelta, moneda)
    except GoogleError:
        stats["fallos"] += 1
        disco[k] = {"t": time.time(), "estado": "fallo"}
        _guardar_disco(disco)
        raise
    resultado = ofertas[0] if ofertas else None
    disco[k] = {"t": time.time(), "estado": "ok", "v": resultado}
    _guardar_disco(disco)
    _cache[clave] = resultado
    time.sleep(PAUSA_S)
    return resultado


def minutos_dia(hora):
    """[h, m] de Google (puede venir [8] o [None, 31]) -> minutos desde las 00:00."""
    h = (hora[0] or 0) if hora else 0
    m = (hora[1] or 0) if hora and len(hora) > 1 else 0
    return h * 60 + m


def enlace(origen, destino, ida, vuelta):
    """Enlace a Google Flights con ruta y fechas cargadas, para confirmar a mano."""
    from urllib.parse import quote, urlencode
    q = f"Flights from {origen} to {destino} on {ida} through {vuelta}"
    return f"{URL}?" + urlencode({"q": q, "curr": "EUR"}, quote_via=quote)


if __name__ == "__main__":
    # Prueba rapida: python google_flights.py
    for o, dst, i, v in [("AQP", "LIM", date(2027, 2, 12), date(2027, 3, 6)),
                         ("LIM", "MAD", date(2027, 2, 13), date(2027, 3, 5))]:
        print(o, dst, mas_barato(o, dst, i, v))
