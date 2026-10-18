#!/usr/bin/env python3
"""
Agente de vuelos: busca los viajes definidos en viajes.json y avisa por Telegram.

Cada viaje indica origen, destino, fecha de salida con flexibilidad, duracion, numero de
adultos y presupuesto por persona. Si el viaje tiene `hub` (ciudad de escala), tambien
compara dos billetes separados (origen-hub + hub-destino + hotel) con el billete directo.

Fuente principal: Google Flights en vivo (no oficial, ver google_flights.py). Respaldo: la API
de datos de Travelpayouts/Aviasales (cache de los ultimos dias, requiere token).
Los precios son orientativos: confirma siempre en el enlace.
"""
import hashlib
import itertools
import json
import os
import statistics
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

try:
    import google_flights as gf
except ImportError:  # faltan dependencias: se usa solo el cache de Travelpayouts
    gf = None

# Valores por defecto de cada viaje; viajes.json solo necesita indicar lo que cambia.
DEFAULTS = {
    "hub": None,                    # ciudad de escala para comparar billetes separados (IATA)
    "flex_dias": 3,                 # se prueba salir entre salida_desde y +N dias
    "dur_min": 21,                  # duracion minima del viaje, en dias
    "dur_max": 28,                  # duracion maxima del viaje, en dias
    "adultos": 1,                   # los precios y totales son los de TODO el grupo
    "noches_hub_opciones": [0, 1],  # noches en el hub a probar en cada sentido (0 = mismo dia)
    "hotel_noche_eur": 60,          # por noche para todo el grupo; ajustalo
    "umbral_por_persona_eur": None, # avisa la primera vez que el precio por persona baja de esto
    "mejora_minima_pct": 5,         # y despues, solo si mejora el ultimo aviso en este %
    "moneda": "eur",
    "top": 3,                       # con mas, el aviso supera los 4000 caracteres de Telegram
}
CAMPOS_OBLIGATORIOS = ("id", "origen", "destino", "salida_desde")

API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
VIAJES_FILE = Path(__file__).with_name("viajes.json")
STATE_FILE = Path(__file__).with_name("estado_viajes.json")


def d(s):
    return date.fromisoformat(s[:10])


def months(d1, d2):
    out, cur = [], date(d1.year, d1.month, 1)
    while cur <= d2:
        out.append(cur.strftime("%Y-%m"))
        cur = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
    return out


def api_get(params):
    token = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Access-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_roundtrips(c, o, dest, dep_months, ret_months):
    """Devuelve {(salida, regreso): oferta mas barata} para ida y vuelta o-dest."""
    best = {}
    for dm in dep_months:
        for rm in ret_months:
            try:
                data = api_get({
                    "origin": o, "destination": dest,
                    "departure_at": dm, "return_at": rm,
                    "one_way": "false", "sorting": "price",
                    "limit": 1000, "unique": "false",
                    "currency": c["moneda"],
                })
            except Exception as e:  # red caida, token invalido, etc.
                print(f"[aviso] {o}-{dest} {dm}/{rm}: {e}", file=sys.stderr)
                continue
            for it in data.get("data", []):
                if not it.get("return_at"):
                    continue
                key = (d(it["departure_at"]), d(it["return_at"]))
                if key not in best or it["price"] < best[key]["price"]:
                    best[key] = it
    return best


def link(it):
    l = it.get("link", "")
    return ("https://www.aviasales.com" + l) if l.startswith("/") else l


def gflights(c, o, dest, ida, vuelta):
    """Enlace a Google Flights con ruta, fechas y pasajeros ya cargados, para confirmar el precio."""
    q = f"Flights from {o} to {dest} on {ida} through {vuelta} for {c['adultos']} adults"
    params = {"q": q, "curr": c["moneda"].upper()}
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote)


def conecta_ida(aqp_lim, lim_mad, margen_min=180):
    """Con billetes separados y 0 noches en el hub, el segundo vuelo debe salir mucho despues de
    que aterrice el primero. Solo se puede comprobar la ida: Google no da el horario de la vuelta."""
    llegada = gf.minutos_dia(aqp_lim["hora_salida"]) + (aqp_lim["duracion_min"] or 0)
    return gf.minutos_dia(lim_mad["hora_salida"]) - llegada >= margen_min


