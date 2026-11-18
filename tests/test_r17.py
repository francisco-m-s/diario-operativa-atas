"""R17 y stop calibrado en la preoperativa (spec §13): selección de la rejilla, ventana de 5 velas contiguas, cálculo, texto,
antigüedad, lectura incremental sin tocar los ficheros, escritura de UNA celda con la etiqueta comprobada, pausa tras error,
convivencia con el sync y aislamiento del ciclo del demonio."""

import ast
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import httplib2
import pytest
from conftest import HojaFalsa

import demonio_v4
from atasv4 import almacen, config, fuentes, r17, sheets, vista

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASO = timedelta(seconds=204)
AHORA_SYNC = '2026-09-22 18:00:00.000'     # como en test_preoperativa: la sesión del 22/09 ya terminó


# --- utilidades -----------------------------------------------------------------------------------------------------

def madrid(y, mo, d, h, mi, s):
    """Hora de Madrid -> datetime UTC con zona."""
    return fuentes.madrid_a_utc(datetime(y, mo, d, h, mi, s)).replace(tzinfo=timezone.utc)


def utc(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def linea(ap, h, l, origen='tiempo_real', revision=0):
    """Una línea del indicador (solo los campos que importan, más un perfil de relleno)."""
    return json.dumps({'simbolo': 'NQ', 'marco': '204s', 'apertura': ap.strftime('%Y-%m-%dT%H:%M:%S.000Z'), 'tick': 0.25,
                       'h': h, 'l': l, 'origen': origen, 'revision': revision,
                       'perfil': [{'precio': l, 'volumen': 1, 'bid': 1, 'ask': 0, 'between': 0}]}) + '\n'


def escribir(carpeta, fecha, lineas, modo='w'):
    with open(os.path.join(carpeta, f'{fecha}.jsonl'), modo, encoding='utf-8', newline='') as f:
        f.writelines(lineas)


def epoch(dt):
    return dt.timestamp()


@pytest.fixture
def ent17(tmp_path):
    """Entorno mínimo: reglas.yaml y config.yaml reales; la carpeta de velas es una temporal."""
    base = tmp_path / 'base'
    base.mkdir()
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), base / 'reglas.yaml')
    shutil.copy(os.path.join(RAIZ, 'config.yaml'), base / 'config.yaml')
    ent = config.cargar_entorno(str(base))
    carpeta = tmp_path / 'velas'
    carpeta.mkdir()
    ent.reglas['preoperativa']['r17']['carpeta'] = str(carpeta)
    ent.carpeta_velas = str(carpeta)
    return ent


# Las 5 M204s históricas del 08/10/2026 16:12:24–16:26:00 (Madrid) y las 5 del vivo (otra rejilla, 72 s antes), tal como están
# en D:\...\velas\NQ_204s\2026-10-08.jsonl: (apertura Madrid, H, L)
HIST_8_10 = [((16, 12, 24), 31263.0, 31248.25), ((16, 15, 48), 31281.5, 31251.0), ((16, 19, 12), 31299.0, 31275.0),
             ((16, 22, 36), 31336.75, 31293.5), ((16, 26, 0), 31363.5, 31320.25)]
VIVO_8_10 = [((16, 11, 12), 31263.0, 31244.5), ((16, 14, 36), 31267.75, 31251.0), ((16, 18, 0), 31299.0, 31262.0),
             ((16, 21, 24), 31319.0, 31286.5), ((16, 24, 48), 31339.75, 31313.75)]


def _lineas_8_10(velas, origen):
    return [linea(madrid(2026, 10, 8, *hms), h, l, origen) for hms, h, l in velas]


def _servicio(ent, hoja=None, logs=None):
    return r17.ServicioR17(ent, hoja or HojaR17(), logs.append if logs is not None else (lambda m: None))


class HojaR17:
    """Doble del sheet: rejilla de valores con la fila de la estrategia en la fila 5 (columna I); anota lecturas y escrituras."""

    def __init__(self, fila=5, col=8, filas=10, cols=12):
        self.valores = [[f'x{f},{c}' for c in range(cols)] for f in range(filas)]
        self.valores[fila][0] = 'Estrategia a usar'
        self.valores[fila][col] = 'Sin estrategia'
        self.valores[fila][col + 1] = ''                                    # la celda de R17: vacía hasta que se escribe
        self.lecturas, self.escrituras, self.falla = [], [], None

    def leer_fila_r17(self, fila, col):
        self.lecturas.append((fila, col))
        if self.falla:
            raise self.falla
        return list(self.valores[fila][:col + 1]) if fila < len(self.valores) else []

    def escribir_celda_r17(self, fila, col, texto):
        if self.falla:
            raise self.falla
        self.escrituras.append((fila, col, texto))
        self.valores[fila][col] = texto


# --- cálculo ---------------------------------------------------------------------------------------------------------

def test_ejemplo_del_8_10_con_las_dos_rejillas_mezcladas(ent17):
    """16:12:24–16:26:00 (Madrid): máx 31.363,50, mín 31.248,25 -> 461 ticks. Con las del vivo mezcladas saldría 476."""
    mezcladas = [linea(madrid(2026, 10, 8, *hms), h, l, 'historico' if (hms, h, l) in HIST_8_10 else 'tiempo_real')
                 for hms, h, l in sorted(HIST_8_10 + VIVO_8_10)]                    # entrelazadas, como en el fichero real
    escribir(ent17.carpeta_velas, '2026-10-08', mezcladas)
    sv = _servicio(ent17)
    ahora = epoch(madrid(2026, 10, 8, 16, 29, 30))
    # R17 = (31363,5 − 31248,25) / 0,25 = 461; stop = 1,53 × 461^0,77 = 172,08 -> 172; cierre de la última = 16:26:00 + 204 s
    assert sv.texto(ahora) == 'R17 461 t · stop 172 t · 16:29:24'


def test_gana_la_rejilla_de_la_vela_mas_reciente(ent17):
    """Si la apertura más reciente es del vivo, la ventana son las 5 últimas del vivo (las históricas quedan fuera)."""
    vivo = VIVO_8_10 + [((16, 28, 12), 31369.75, 31335.0)]           # 6 del vivo, contiguas
    lineas = _lineas_8_10(HIST_8_10, 'historico') + _lineas_8_10(vivo, 'tiempo_real')
    escribir(ent17.carpeta_velas, '2026-10-08', lineas)
    # las 5 últimas del vivo: 16:14:36 … 16:28:12 -> máx 31369,75, mín 31251 -> 475 ticks (1,53 × 475^0,77 = 176,1); cierre 16:31:36
    assert _servicio(ent17).texto(epoch(madrid(2026, 10, 8, 16, 31, 40))) == 'R17 475 t · stop 176 t · 16:31:36'


def test_solo_tiempo_real_tambien_cuenta(ent17):
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(VIVO_8_10, 'tiempo_real'))
    # máx 31339,75, mín 31244,5 -> 381 ticks (1,53 × 381^0,77 = 148,6); cierre 16:24:48 + 204 s = 16:28:12
    assert _servicio(ent17).texto(epoch(madrid(2026, 10, 8, 16, 28, 20))) == 'R17 381 t · stop 149 t · 16:28:12'


