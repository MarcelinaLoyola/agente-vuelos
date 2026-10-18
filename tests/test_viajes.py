import sys, json, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import buscador_vuelos as b

ok_total = True
def check(nombre, cond):
    global ok_total; ok_total &= bool(cond)
    print(("OK  " if cond else "FALLA"), nombre)

tmpdir = pathlib.Path(tempfile.mkdtemp())
b.VIAJES_FILE = tmpdir / "viajes.json"
b.STATE_FILE = tmpdir / "estado_viajes.json"

# --- carga de viajes: BOM, defaults, invalidos, inactivos, filtro por id
viajes = {"viajes": [
    {"id": "madrid", "origen": "AQP", "destino": "MAD", "hub": "LIM", "salida_desde": "2027-02-12", "adultos": 3, "umbral_por_persona_eur": 980},
    {"id": "cusco", "origen": "LIM", "destino": "CUZ", "salida_desde": "2027-05-01", "dur_min": 5, "dur_max": 7},
    {"id": "roto", "origen": "LIM"},
    {"id": "off", "origen": "A", "destino": "B", "salida_desde": "2027-01-01", "activo": False},
]}
b.VIAJES_FILE.write_bytes(b"\xef\xbb\xbf" + json.dumps(viajes).encode())
v = b.cargar_viajes()
check("carga con BOM, ignora invalido e inactivo", [c["id"] for c in v] == ["madrid", "cusco"])
check("aplica valores por defecto", v[1]["adultos"] == 1 and v[1]["flex_dias"] == 3 and v[1]["hub"] is None)
check("filtra por id", [c["id"] for c in b.cargar_viajes("cusco")] == ["cusco"])

# --- avisos por viaje
enviados, guias = [], []
tel_ok = {"v": True}
def fake_tel(t):
    if not tel_ok["v"]: return False
    (guias if t.startswith("Como comprarlo") else enviados).append(t.split("\n")[0]); return True
b.telegram = fake_tel
b.mensaje = lambda *a, **k: "msg"
def u(p): return [{"total": p, "salida": None, "regreso": None, "dias": 5, "escalas": 0, "link": ""}]
def correr(c, precio, estados):
    b.buscar_vivo_robusto = lambda cc: ([], u(precio))
    b.procesar_viaje(c, estados)

madrid, cusco = v
est = {}
correr(madrid, 2664, est)
check("madrid: 2664 <= 980x3 -> avisa", len(enviados) == 1 and est["madrid"]["avisado"] == 2664)
correr(cusco, 300, est)
check("cusco sin umbral: avisa el primer precio", len(enviados) == 2 and "Primer precio" in enviados[1] and est["cusco"]["avisado"] == 300)
correr(madrid, 2640, est); correr(cusco, 290, est)
check("sin mejora del 5%: no avisa, estados independientes", len(enviados) == 2 and est["madrid"]["avisado"] == 2664 and est["cusco"]["avisado"] == 300)
correr(cusco, 250, est)
check("cusco 250 mejora >=5%: avisa solo cusco", len(enviados) == 3 and est["cusco"]["avisado"] == 250 and est["madrid"]["avisado"] == 2664)
check("cada aviso va con su guia", len(guias) == len(enviados))
madrid2 = {**madrid, "adultos": 2}
correr(madrid2, 1900, est)
check("cambia el grupo: estado se reinicia y vuelve a avisar", len(enviados) == 4 and est["madrid"]["avisado"] == 1900)
tel_ok["v"] = False
est2 = {}
correr(madrid, 2700, est2)
check("Telegram caido: guarda mejor pero no marca avisado", est2["madrid"]["mejor"] == 2700 and est2["madrid"]["avisado"] is None)
tel_ok["v"] = True
correr({**madrid, "salida_desde": "2020-01-01"}, 1, est2)
check("fechas pasadas: se omite", est2["madrid"]["mejor"] == 2700)

# --- main: un viaje que falla no impide los demas; el archivo de estado se escribe
import importlib; importlib.reload(b)
b.VIAJES_FILE = tmpdir / "viajes.json"; b.STATE_FILE = tmpdir / "estado_viajes.json"
if b.STATE_FILE.exists(): b.STATE_FILE.unlink()
llamadas = []
b.telegram = lambda t: True
def falso(c):
    llamadas.append(c["id"])
    if c["id"] == "madrid": raise RuntimeError("boom")
    return ([], u(100))
b.buscar_vivo_robusto = falso
sys.argv = ["x"]
b.main()
st = json.loads(b.STATE_FILE.read_text())
check("main: madrid falla, cusco se procesa y se guarda", llamadas == ["madrid", "cusco"] and "cusco" in st and "madrid" not in st)

# --- mensajes reales
importlib.reload(b)
c = {**b.DEFAULTS, "id": "x", "nombre": "Cusco", "origen": "LIM", "destino": "CUZ", "adultos": 2, "salida_desde": "2027-05-01"}
un = [{"total": 400, "salida": "2027-05-01", "regreso": "2027-05-06", "dias": 5, "escalas": 0, "link": "", "detalle": "LATAM"}]
m = b.mensaje(c, [], un, "vivo"); g = b.guia_compra(c, [], un)
check("viaje sin escala: mensaje directo, sin texto de hub", "Cusco (LIM -> CUZ), 2 adultos" in m and "por persona" in m and "conexion" not in m and "tramos" not in m)
check("enlace lleva 2 adultos", "for%202%20adults" in m)
check("guia de billete directo", "un solo billete LIM-CUZ" in g and "200 por persona" in g)
c2 = {**b.DEFAULTS, "id": "m", "origen": "AQP", "destino": "MAD", "hub": "LIM", "adultos": 3, "salida_desde": "2027-02-12"}
t = {"total": 3012, "lim_mad": 2609, "aqp_lim": 343, "hotel": 60, "aqp_lim_estimado": False, "noches": (0, 0),
     "salida": "2027-02-12", "regreso": "2027-03-05", "dias": 21, "lim_mad_salida": "2027-02-12",
     "mad_lim_salida": "2027-03-05", "link": "", "detalle": "x"}
m2 = b.mensaje(c2, [t, t, t], un * 3, "vivo"); g2 = b.guia_compra(c2, [t], un)
check("viaje con escala cabe en Telegram", len(m2) < 4000)
check("viaje con escala menciona hub y tramos", "LIM-MAD ida/vuelta" in m2 and "Noches en LIM" in m2)
print("\nRESULTADO:", "TODO OK" if ok_total else "HAY FALLOS")