def buscar_vivo(c):
    """Precios en vivo de Google Flights. Devuelve None si Google no responde."""
    d0 = d(c["salida_desde"])
    hub, opciones = c.get("hub"), c["noches_hub_opciones"]
    tramos, unico = [], []
    fallos_seguidos = 0

    def consulta(origen, destino, ida, vuelta):
        nonlocal fallos_seguidos
        try:
            r = gf.mas_barato(origen, destino, ida, vuelta, c["moneda"].upper(), c["adultos"])
            fallos_seguidos = 0
            return r
        except gf.GoogleError as e:
            fallos_seguidos += 1
            print(f"[aviso] Google Flights: {e}", file=sys.stderr)
            if fallos_seguidos >= 3:
                raise
            return None

    try:
        for i in range(c["flex_dias"] + 1):
            dep = d0 + timedelta(i)
            for dur in range(c["dur_min"], c["dur_max"] + 1):
                ret = dep + timedelta(dur)
                b = consulta(c["origen"], hub, dep, ret) if hub else None
                if b:
                    for no, nb in itertools.product(opciones, opciones):
                        dep_hub, ret_dest = dep + timedelta(no), ret - timedelta(nb)
                        a = consulta(hub, c["destino"], dep_hub, ret_dest)
                        if not a:
                            continue
                        if no == 0 and not conecta_ida(b, a):
                            continue
                        hotel = c["hotel_noche_eur"] * (no + nb)
                        tramos.append({
                            "total": a["precio"] + b["precio"] + hotel,
                            "lim_mad": a["precio"], "aqp_lim": b["precio"], "hotel": hotel,
                            "aqp_lim_estimado": False, "noches": (no, nb),
                            "salida": dep, "regreso": ret, "dias": dur,
                            "lim_mad_salida": dep_hub, "mad_lim_salida": ret_dest,
                            "link": "",
                            "detalle": (f"{hub}-{c['destino']}: {', '.join(a['aerolineas'])}, "
                                        f"escalas {a['escalas']} | {c['origen']}-{hub}: "
                                        f"{', '.join(b['aerolineas'])}")})
                m = consulta(c["origen"], c["destino"], dep, ret)
                if m:
                    unico.append({"total": m["precio"], "salida": dep, "regreso": ret,
                                  "dias": dur, "escalas": m["escalas"], "link": "",
                                  "detalle": ", ".join(m["aerolineas"])})
    except gf.GoogleError:
        return None
    print(f"[info] Google Flights: {gf.stats['consultas']} consultas, "
          f"{gf.stats['fallos']} fallidas.", file=sys.stderr)
    tramos.sort(key=lambda x: x["total"])
    unico.sort(key=lambda x: x["total"])
    return tramos[: c["top"]], unico[: c["top"]]


def buscar_vivo_robusto(c, max_procesos=40):
    """Hace la busqueda en vivo en procesos hijos, que se relanzan si el cliente HTTP los mata.

    Cada hijo guarda sus resultados en el cache en disco de google_flights; al terminar uno
    sin caidas, el proceso principal arma el resultado leyendo solo de ese cache.
    """
    if gf is None:
        return None
    for _ in range(max_procesos):
        hijo = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                               "--precalentar", c["id"]])
        if hijo.returncode == 0:
            return buscar_vivo(c)
    print("[aviso] La busqueda en vivo se interrumpio demasiadas veces.", file=sys.stderr)
    return None