def test_ultima_version_de_cada_apertura(ent17):
    base = madrid(2026, 10, 8, 16, 0, 0)
    velas = [(base + i * PASO, 31000.0 + i, 30900.0 + i) for i in range(5)]
    lineas = [linea(a, h, l) for a, h, l in velas]
    lineas.insert(2, linea(velas[2][0], 31999.0, 30001.0, revision=0))        # versión vieja con extremos absurdos
    lineas.append(linea(velas[2][0], velas[2][1], velas[2][2], 'historico', revision=1))   # la buena, la última del fichero
    escribir(ent17.carpeta_velas, '2026-10-08', lineas)
    # máx 31004, mín 30900 -> 416 ticks
    assert _servicio(ent17).texto(epoch(base + 5 * PASO + timedelta(seconds=10))).startswith('R17 416 t')


def test_hueco_menos_de_cinco_contiguas(ent17):
    """10 velas con la 8.ª ausente: las 5 más recientes no son contiguas -> sin datos, desde el cierre de la última ventana buena."""
    base = madrid(2026, 10, 8, 16, 0, 0)
    velas = [base + i * PASO for i in range(10) if i != 7]
    escribir(ent17.carpeta_velas, '2026-10-08', [linea(a, 31000.0, 30900.0) for a in velas])
    ahora = epoch(base + 10 * PASO + timedelta(seconds=10))                   # la última (i = 9) es reciente
    # la última ventana válida acaba en i = 6; cierre = base + 7 × 204 s = 16:23:48
    assert _servicio(ent17).texto(ahora) == 'R17 sin datos desde 16:23:48'


def test_menos_de_cinco_en_total_es_sin_datos_a_secas(ent17):
    base = madrid(2026, 10, 8, 16, 0, 0)
    escribir(ent17.carpeta_velas, '2026-10-08', [linea(base + i * PASO, 31000.0, 30900.0) for i in range(4)])
    assert _servicio(ent17).texto(epoch(base + 4 * PASO)) == 'R17 sin datos'


def test_sin_ficheros_es_sin_datos(ent17):
    assert _servicio(ent17).texto(epoch(madrid(2026, 10, 8, 16, 0, 0))) == 'R17 sin datos'


def test_cruce_de_medianoche_utc_usa_el_fichero_del_dia_anterior(ent17):
    a = [utc(2026, 10, 9, 23, 49, 36) + i * PASO for i in range(7)]      # a4 = 00:03:12 ya es del día 10
    assert [x.day for x in a] == [9, 9, 9, 9, 10, 10, 10]
    escribir(ent17.carpeta_velas, '2026-10-09', [linea(x, 31000.0 + i, 30900.0 + i) for i, x in enumerate(a[:4])])
    escribir(ent17.carpeta_velas, '2026-10-10', [linea(x, 31000.0 + i, 30900.0 + i) for i, x in enumerate(a[4:], 4)])
    ahora = epoch(utc(2026, 10, 10, 0, 13, 30))
    # ventana a2…a6: máx 31006, mín 30902 -> 416 ticks (1,53 × 416^0,77 = 159,0); cierre de a6 = 00:13:24 UTC = 02:13:24 Madrid
    assert _servicio(ent17).texto(ahora) == 'R17 416 t · stop 159 t · 02:13:24'
    os.remove(os.path.join(ent17.carpeta_velas, '2026-10-09.jsonl'))           # sin el de ayer solo hay 3: sin datos
    assert _servicio(ent17).texto(ahora) == 'R17 sin datos'


def test_antiguedad_maxima_y_borde(ent17):
    base = madrid(2026, 10, 8, 16, 0, 0)
    escribir(ent17.carpeta_velas, '2026-10-08', [linea(base + i * PASO, 31000.0 + i, 30900.0) for i in range(5)])
    cierre = base + 5 * PASO                                                 # 16:17:00 Madrid
    sv = _servicio(ent17)
    # (31004 − 30900) / 0,25 = 416 ticks; 1,53 × 416^0,77 = 159,0. A 2 × 204 + 30 s exactos aún vale
    assert sv.texto(epoch(cierre) + 438) == 'R17 416 t · stop 159 t · 16:17:00'
    assert sv.texto(epoch(cierre) + 438.5) == 'R17 sin datos desde 16:17:00'
    assert sv.texto(epoch(cierre) + 3600) == 'R17 sin datos desde 16:17:00'


def test_hora_de_madrid_en_invierno_y_verano(ent17):
    for dia, base, esperada in ((15, utc(2026, 1, 15, 15, 0, 0), '16:'), (15, utc(2026, 7, 15, 15, 0, 0), '17:')):
        carpeta = os.path.join(ent17.carpeta_velas, str(base.month))
        os.makedirs(carpeta)
        ent17.reglas['preoperativa']['r17']['carpeta'] = carpeta
        escribir(carpeta, base.strftime('%Y-%m-%d'), [linea(base + i * PASO, 31000.0, 30900.0) for i in range(5)])
        texto = _servicio(ent17).texto(epoch(base + 5 * PASO))
        assert texto.endswith('· ' + esperada + '17:00')


@pytest.mark.parametrize('ticks,stop', [(0, 0), (100, 53), (300, 124), (450, 169), (461, 172), (600, 211), (1000, 312)])
def test_stop_calibrado(ticks, stop):
    """Valores a mano de 1,53 × R17^0,77 al entero (p. ej. 461 -> 172,08). La tabla del diseño dice 169 para 450, no para 461."""
    ventana = [r17.Vela(0, ticks * 0.25 + 31000.0, 31000.0)]
    assert r17.calcular(ventana, 0.25, 1.53, 0.77) == (ticks, stop)


def test_r17_son_ticks_enteros_de_la_ventana_entera():
    v = [r17.Vela(0, 100.5, 99.0), r17.Vela(1, 101.25, 99.75), r17.Vela(2, 100.0, 98.5)]
    assert r17.calcular(v, 0.25, 1.53, 0.77)[0] == 11                        # (101,25 − 98,5) / 0,25


def test_formato_del_texto_viene_de_reglas(ent17):
    C = ent17.reglas['preoperativa']['r17']
    assert C['texto'].format(r17=461, stop=172, hora='16:29:24') == 'R17 461 t · stop 172 t · 16:29:24'
    assert C['sin_datos_desde'].format(hora='16:29:24') == 'R17 sin datos desde 16:29:24'
    assert C['sin_datos'] == 'R17 sin datos'
    assert (C['marco_s'], C['velas'], C['tick'], C['stop'], C['antiguedad_max_s']) == (
        204, 5, 0.25, {'coef': 1.53, 'exponente': 0.77}, 2 * 204 + 30)
    assert C['tick'] == ent17.reglas['simbolos']['NQ']['tick']               # un solo sitio para el tick del NQ