def buscar_cache(c):
    d0 = d(c["salida_desde"])
    d1 = d0 + timedelta(c["flex_dias"])
    r0, r1 = d0 + timedelta(c["dur_min"]), d1 + timedelta(c["dur_max"])
    hub, opciones, n = c.get("hub"), c["noches_hub_opciones"], c["adultos"]
    max_n = max(opciones)

    aqp_mad = fetch_roundtrips(c, c["origen"], c["destino"], months(d0, d1), months(r0, r1))
    lim_mad = aqp_lim = {}
    if hub:
        lim_mad = fetch_roundtrips(
            c, hub, c["destino"],
            months(d0, d1 + timedelta(max_n)),
            months(r0 - timedelta(max_n), r1))
        aqp_lim = fetch_roundtrips(c, c["origen"], hub, months(d0, d1), months(r0, r1))

    ref_lim = statistics.median([v["price"] for v in aqp_lim.values()]) if aqp_lim else None

    tramos, unico = [], []
    for no, nb in itertools.product(opciones, opciones):
        hotel = c["hotel_noche_eur"] * (no + nb)
        for (dep_hub, ret_dest), a in lim_mad.items():
            aqp_dep, aqp_ret = dep_hub - timedelta(no), ret_dest + timedelta(nb)
            dur = (aqp_ret - aqp_dep).days
            if not (d0 <= aqp_dep <= d1 and c["dur_min"] <= dur <= c["dur_max"]):
                continue
            b = aqp_lim.get((aqp_dep, aqp_ret))
            estimado = b is None
            precio_b = b["price"] if b else ref_lim
            if precio_b is None:
                continue
            tramos.append({
                "total": (a["price"] + precio_b) * n + hotel,  # el cache da el precio por persona
                "lim_mad": a["price"] * n, "aqp_lim": precio_b * n, "hotel": hotel,
                "aqp_lim_estimado": estimado, "noches": (no, nb),
                "salida": aqp_dep, "regreso": aqp_ret, "dias": dur,
                "lim_mad_salida": dep_hub, "mad_lim_salida": ret_dest,
                "link": link(a),
            })
    for (dep, ret), a in aqp_mad.items():
        dur = (ret - dep).days
        if d0 <= dep <= d1 and c["dur_min"] <= dur <= c["dur_max"]:
            unico.append({"total": a["price"] * n, "salida": dep, "regreso": ret,
                          "dias": dur, "escalas": a.get("transfers"), "link": link(a)})
    tramos.sort(key=lambda x: x["total"])
    unico.sort(key=lambda x: x["total"])
    return tramos[: c["top"]], unico[: c["top"]]


def f(x):
    return f"{x:,.0f}".replace(",", ".")


def adultos_txt(n):
    return f"{n} adulto" + ("" if n == 1 else "s")


def titulo(c):
    return c.get("nombre") or f"{c['origen']} - {c['destino']}"


def mensaje(c, tramos, unico, fuente="cache"):
    en_vivo = fuente == "vivo"
    n, o, dst, hub = c["adultos"], c["origen"], c["destino"], c.get("hub")
    m = f"Vuelos {titulo(c)} ({o} -> {dst}), {adultos_txt(n)} " + (
        "(precios en vivo de Google Flights)\n" if en_vivo else "(precios en cache)\n")
    if hub:
        if tramos:
            m += f"\nMejores combinaciones por tramos (total de {adultos_txt(n)} con hotel):\n"
            for t in tramos:
                est = f" ({o}-{hub} estimado)" if t["aqp_lim_estimado"] else ""
                m += (f"- {f(t['total'])} EUR ({f(t['total'] / n)} por persona) | "
                      f"sale {t['salida']} vuelve {t['regreso']} "
                      f"({t['dias']} dias){est}\n"
                      f"  Noches en {hub} ida/vuelta: {t['noches'][0]}/{t['noches'][1]}\n"
                      f"  {hub}-{dst} ida/vuelta {f(t['lim_mad'])} + {o}-{hub} {f(t['aqp_lim'])} "
                      f"+ hotel {f(t['hotel'])}\n"
                      + (f"  {t['detalle']}\n" if t.get("detalle") else "")
                      + (f"  {t['link']}\n" if t["link"] else "")
                      + f"  Confirmar en vivo (Google Flights):\n"
                      f"  {o}-{hub}: {gflights(c, o, hub, t['salida'], t['regreso'])}\n"
                      f"  {hub}-{dst}: "
                      f"{gflights(c, hub, dst, t['lim_mad_salida'], t['mad_lim_salida'])}\n")
        else:
            m += "\nNo encontre combinaciones por tramos en esas fechas.\n"
    if unico:
        m += f"\nBillete {'unico ' if hub else 'directo '}{o}-{dst}:\n"
        for u in unico[:3]:
            m += (f"- {f(u['total'])} EUR ({f(u['total'] / n)} por persona) | "
                  f"sale {u['salida']} vuelve {u['regreso']} "
                  f"({u['dias']} dias, escalas: {u['escalas']})\n"
                  + (f"  {u['detalle']}\n" if u.get("detalle") else "")
                  + (f"  {u['link']}\n" if u["link"] else "")
                  + f"  Google Flights: {gflights(c, o, dst, u['salida'], u['regreso'])}\n")
    elif not hub:
        m += "\nNo encontre vuelos en esas fechas.\n"
    m += ("\nPrecios en vivo, pero pueden cambiar: confirma al abrir el enlace. " if en_vivo
          else "\nPrecios en cache de buscadores: confirma al abrir el enlace. ")
    if hub:
        m += (f"Con 0 noches en {hub}, la conexion de ida se verifica (3 h minimo) pero la de "
              "vuelta no, y no hay proteccion entre billetes separados.")
    return m


def guia_compra(c, tramos, unico):
    """Segundo mensaje del aviso: como comprar la opcion mas barata sin llevarse sorpresas."""
    n, o, dst, hub = c["adultos"], c["origen"], c["destino"], c.get("hub")
    mejor_t = tramos[0] if tramos else None
    mejor_u = unico[0] if unico else None
    g = "Como comprarlo (lo haces tu, desde tu navegador: el agente nunca compra)\n\n"
    if mejor_t and (not mejor_u or mejor_t["total"] <= mejor_u["total"]):
        g += (f"Opcion elegida: {f(mejor_t['total'])} EUR para {adultos_txt(n)} "
              f"({f(mejor_t['total'] / n)} por persona), en dos billetes separados.\n"
              + (f"{mejor_t['detalle']}\n" if mejor_t.get("detalle") else "") + "\n"
              "1. Abre cada enlace de Google Flights y confirma que el precio sigue igual "
              f"y que figuran {adultos_txt(n)}. Los precios cambian en horas.\n"
              "2. Elige 'Reservar' en la web de la aerolinea si cuesta lo mismo que en una agencia: "
              "los cambios y reclamos son mas faciles.\n"
              f"3. Compra primero el {hub}-{dst}, que es el caro y el que manda en las fechas; "
              f"el {o}-{hub} despues, enseguida.\n"
              "4. Antes de pagar, mira el equipaje: los precios que ves suelen ser la tarifa "
              "basica sin maleta, y una low cost puede cobrarla aparte.\n"
              f"5. Con billetes separados debes recoger tu maleta en {hub} y facturarla de nuevo. "
              "Deja margen: 4 horas o una noche, sobre todo en la vuelta, que el agente no puede "
              f"verificar. Revisa tu mismo la hora de llegada del {dst}-{hub} y la salida del "
              f"{hub}-{o}.\n"
              f"6. Compra los {n} pasajeros en una sola reserva por tramo: si los compras de a "
              "uno, el precio de los siguientes puede subir cuando se agota la tarifa.\n"
              "7. Paga con tarjeta de credito (da mas proteccion) y compara el total en la moneda "
              "de la web y en la de tu banco: el cambio de moneda puede subir el precio.\n")
    elif mejor_u:
        g += (f"Opcion elegida: {f(mejor_u['total'])} EUR para {adultos_txt(n)} "
              f"({f(mejor_u['total'] / n)} por persona), en un solo billete {o}-{dst}.\n"
              + (f"{mejor_u['detalle']}\n" if mejor_u.get("detalle") else "") + "\n"
              "1. Abre el enlace de Google Flights y confirma el precio y los pasajeros.\n"
              "2. Reserva en la web de la aerolinea. Un solo billete te protege si pierdes una "
              "conexion.\n"
              f"3. Compra los {n} pasajeros en una sola reserva: si los compras de a uno, el "
              "precio de los siguientes puede subir.\n"
              "4. Revisa el equipaje y paga con tarjeta de credito, comparando la moneda de la "
              "web con la de tu banco.\n")
    g += ("\nSi al abrir el enlace el precio es mayor, no pasa nada: el agente seguira buscando "
          "y te avisara si baja otra vez.")
    return g


def telegram(texto):
    """Envia el mensaje. Devuelve True solo si Telegram lo acepto."""
    # strip: un secret pegado con espacios, salto de linea o marca BOM rompe la peticion
    tok, chat = (os.environ.get(v, "").strip(chr(0xFEFF) + " \t\r\n")
                 for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))
    if not tok or not chat:
        print("[aviso] Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID: no se envio aviso.")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": texto[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=30)
        return True
    except Exception as e:
        print(f"[aviso] No se pudo enviar el mensaje de Telegram: {e}", file=sys.stderr)
        return False


def cargar_viajes(solo_id=None):
    """Viajes activos de viajes.json, con los valores por defecto aplicados."""
    if not VIAJES_FILE.exists():
        return []
    # utf-8-sig: tolera un BOM si el archivo se edito a mano en Windows
    datos = json.loads(VIAJES_FILE.read_text(encoding="utf-8-sig"))
    viajes = []
    for v in datos.get("viajes", []):
        c = {**DEFAULTS, **v}
        falta = [k for k in CAMPOS_OBLIGATORIOS if not c.get(k)]
        if falta:
            print(f"[aviso] Viaje {v.get('id', '?')} ignorado: faltan {', '.join(falta)}.",
                  file=sys.stderr)
            continue
        if not c.get("activo", True) or (solo_id and c["id"] != solo_id):
            continue
        viajes.append(c)
    return viajes