def test_ninguna_regla_de_negocio_en_el_codigo():
    """Regla de oro del proyecto: lo que va en reglas.yaml no está en el .py."""
    arbol = ast.parse(open(os.path.join(RAIZ, 'atasv4', 'r17.py'), encoding='utf-8').read())
    docstrings = {id(n.body[0].value) for n in ast.walk(arbol) if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    constantes = [n.value for n in ast.walk(arbol) if isinstance(n, ast.Constant) and id(n) not in docstrings]
    assert not {204, 1.53, 0.77, 0.25, 438} & {c for c in constantes if isinstance(c, (int, float))}
    for literal in ('Estrategia a usar', 'sin datos', 'NQ_204s', 'stop {', 'R17 {'):
        assert not [c for c in constantes if isinstance(c, str) and literal in c], literal


def test_analizar_devuelve_ultima_ventana_y_valida():
    marco = 204_000
    v = {i * marco: r17.Vela(i * marco, 1.0, 0.0) for i in (0, 1, 2, 3, 4, 5, 6, 8, 9)}
    ultima, ventana, valida = r17.analizar(v, marco, 5)
    assert ultima.ap_ms == 9 * marco and ventana is None and valida.ap_ms == 6 * marco
    v[7 * marco] = r17.Vela(7 * marco, 1.0, 0.0)
    ultima, ventana, valida = r17.analizar(v, marco, 5)
    assert [x.ap_ms // marco for x in ventana] == [5, 6, 7, 8, 9] and valida.ap_ms == 9 * marco
    assert r17.analizar({}, marco, 5) == (None, None, None)
    # una vela fuera de rejilla (+72 s) no cuenta, ni como hueco ni como vela
    v[3 * marco + 72_000] = r17.Vela(3 * marco + 72_000, 99.0, 0.0)
    assert [x.ap_ms // marco for x in r17.analizar(v, marco, 5)[1]] == [5, 6, 7, 8, 9]


# --- lectura incremental -----------------------------------------------------------------------------------------------

def test_lector_incremental_no_relee_lo_ya_leido(tmp_path):
    ruta = str(tmp_path / '2026-10-08.jsonl')
    base = utc(2026, 10, 8, 14, 0, 0)
    with open(ruta, 'w', newline='') as f:
        f.writelines(linea(base + i * PASO, 31000.0, 30900.0) for i in range(3))
    lector = r17.LectorDia(ruta)
    lector.leer()
    assert len(lector.velas) == 3 and lector.pos == os.path.getsize(ruta)
    # se cambia EN SITIO (misma longitud) la primera línea: un lector que releyera el fichero vería 31999,0
    with open(ruta, 'r+b') as f:
        datos = f.read()
        f.seek(0)
        f.write(datos.replace(b'31000.0', b'31999.0', 1))
    with open(ruta, 'a', newline='') as f:
        f.write(linea(base + 3 * PASO, 31001.0, 30901.0))
    lector.leer()
    assert len(lector.velas) == 4 and lector.velas[r17.a_ms('2026-10-08T14:00:00.000Z')].h == 31000.0


def test_lector_espera_a_la_linea_completa(tmp_path):
    ruta = str(tmp_path / 'd.jsonl')
    l1, l2 = linea(utc(2026, 10, 8, 14, 0, 0), 31000.0, 30900.0), linea(utc(2026, 10, 8, 14, 3, 24), 31001.0, 30901.0)
    with open(ruta, 'w', newline='') as f:
        f.write(l1 + l2[:40])                                               # la segunda, a medio escribir
    lector = r17.LectorDia(ruta)
    lector.leer()
    assert len(lector.velas) == 1 and lector.pos == len(l1) and lector.corruptas == 0
    with open(ruta, 'a', newline='') as f:
        f.write(l2[40:])
    lector.leer()
    assert len(lector.velas) == 2 and lector.corruptas == 0


def test_lector_salta_lineas_corruptas_y_se_reinicia_si_el_fichero_se_encoge(tmp_path):
    ruta = str(tmp_path / 'd.jsonl')
    ok = linea(utc(2026, 10, 8, 14, 0, 0), 31000.0, 30900.0)
    with open(ruta, 'w', newline='') as f:
        f.write('basura\n[1, 2]\n{"apertura": "no es una fecha", "h": 1, "l": 0}\n{"h": 1, "l": 0}\n\n' + ok)
    lector = r17.LectorDia(ruta)
    lector.leer()
    assert len(lector.velas) == 1 and lector.corruptas == 4
    otra = linea(utc(2026, 10, 8, 15, 0, 0), 31500.0, 31400.0)
    with open(ruta, 'w', newline='') as f:
        f.write(otra)                                                        # sustituido por uno más corto
    lector.leer()
    assert list(v.h for v in lector.velas.values()) == [31500.0]


def test_lector_fichero_inexistente_no_falla(tmp_path):
    lector = r17.LectorDia(str(tmp_path / 'no_existe.jsonl'))
    lector.leer()
    assert lector.velas == {}


def test_nunca_se_modifica_ni_se_abre_para_escribir(ent17, monkeypatch):
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    ruta = os.path.join(ent17.carpeta_velas, '2026-10-08.jsonl')
    antes = (hashlib.sha256(open(ruta, 'rb').read()).hexdigest(), os.stat(ruta).st_mtime_ns)
    modos = []
    real = open

    def espia(archivo, modo='r', *a, **k):
        modos.append((str(archivo), modo))
        return real(archivo, modo, *a, **k)

    monkeypatch.setattr(r17, 'open', espia, raising=False)
    sv = _servicio(ent17)
    for _ in range(3):
        sv.texto(epoch(madrid(2026, 10, 8, 16, 29, 30)))
    assert modos and all(m == 'rb' for _, m in modos)
    assert (hashlib.sha256(open(ruta, 'rb').read()).hexdigest(), os.stat(ruta).st_mtime_ns) == antes


# --- escritura: una celda, con la etiqueta comprobada ------------------------------------------------------------------

def _preparar(ent17, hoja=None, logs=None):
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    hoja = hoja or HojaR17()
    sv = _servicio(ent17, hoja, logs)
    sv.fijar_celda('I6')                                                    # (fila 5, columna 8)
    return sv, hoja, epoch(madrid(2026, 10, 8, 16, 29, 30))


def test_escribe_solo_la_celda_de_la_derecha_de_la_estrategia(ent17):
    sv, hoja, ahora = _preparar(ent17)
    antes = [list(r) for r in hoja.valores]
    sv.paso(ahora)
    despues = hoja.valores
    cambios = [(f, c) for f in range(len(antes)) for c in range(len(antes[f])) if antes[f][c] != despues[f][c]]
    assert cambios == [(5, 9)] and despues[5][9] == 'R17 461 t · stop 172 t · 16:29:24'
    assert hoja.escrituras == [(5, 9, 'R17 461 t · stop 172 t · 16:29:24')]
    assert despues[5][8] == 'Sin estrategia'                              # la celda de la estrategia, intacta
    assert sv.texto_escrito() == 'R17 461 t · stop 172 t · 16:29:24'


def test_solo_escribe_cuando_el_texto_cambia(ent17):
    sv, hoja, ahora = _preparar(ent17)
    sv.paso(ahora)
    for k in range(1, 40):                                                  # 40 pasos de 5 s: el texto no cambia
        sv.paso(ahora + 5 * k)
    assert len(hoja.escrituras) == 1 and len(hoja.lecturas) == 1            # ni se vuelve a escribir ni a leer el sheet
    # entra la vela siguiente: el texto cambia (16:15:48…16:29:24: máx 31369,75, mín 31251 -> 475 ticks) y se escribe una vez más
    escribir(ent17.carpeta_velas, '2026-10-08', [linea(madrid(2026, 10, 8, 16, 29, 24), 31369.75, 31335.25, 'historico')], 'a')
    sv.paso(ahora + 204)
    sv.paso(ahora + 209)
    assert [e[2] for e in hoja.escrituras] == ['R17 461 t · stop 172 t · 16:29:24', 'R17 475 t · stop 176 t · 16:32:48']


def test_pasa_a_sin_datos_una_sola_vez(ent17):
    sv, hoja, ahora = _preparar(ent17)
    sv.paso(ahora)
    for k in range(1, 30):                                                  # el mercado se para: ya no entran velas
        sv.paso(ahora + 60 * k)
    textos = [e[2] for e in hoja.escrituras]
    assert textos == ['R17 461 t · stop 172 t · 16:29:24', 'R17 sin datos desde 16:29:24']


def test_etiqueta_distinta_no_se_escribe_y_espera_al_sync(ent17):
    sv, hoja, ahora = _preparar(ent17)
    hoja.valores.insert(5, ['otra fila'] * 12)                              # alguien inserta una fila: la estrategia baja a la 6
    snapshot = [list(r) for r in hoja.valores]
    logs = []
    sv.log = logs.append
    sv.paso(ahora)
    assert hoja.escrituras == [] and hoja.valores == snapshot               # no escribe nada, en ninguna celda
    assert len(logs) == 1 and 'no se escribe hasta el siguiente sync' in logs[0]
    leidas = len(hoja.lecturas)
    sv.paso(ahora + 5)                                                      # en pausa: ni siquiera vuelve a leer el sheet
    assert len(hoja.lecturas) == leidas and len(logs) == 1
    sv.fijar_celda('I7')                                                    # el sync recoloca la fila y avisa a R17
    sv.paso(ahora + 6)
    assert hoja.escrituras == [(6, 9, 'R17 461 t · stop 172 t · 16:29:24')]


def test_sin_celda_conocida_no_hace_nada(ent17):
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    hoja = HojaR17()
    _servicio(ent17, hoja).paso(epoch(madrid(2026, 10, 8, 16, 29, 30)))
    assert hoja.lecturas == [] and hoja.escrituras == []


def test_si_el_sheet_ya_tiene_el_texto_no_lo_reescribe(ent17):
    sv, hoja, ahora = _preparar(ent17)
    hoja.valores[5][9] = 'R17 461 t · stop 172 t · 16:29:24'
    sv.paso(ahora)
    assert hoja.escrituras == [] and sv.texto_escrito() == 'R17 461 t · stop 172 t · 16:29:24'


def test_fallo_de_red_pausa_y_un_log_por_minuto(ent17):
    logs = []
    sv, hoja, ahora = _preparar(ent17, logs=logs)
    hoja.falla = ConnectionError('sin red')
    sv.paso(ahora)                                                          # no lanza
    assert logs[0].startswith('[R17 ERROR] ConnectionError') and sv.texto_escrito() == ''
    sv.paso(ahora + 5)
    sv.paso(ahora + 29)
    assert len(hoja.lecturas) == 1                                          # en pausa 30 s
    sv.paso(ahora + 31)                                                     # acabada la pausa: reintenta (y falla otra vez)
    assert len(hoja.lecturas) == 2 and len(logs) == 1                       # pero solo un log por minuto
    hoja.falla = None
    sv.paso(ahora + 62)
    assert sv.texto_escrito() == 'R17 461 t · stop 172 t · 16:29:24'


def test_fallo_de_fichero_o_de_calculo_no_lanza(ent17, monkeypatch):
    logs = []
    sv, hoja, ahora = _preparar(ent17, logs=logs)
    monkeypatch.setattr(r17.LectorDia, 'leer', lambda self: (_ for _ in ()).throw(PermissionError('bloqueado')))
    sv.paso(ahora)
    assert logs[-1].startswith('[R17 ERROR] PermissionError') and hoja.escrituras == []
    monkeypatch.undo()
    monkeypatch.setattr(r17, 'calcular', lambda *a: (_ for _ in ()).throw(ZeroDivisionError('x')))
    sv.paso(ahora + 100)
    assert logs[-1].startswith('[R17 ERROR] ZeroDivisionError') and hoja.escrituras == []


# --- el sheet real: rangos y una sola celda ----------------------------------------------------------------------------

class _Peticion:
    def __init__(self, res):
        self.res = res

    def execute(self):
        return self.res


class _ServicioFalso:
    def __init__(self, fila):
        self.llamadas, self.fila = [], fila

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kw):
        self.llamadas.append(('get', kw))
        return _Peticion({'values': [self.fila]})

    def update(self, **kw):
        self.llamadas.append(('update', kw))
        return _Peticion({})

    def __getattr__(self, nombre):                                          # cualquier otra llamada a la API sería un fallo
        raise AssertionError(f'llamada inesperada a la API: {nombre}')


def test_hoja_v4_lee_una_fila_y_escribe_una_celda_raw():
    falso = _ServicioFalso(['Estrategia a usar', '', '', '', '', '', '', '', 'Sin estrategia'])
    h = sheets.HojaV4.__new__(sheets.HojaV4)
    h.sid, h.q, h._svc_r17 = 'SID', "'V4'", falso
    assert h.leer_fila_r17(16, 9)[0] == 'Estrategia a usar'
    h.escribir_celda_r17(16, 9, 'R17 461 t · stop 172 t · 16:29:24')
    (op1, kw1), (op2, kw2) = falso.llamadas
    assert (op1, kw1['range'], kw1['valueRenderOption']) == ('get', "'V4'!A17:J17", 'UNFORMATTED_VALUE')
    assert (op2, kw2['range'], kw2['valueInputOption']) == ('update', "'V4'!J17", 'RAW')
    assert kw2['body'] == {'values': [['R17 461 t · stop 172 t · 16:29:24']]} and kw2['spreadsheetId'] == 'SID'


def test_hoja_v4_fila_vacia():
    h = sheets.HojaV4.__new__(sheets.HojaV4)
    h.sid, h.q, h._svc_r17 = 'SID', "'V4'", _ServicioFalso([])
    h._svc_r17.fila = None
    h._svc_r17.get = lambda **kw: _Peticion({})
    assert h.leer_fila_r17(3, 9) == []


# --- convivencia con el sync -------------------------------------------------------------------------------------------

def test_el_sync_no_borra_la_celda_de_r17_y_le_dice_donde_esta_la_fila(entorno_hoy, ent17):
    ent, con, s = entorno_hoy
    ent.reglas['preoperativa']['r17']['carpeta'] = ent17.carpeta_velas
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    sv = r17.ServicioR17(ent, s.hoja, lambda m: None)
    s.r17 = sv
    _, lay = s.sync(AHORA_SYNC)
    col = lay['preop_col'] + 1
    assert lay['valores'][lay['preop_fila']][col] == ''                     # aún no hay texto
    assert almacen.get_estado(con, 'preop_celda') == vista.celda_a1(lay['preop_fila'], lay['preop_col']) == sv._celda
    sv.paso(epoch(madrid(2026, 10, 8, 16, 29, 30)))
    texto = 'R17 461 t · stop 172 t · 16:29:24'
    assert s.hoja.escrituras_r17 == [(lay['preop_fila'], col, texto)]
    # un sync posterior reescribe la pestaña entera y la celda sigue con el último texto
    _, lay2 = s.sync(AHORA_SYNC)
    assert lay2['valores'][lay2['preop_fila']][col] == texto and s.hoja.valores[lay2['preop_fila']][col] == texto
    # lo demás de la fila de la preoperativa es lo de siempre
    fila = lay2['valores'][lay2['preop_fila']]
    assert fila[0] == 'Estrategia a usar' and fila[lay2['preop_col']] == 'Sin estrategia'
    assert all(v == '' for i, v in enumerate(fila) if i not in (0, lay2['preop_col'], col))


def test_sin_servicio_r17_el_layout_no_cambia(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA_SYNC)
    assert s.r17 is None and lay['valores'][lay['preop_fila']][lay['preop_col'] + 1] == ''
    lay_b = vista.construir(s.calcular(AHORA_SYNC), ent, {}, [], 'Sin estrategia')
    vista.poner_r17(lay_b, '')
    assert lay_b['valores'][lay_b['preop_fila']][lay_b['preop_col'] + 1] == ''


def test_el_sync_no_espera_a_un_r17_colgado(entorno_hoy, ent17):
    """R17 con el cerrojo cogido (red colgada): el sync espera como mucho r17_espera_sync_s y escribe igualmente."""
    ent, con, s = entorno_hoy
    ent.cfg['r17_espera_sync_s'] = 0.3
    sv = r17.ServicioR17(ent, s.hoja, lambda m: None)
    s.r17 = sv
    soltar = threading.Event()
    tomado = threading.Event()

    def colgado():
        with sv.cerrojo:
            tomado.set()
            soltar.wait(5)

    hilo = threading.Thread(target=colgado)
    hilo.start()
    assert tomado.wait(2)
    try:
        t0 = time.monotonic()
        _, lay = s.sync(AHORA_SYNC)
        espera = time.monotonic() - t0
        assert 0.25 <= espera < 3 and s.hoja.escrito is lay                 # esperó el plazo y escribió la pestaña
    finally:
        soltar.set()
        hilo.join()
    assert sv.cerrojo.acquire(timeout=1)                                    # el cerrojo del hilo colgado sigue siendo suyo y se libera
    sv.cerrojo.release()


# --- aislamiento del ciclo del demonio ---------------------------------------------------------------------------------

class _SyncFalso:
    def ingerir(self):
        return 0

    def vigilar(self, avisar):
        class R:
            filas_abiertas = []
        return R()

    def pendiente(self):
        return False

    def vigilar_preoperativa(self):
        return None


def test_un_r17_colgado_no_retrasa_el_ciclo(entorno_hoy, ent17):
    ent, con, s = entorno_hoy
    ent.reglas['preoperativa']['r17']['carpeta'] = ent17.carpeta_velas
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    liberar, dentro = threading.Event(), threading.Event()

    class HojaColgada(HojaR17):
        def leer_fila_r17(self, fila, col):
            dentro.set()
            liberar.wait(10)                                                # la red no contesta
            return super().leer_fila_r17(fila, col)

    ent.cfg['r17_intervalo_s'] = 0.01
    sv = r17.ServicioR17(ent, HojaColgada(), lambda m: None)
    sv.fijar_celda('I6')
    sv.paso = (lambda original: lambda ahora_s=None: original(epoch(madrid(2026, 10, 8, 16, 29, 30))))(sv.paso)
    parar = threading.Event()
    hilo = threading.Thread(target=sv.bucle, args=(parar,), daemon=True)
    hilo.start()
    try:
        assert dentro.wait(3)                                               # el hilo de R17 está bloqueado en la red
        ciclo = demonio_v4.Ciclo(ent, con, _SyncFalso(), lambda m: None, avisar=lambda *a: None)
        t0 = time.monotonic()
        for _ in range(20):
            ciclo.paso()
        assert time.monotonic() - t0 < 1.0                                  # el ciclo no se entera
    finally:
        parar.set()
        liberar.set()
        hilo.join(5)
    assert not hilo.is_alive()


def test_iniciar_r17_arranca_un_hilo_daemon_que_escribe_y_se_para(entorno_hoy, ent17):
    ent, con, s = entorno_hoy
    ent.reglas['preoperativa']['r17']['carpeta'] = ent17.carpeta_velas
    ent.cfg['r17_intervalo_s'] = 0.01
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    almacen.set_estado(con, 'preop_celda', 'I6')
    hoja = HojaR17()
    sv = r17.ServicioR17(ent, hoja, lambda m: None)
    sv.paso = (lambda original: lambda ahora_s=None: original(epoch(madrid(2026, 10, 8, 16, 29, 30))))(sv.paso)
    parar = threading.Event()
    hilo = demonio_v4.iniciar_r17(sv, con, parar)
    try:
        assert hilo.daemon and hilo.name == 'R17'
        limite = time.monotonic() + 3
        while not hoja.escrituras and time.monotonic() < limite:
            time.sleep(0.01)
        assert hoja.escrituras == [(5, 9, 'R17 461 t · stop 172 t · 16:29:24')]
    finally:
        parar.set()
        hilo.join(3)
    assert not hilo.is_alive()


# =====================================================================================================================
# Revisión independiente (09/10/2026): plazo de pared (M1), hilo que no muere (B1), configuración mala (B2), celda destino
# (B3), el sync conserva el texto (B4) y los tests que faltaban (B8)
# =====================================================================================================================

def _colgada(liberar, dentro=None):
    """Hoja cuya lectura no vuelve hasta `liberar` (la red goteando): anota la lectura y avisa en `dentro`."""
    class Colgada(HojaR17):
        def leer_fila_r17(self, fila, col):
            self.lecturas.append((fila, col))
            if dentro is not None:
                dentro.set()
            liberar.wait(10)
            return list(self.valores[fila][:col + 1])
    return Colgada()


def _servicio_con_cerrojo_en_el_log(ent, hoja, logs):
    sv = r17.ServicioR17(ent, hoja, lambda m: logs.append((m, sv.cerrojo.locked())))
    sv.fijar_celda('I6')
    return sv


def test_m1_peticion_colgada_se_abandona_con_plazo_de_pared_y_suelta_el_cerrojo(ent17):
    ent17.cfg.update({'r17_plazo_s': 0.3, 'r17_espera_sync_s': 2})
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    liberar, dentro = threading.Event(), threading.Event()
    hoja, logs = _colgada(liberar, dentro), []
    sv = _servicio_con_cerrojo_en_el_log(ent17, hoja, logs)
    ahora = epoch(madrid(2026, 10, 8, 16, 29, 30))
    t0 = time.monotonic()
    sv.paso(ahora)
    assert 0.25 <= time.monotonic() - t0 < 1.5                              # vuelve al cumplirse el plazo, no cuando conteste la red
    assert dentro.is_set() and not sv.cerrojo.locked()                      # el cerrojo se ha soltado
    assert logs and logs[0][0].startswith('[R17 ERROR] TimeoutError') and logs[0][1] is False   # y el log no se hizo con él cogido
    # mientras la petición abandonada siga viva no se lanza otra (un hilo colgado por cada intento acabaría con la máquina)
    leidas = len(hoja.lecturas)
    sv.paso(ahora + 31)
    assert len(hoja.lecturas) == leidas
    liberar.set()
    sv._red.join(2)
    assert hoja.escrituras == []                                            # la abandonada, al volver, no escribe
    sv.paso(ahora + 62)
    assert hoja.escrituras == [(5, 9, 'R17 461 t · stop 172 t · 16:29:24')]  # y R17 sigue funcionando


def test_m1_el_log_de_etiqueta_y_de_destino_no_se_hace_con_el_cerrojo_cogido(ent17):
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    logs = []
    hoja = HojaR17()
    sv = _servicio_con_cerrojo_en_el_log(ent17, hoja, logs)
    ahora = epoch(madrid(2026, 10, 8, 16, 29, 30))
    hoja.valores[5][9] = 'otra cosa'
    sv.paso(ahora)                                                          # destino con otro contenido
    sv.fijar_celda('I7')                                                    # etiqueta que no está
    sv.paso(ahora + 1)
    assert len(logs) == 2 and all(bloqueado is False for _, bloqueado in logs)


def test_m1_el_sync_espera_como_mucho_el_plazo_de_la_peticion(entorno_hoy, ent17):
    ent, con, s = entorno_hoy
    ent.cfg.update({'r17_plazo_s': 0.4, 'r17_espera_sync_s': 6})
    ent.reglas['preoperativa']['r17']['carpeta'] = ent17.carpeta_velas
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    liberar, dentro = threading.Event(), threading.Event()
    sv = r17.ServicioR17(ent, _colgada(liberar, dentro), lambda m: None)
    sv.fijar_celda('I6')
    s.r17 = sv
    t0 = time.monotonic()
    s.sync(AHORA_SYNC)
    base = time.monotonic() - t0                                            # el sync solo
    hilo = threading.Thread(target=sv.paso, args=(epoch(madrid(2026, 10, 8, 16, 29, 30)),))
    hilo.start()
    try:
        assert dentro.wait(2)                                               # R17 está en la red con el cerrojo cogido
        t0 = time.monotonic()
        _, lay = s.sync(AHORA_SYNC)
        con_r17 = time.monotonic() - t0
    finally:
        liberar.set()
        hilo.join(3)
    assert s.hoja.escrito is lay
    assert con_r17 < base + 0.4 + 1.5 and con_r17 < 5                       # esperó el plazo de la petición, no los 6 s del sync


def test_m1_el_plazo_nunca_supera_la_espera_del_sync(ent17):
    ent17.cfg.update({'r17_plazo_s': 20, 'r17_espera_sync_s': 3})
    assert _servicio(ent17).plazo_s == 3                                    # el cerrojo se suelta antes de que el sync deje de esperar


class _Goteo:
    """Servidor HTTP local que responde con `n` bytes a razón de uno cada `cada_s`: el timeout por operación de socket (0,3 s)
    no salta nunca, pero la respuesta tarda `n × cada_s`."""

    def __init__(self, n, cada_s):
        self.srv = socket.socket()
        self.srv.bind(('127.0.0.1', 0))
        self.srv.listen(1)
        self.url = f'http://127.0.0.1:{self.srv.getsockname()[1]}/'
        self.n, self.cada_s = n, cada_s
        threading.Thread(target=self._servir, daemon=True).start()

    def _servir(self):
        try:
            c, _ = self.srv.accept()
            c.recv(65536)
            c.sendall(f'HTTP/1.1 200 OK\r\nContent-Length: {self.n}\r\nConnection: close\r\n\r\n'.encode())
            for _ in range(self.n):
                time.sleep(self.cada_s)
                c.sendall(b'x')
            c.close()
        except OSError:
            pass
        finally:
            self.srv.close()


def test_m1_respuesta_que_gotea_un_byte_cada_pocos_decimas_no_cuelga_a_r17(ent17):
    """Red simulada con sockets reales: httplib2 con timeout de 0,3 s no se entera de un goteo de 0,1 s por byte."""
    ent17.cfg.update({'r17_plazo_s': 0.5, 'r17_espera_sync_s': 3})
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    servidor = _Goteo(25, 0.1)                                              # 2,5 s en total
    resultado = {}

    class HojaGoteo(HojaR17):
        def leer_fila_r17(self, fila, col):
            self.lecturas.append((fila, col))
            t0 = time.monotonic()
            try:
                httplib2.Http(timeout=0.3, proxy_info=None).request(servidor.url)
            except Exception as e:
                resultado['error'] = e
            resultado['duracion'] = time.monotonic() - t0
            resultado['fin'] = True
            return list(self.valores[fila][:col + 1])

    hoja, logs = HojaGoteo(), []
    sv = _servicio_con_cerrojo_en_el_log(ent17, hoja, logs)
    t0 = time.monotonic()
    sv.paso(epoch(madrid(2026, 10, 8, 16, 29, 30)))
    assert time.monotonic() - t0 < 1.5 and logs and logs[0][0].startswith('[R17 ERROR] TimeoutError')
    limite = time.monotonic() + 6
    while 'fin' not in resultado and time.monotonic() < limite:
        time.sleep(0.05)
    assert 'error' not in resultado and resultado['duracion'] >= 2.0       # el timeout de socket nunca saltó: sin plazo de pared no volvía
    assert hoja.escrituras == []                                            # y lo que contestó tarde no escribió nada


# --- B1: el hilo no muere ------------------------------------------------------------------------------------------------

def test_b1_el_hilo_sobrevive_a_un_log_que_lanza_y_deja_rastro_en_stderr(ent17, capsys):
    ent17.cfg.update({'r17_intervalo_s': 0.01, 'preoperativa_pausa_error_s': 0.05})
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    hoja = HojaR17()
    hoja.falla = ConnectionError('sin red')

    def log_roto(m):
        raise OSError('disco lleno')

    sv = r17.ServicioR17(ent17, hoja, log_roto)
    sv.fijar_celda('I6')
    parar = threading.Event()
    hilo = threading.Thread(target=sv.bucle, args=(parar,), daemon=True)
    hilo.start()
    try:
        limite = time.monotonic() + 3
        while not sv.log_fallido and time.monotonic() < limite:
            time.sleep(0.01)
        assert sv.log_fallido >= 1 and hilo.is_alive()                      # el log lanzó y el hilo sigue
        assert '[R17 ERROR] ConnectionError' in capsys.readouterr().err     # y el mensaje no se perdió
        hoja.falla = None                                                   # vuelve la red: R17 sigue sirviendo
        limite = time.monotonic() + 3
        while not hoja.escrituras and time.monotonic() < limite:
            time.sleep(0.01)
        assert hoja.escrituras and hilo.is_alive()
    finally:
        parar.set()
        hilo.join(3)
    assert not hilo.is_alive()


def test_b1_el_bucle_sobrevive_a_una_excepcion_imprevista(ent17):
    ent17.cfg['r17_intervalo_s'] = 0.01
    logs, llamadas = [], []
    sv = _servicio(ent17, HojaR17(), logs)

    def paso():
        llamadas.append(1)
        if len(llamadas) <= 3:
            raise RuntimeError('imprevisto')

    sv.paso = paso
    parar = threading.Event()
    hilo = threading.Thread(target=sv.bucle, args=(parar,), daemon=True)
    hilo.start()
    try:
        limite = time.monotonic() + 3
        while len(llamadas) < 8 and time.monotonic() < limite:
            time.sleep(0.01)
        assert len(llamadas) >= 8 and hilo.is_alive()
    finally:
        parar.set()
        hilo.join(3)
    assert logs == ['[R17 ERROR] RuntimeError: imprevisto']                 # un solo log por minuto


@pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')     # la SystemExit del hilo es el caso
def test_b1_si_el_hilo_acaba_sin_que_se_le_pida_queda_rastro(ent17):
    ent17.cfg['r17_intervalo_s'] = 0.01
    logs = []
    sv = _servicio(ent17, HojaR17(), logs)

    def paso():
        raise SystemExit(1)                                                 # nada que `except Exception` pueda recoger

    sv.paso = paso
    hilo = threading.Thread(target=sv.bucle, args=(threading.Event(),), daemon=True)
    hilo.start()
    hilo.join(3)
    assert not hilo.is_alive() and logs == ['[R17 ERROR] El hilo de R17 ha terminado sin que se le pidiera.']


# --- B2: una configuración mala no impide arrancar el demonio ----------------------------------------------------------------

def _estropear_cfg(clave, valor):
    return lambda ent: ent.cfg.__setitem__(clave, valor)


MALAS = [
    ('intervalo no numérico', _estropear_cfg('r17_intervalo_s', 'cinco')),
    ('plazo negativo', _estropear_cfg('r17_plazo_s', -1)),
    ('espera del sync cero', _estropear_cfg('r17_espera_sync_s', 0)),
    ('plantilla con un campo que no existe', lambda ent: ent.reglas['preoperativa']['r17'].update(texto='R17 {nada}')),
    ('sin prefijo', lambda ent: ent.reglas['preoperativa']['r17'].pop('prefijo')),
    ('marco cero', lambda ent: ent.reglas['preoperativa']['r17'].update(marco_s=0)),
    ('sin sección r17', lambda ent: ent.reglas['preoperativa'].pop('r17')),
]


@pytest.mark.parametrize('nombre,estropear', MALAS, ids=[m[0] for m in MALAS])
def test_b2_crear_r17_con_configuracion_mala_registra_y_devuelve_none(ent17, nombre, estropear):
    estropear(ent17)
    logs = []
    assert demonio_v4.crear_r17(ent17, HojaR17(), logs.append) is None
    assert len(logs) == 1 and logs[0].startswith('[R17 ERROR] Configuración de R17 no válida; el demonio arranca sin R17')


def test_b2_crear_r17_apagado_o_bueno(ent17):
    logs = []
    assert isinstance(demonio_v4.crear_r17(ent17, HojaR17(), logs.append), r17.ServicioR17) and logs == []
    ent17.reglas['preoperativa']['r17']['activo'] = False
    assert demonio_v4.crear_r17(ent17, HojaR17(), logs.append) is None and logs == []


class _StdinQ:
    """Stdin del demonio: una `q` al cabo de un rato (da tiempo a que arranquen el sync y el hilo) y luego EOF."""

    def __init__(self, espera=0.6):
        self.espera, self.n = espera, 0

    def readline(self):
        self.n += 1
        if self.n == 1:
            time.sleep(self.espera)
            return 'q\n'
        return ''


def _correr_main(monkeypatch, entorno_hoy, tmp_path, tras_preparar=None):
    """demonio_v4.main() entero, con el sheet falso, sin mutex y con `q` por stdin. Devuelve (logs, hoja)."""
    ent, con, s = entorno_hoy
    ent.cfg['intervalo_s'] = 0.05
    ent.cfg['r17_intervalo_s'] = 0.05
    velas = tmp_path / 'velas_main'
    velas.mkdir()
    ent.reglas['preoperativa']['r17']['carpeta'] = str(velas)       # nunca la carpeta real de velas
    hoja, logs, conexiones = HojaFalsa(), [], []
    original = almacen.conectar
    monkeypatch.setattr(almacen, 'conectar', lambda *a, **k: conexiones.append(original(*a, **k)) or conexiones[-1])
    monkeypatch.setattr(config, 'cargar_entorno', lambda *a, **k: ent)
    monkeypatch.setattr(sheets, 'HojaV4', lambda e: hoja)
    monkeypatch.setattr(demonio_v4, 'instancia_unica', lambda nombre: 1)
    monkeypatch.setattr(demonio_v4, 'log', logs.append)
    monkeypatch.setattr(sys, 'stdin', _StdinQ())
    monkeypatch.setattr(sys, 'argv', ['demonio_v4.py'])
    if tras_preparar:
        tras_preparar(ent)
    try:
        demonio_v4.main()
    finally:
        for c in conexiones:
            c.close()
    return logs, hoja


def _hilos_r17():
    return [t for t in threading.enumerate() if t.name == 'R17' and t.is_alive()]


def test_b8_main_con_r17_apagado_no_arranca_hilo_ni_toca_la_celda(entorno_hoy, monkeypatch, tmp_path):
    lanzados = []
    monkeypatch.setattr(demonio_v4, 'iniciar_r17', lambda *a: lanzados.append(a))
    logs, hoja = _correr_main(monkeypatch, entorno_hoy, tmp_path,
                              lambda ent: ent.reglas['preoperativa']['r17'].update(activo=False))
    assert lanzados == [] and not _hilos_r17() and not [m for m in logs if 'R17' in m]
    assert [m for m in logs if m.startswith('[SYNC]')] and '[SALIDA] Demonio V5 cerrado.' in logs
    assert not hasattr(hoja, 'escrituras_r17')
    fila = next(r for r in hoja.valores if r and r[0] == 'Estrategia a usar')
    assert fila[vista.OC['Estrategia'] + 1] == ''


@pytest.mark.parametrize('nombre,estropear', [MALAS[0], MALAS[6]], ids=['intervalo no numérico', 'sin sección r17'])
def test_b8_main_con_configuracion_de_r17_mala_arranca_sin_r17(entorno_hoy, monkeypatch, tmp_path, nombre, estropear):
    logs, hoja = _correr_main(monkeypatch, entorno_hoy, tmp_path, estropear)
    assert [m for m in logs if m.startswith('[R17 ERROR] Configuración de R17 no válida')]
    assert not [m for m in logs if m.startswith('[R17] Hilo en marcha')] and not _hilos_r17()
    assert [m for m in logs if m.startswith('[SYNC]')] and '[SALIDA] Demonio V5 cerrado.' in logs   # el demonio funcionó
    assert not hasattr(hoja, 'escrituras_r17')


def test_b8_main_si_el_hilo_no_arranca_el_demonio_sigue(entorno_hoy, monkeypatch, tmp_path):
    def roto(*a):
        raise RuntimeError('no hay hilos')

    monkeypatch.setattr(demonio_v4, 'iniciar_r17', roto)
    logs, _ = _correr_main(monkeypatch, entorno_hoy, tmp_path)
    assert [m for m in logs if m.startswith('[R17 ERROR] No se pudo arrancar el hilo de R17')]
    assert [m for m in logs if m.startswith('[SYNC]')] and '[SALIDA] Demonio V5 cerrado.' in logs


def test_b8_main_con_r17_activo_arranca_el_hilo_y_lo_para_al_salir(entorno_hoy, monkeypatch, tmp_path):
    logs, hoja = _correr_main(monkeypatch, entorno_hoy, tmp_path)
    assert [m for m in logs if m.startswith('[R17] Hilo en marcha')] and '[SALIDA] Demonio V5 cerrado.' in logs
    limite = time.monotonic() + 3
    while _hilos_r17() and time.monotonic() < limite:
        time.sleep(0.05)
    assert not _hilos_r17()                                                 # `parar.set()` del demonio lo despierta y acaba


# --- B3: la celda destino ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('contenido', ['Sin estrategia', 'otra cosa', 42, 'r17 en minúsculas', ' R17 con espacio delante'])
def test_b3_no_escribe_sobre_una_celda_con_otro_contenido(ent17, contenido):
    logs = []
    sv, hoja, ahora = _preparar(ent17, logs=logs)
    hoja.valores[5][9] = contenido
    snapshot = [list(r) for r in hoja.valores]
    sv.paso(ahora)
    assert hoja.escrituras == [] and hoja.valores == snapshot
    assert len(logs) == 1 and 'otro contenido' in logs[0] and 'I6' in logs[0]
    leidas = len(hoja.lecturas)
    sv.paso(ahora + 5)                                                      # en pausa, sin volver a leer ni a avisar
    assert len(hoja.lecturas) == leidas and len(logs) == 1


@pytest.mark.parametrize('contenido', ['', '   ', 'R17 sin datos', 'R17 viejo · 10:00:00'])
def test_b3_escribe_si_la_celda_esta_vacia_o_es_de_r17(ent17, contenido):
    sv, hoja, ahora = _preparar(ent17)
    hoja.valores[5][9] = contenido
    sv.paso(ahora)
    assert hoja.escrituras == [(5, 9, 'R17 461 t · stop 172 t · 16:29:24')]


def test_b3_columna_insertada_a_mano_no_recibe_el_texto(ent17):
    """El operador inserta una columna delante de la I: la estrategia pasa a la J, que es donde R17 iba a escribir."""
    logs = []
    sv, hoja, ahora = _preparar(ent17, logs=logs)
    for fila in hoja.valores:
        fila.insert(8, '')
    assert hoja.valores[5][9] == 'Sin estrategia'
    snapshot = [list(r) for r in hoja.valores]
    sv.paso(ahora)
    assert hoja.escrituras == [] and hoja.valores == snapshot and len(logs) == 1 and 'columna insertada' in logs[0]
    sv.fijar_celda('J6')                                                    # cuando el sync recoloca la fila, R17 sigue la estrategia
    sv.paso(ahora + 1)
    assert hoja.escrituras == [(5, 10, 'R17 461 t · stop 172 t · 16:29:24')]


# --- B4: el sync no borra el texto de R17 -------------------------------------------------------------------------------------

TEXTO_R17 = 'R17 461 t · stop 172 t · 16:29:24'


def test_b4_leer_r17_del_sheet(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA_SYNC)
    v = [list(r) for r in lay['valores']]
    fila, col = lay['preop_fila'], lay['preop_col'] + 1
    assert vista.leer_r17(v, ent.reglas) is None                            # vacía
    v[fila][col] = TEXTO_R17
    assert vista.leer_r17(v, ent.reglas) == TEXTO_R17
    for otro in ('basura', 42, 'r17 en minúsculas', ''):
        v[fila][col] = otro
        assert vista.leer_r17(v, ent.reglas) is None
    v[fila][col] = TEXTO_R17
    ent.reglas['preoperativa']['r17']['activo'] = False
    assert vista.leer_r17(v, ent.reglas) is None                            # apagado: no se conserva
    ent.reglas['preoperativa']['r17']['activo'] = True
    del v[fila]                                                             # sin la fila de la estrategia
    assert vista.leer_r17(v, ent.reglas) is None
    del ent.reglas['preoperativa']['r17']                                   # sin sección (configuración mala): tampoco lanza
    assert vista.leer_r17(lay['valores'], ent.reglas) is None


def test_b4_el_sync_sin_hilo_conserva_el_texto_de_r17(entorno_hoy):
    """`cli_v4.py sync` con el demonio parado: Sincronizador sin servicio de R17."""
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA_SYNC)
    fila, col = lay['preop_fila'], lay['preop_col'] + 1
    assert s.r17 is None
    s.hoja.valores[fila][col] = TEXTO_R17
    _, lay2 = s.sync(AHORA_SYNC)
    assert lay2['valores'][fila][col] == TEXTO_R17 and s.hoja.valores[fila][col] == TEXTO_R17
    s.hoja.valores[fila][col] = 'basura'                                    # lo que no es de R17 no se conserva, como siempre
    _, lay3 = s.sync(AHORA_SYNC)
    assert lay3['valores'][fila][col] == '' and s.hoja.valores[fila][col] == ''
    s.hoja.valores[fila][col] = TEXTO_R17
    ent.reglas['preoperativa']['r17']['activo'] = False                     # R17 apagado: la celda se limpia
    _, lay4 = s.sync(AHORA_SYNC)
    assert lay4['valores'][fila][col] == ''


def test_b4_el_primer_sync_con_el_servicio_siembra_el_texto_y_r17_no_lo_reescribe(entorno_hoy, ent17):
    ent, con, s = entorno_hoy
    ent.reglas['preoperativa']['r17']['carpeta'] = ent17.carpeta_velas
    escribir(ent17.carpeta_velas, '2026-10-08', _lineas_8_10(HIST_8_10, 'historico'))
    _, lay = s.sync(AHORA_SYNC)
    fila, col = lay['preop_fila'], lay['preop_col'] + 1
    s.hoja.valores[fila][col] = TEXTO_R17                                   # lo que dejó la ejecución anterior del demonio
    sv = r17.ServicioR17(ent, s.hoja, lambda m: None)                       # demonio recién arrancado: R17 aún no ha escrito
    s.r17 = sv
    _, lay2 = s.sync(AHORA_SYNC)
    assert sv.texto_escrito() == TEXTO_R17 and s.hoja.valores[fila][col] == TEXTO_R17
    sv.paso(epoch(madrid(2026, 10, 8, 16, 29, 30)))                         # el texto de ahora es el mismo: nada que escribir
    assert not hasattr(s.hoja, 'escrituras_r17')
    # con el texto ya viejo, en cambio, R17 lo renueva
    sv.paso(epoch(madrid(2026, 10, 8, 17, 30, 0)))
    assert s.hoja.escrituras_r17 == [(fila, col, 'R17 sin datos desde 16:29:24')]


def test_b4_sembrar_no_pisa_lo_que_r17_ya_ha_escrito(ent17):
    sv = _servicio(ent17)
    sv.sembrar(None)
    assert sv.texto_escrito() == ''
    sv.sembrar('R17 viejo')
    sv.sembrar('R17 otro')
    assert sv.texto_escrito() == 'R17 viejo'


def test_b4_un_fallo_de_r17_dentro_del_sync_no_lo_tumba(entorno_hoy):
    ent, con, s = entorno_hoy
    logs = []
    s.log = logs.append
    sv = r17.ServicioR17.__new__(r17.ServicioR17)
    sv.cerrojo = threading.Lock()
    sv.espera_sync_s = 1.0
    sv.sembrar = lambda texto: (_ for _ in ()).throw(RuntimeError('roto'))
    sv.fijar_celda = lambda a1: None
    s.r17 = sv
    _, lay = s.sync(AHORA_SYNC)                                             # no lanza
    assert s.hoja.escrito is lay and [m for m in logs if m.startswith('[R17 ERROR] RuntimeError: roto')]