def firma(c):
    """Cambia cuando cambia algo que hace los precios no comparables con los guardados."""
    campos = ("origen", "destino", "hub", "salida_desde", "flex_dias", "dur_min", "dur_max",
              "adultos", "moneda")
    return hashlib.sha1(json.dumps([c.get(k) for k in campos]).encode()).hexdigest()[:10]


def procesar_viaje(c, estados):
    """Busca un viaje, avisa si corresponde y actualiza su estado dentro de `estados`."""
    if d(c["salida_desde"]) + timedelta(c["flex_dias"]) < date.today():
        print(f"[info] Viaje {c['id']}: las fechas ya pasaron, se omite.", file=sys.stderr)
        return
    resultado = buscar_vivo_robusto(c)
    fuente = "vivo"
    if resultado is None or not (resultado[0] or resultado[1]):
        print("[aviso] Sin datos en vivo de Google Flights: uso el cache de Travelpayouts.",
              file=sys.stderr)
        resultado, fuente = buscar_cache(c), "cache"
    tramos, unico = resultado
    txt = mensaje(c, tramos, unico, fuente)
    print(txt, flush=True)

    # Solo precios confiables deciden el aviso: las combinaciones con precio de tramo estimado
    # (mediana) se muestran, pero no disparan alertas.
    confiables = [t["total"] for t in tramos if not t["aqp_lim_estimado"]]
    confiables += [u["total"] for u in unico]
    mejor_hoy = min(confiables, default=None)
    if mejor_hoy is None:
        return
    # El estado guarda el ultimo precio por el que SI se aviso, no el mejor visto: asi un
    # precio bueno que no llego a Telegram (sin configurar, o con error) se avisa despues.
    estado = estados.get(c["id"], {})
    if estado.get("firma") != firma(c):
        estado = {}  # el viaje cambio: los precios guardados no son comparables
    avisado = estado.get("avisado")
    umbral = c.get("umbral_por_persona_eur")
    if avisado is None:
        avisar = mejor_hoy <= umbral * c["adultos"] if umbral else True
        motivo = "Precio bajo tu presupuesto" if umbral else "Primer precio encontrado"
    else:
        avisar = mejor_hoy <= avisado * (1 - c["mejora_minima_pct"] / 100)
        motivo = "El precio bajo respecto al ultimo aviso"
    if avisar and telegram(f"{titulo(c)} - {motivo}: {f(mejor_hoy)} EUR\n\n{txt}"):
        avisado = mejor_hoy
        telegram(guia_compra(c, tramos, unico))
    estados[c["id"]] = {"firma": firma(c), "mejor": min(mejor_hoy, estado.get("mejor", mejor_hoy)),
                        "avisado": avisado, "fecha": date.today().isoformat()}


def main():
    if "--test-telegram" in sys.argv:
        telegram("Prueba: el agente de vuelos esta conectado.")
        return
    if "--precalentar" in sys.argv:  # modo hijo: solo llena el cache en disco de un viaje
        viaje_id = sys.argv[sys.argv.index("--precalentar") + 1]
        for c in cargar_viajes(viaje_id):
            buscar_vivo(c)
        return
    solo = sys.argv[sys.argv.index("--viaje") + 1] if "--viaje" in sys.argv else None
    viajes = cargar_viajes(solo)
    if not viajes:
        print("[aviso] No hay viajes activos en viajes.json.")
        return
    estados = {}
    if STATE_FILE.exists():
        estados = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    antes = json.dumps(estados, sort_keys=True)
    for c in viajes:
        try:
            procesar_viaje(c, estados)
        except Exception as e:  # un viaje con problemas no debe impedir buscar los demas
            print(f"[aviso] Viaje {c['id']}: {type(e).__name__}: {e}", file=sys.stderr)
    # Solo se reescribe si cambio algo importante; la fecha sola no justifica un commit.
    clave = lambda e: {k: e.get(k) for k in ("firma", "mejor", "avisado")}
    if {i: clave(e) for i, e in estados.items()} != \
            {i: clave(e) for i, e in json.loads(antes).items()}:
        STATE_FILE.write_text(json.dumps(estados, indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
