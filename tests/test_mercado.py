"""Estado del mercado (spec §12, decisión del operador del 08/10/2026): fila de preoperativa `Estado del mercado` encima de
`Estrategia a usar`, desplegable `Balance` / `Imbalance` / `-`, reseteo por sesión CME, lectura en la misma petición que la
estrategia y la compuerta, y `operaciones.datos.mercado` con la última lectura anterior a la apertura de cada decisión.
No es de la compuerta ni de los objetivos. Fixture real `entorno_hoy` (22/09/2026)."""

import ast
import json
import os

import pytest

from atasv4 import almacen, config, motor, sheets, vista
from conftest import RAIZ

@pytest.fixture(autouse=True)
def _compuerta_visible(monkeypatch):
    """Tests de las filas SÍ/NO de la compuerta: la V5 ya no las muestra (`preoperativa.compuerta.mostrar: false`, spec §16.3),
    pero el código sigue ahí para las decisiones de la V4; aquí se prueban con el interruptor encendido."""
    from atasv4 import config as _config
    real = _config.cargar_entorno

    def cargar(*a, **k):
        ent = real(*a, **k)
        ent.reglas['preoperativa']['compuerta']['mostrar'] = True
        return ent

    monkeypatch.setattr(_config, 'cargar_entorno', cargar)


OC = vista.OC
AHORA = '2026-09-22 18:00:00.000'          # sesión del 22/09 ya terminada (UTC)
SIGUIENTE = '2026-09-23 18:00:00.000'      # la sesión siguiente
COL = OC['Estrategia']


@pytest.fixture(scope='module')
def R():
    return config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'))


def _mercado_hoja(s, lay, valor):
    s.hoja.valores[lay['mercado_fila']][lay['preop_col']] = valor


def _datos(con):
    return {r['op_id']: json.loads(r['datos']) for r in con.execute('SELECT op_id, datos FROM operaciones')}


# ===========================================================================
# Layout y desplegable
# ===========================================================================

def test_la_fila_va_encima_de_la_estrategia_con_el_defecto(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    v = lay['valores']
    assert lay['mercado_fila'] == lay['preop_fila'] - 1
    fila = v[lay['mercado_fila']]
    assert fila[0] == 'Estado del mercado' and fila[COL] == '-' and not any(fila[1:COL])
    assert v[lay['preop_fila']][0] == 'Estrategia a usar' and v[lay['compuerta_fila']][0] == 'Sueño ≥ 6 h (reloj)'
    assert all(not any(c for c in r) for r in v[lay['mercado_fila'] - 2:lay['mercado_fila']])      # las 2 vacías, justo encima


def test_el_desplegable_es_estricto_y_con_los_tres_valores(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    h = sheets.HojaV4.__new__(sheets.HojaV4)
    h.R, h.tab_id, h.filas = ent.reglas, 1, 500
    val = [r['setDataValidation'] for r in h._formatos(lay) if 'setDataValidation' in r]
    m = next(x for x in val if x['range']['startRowIndex'] == lay['mercado_fila'])
    assert [x['userEnteredValue'] for x in m['rule']['condition']['values']] == ['Balance', 'Imbalance', '-']
    assert m['rule']['strict'] is True
    assert (m['range']['endRowIndex'], m['range']['startColumnIndex']) == (lay['mercado_fila'] + 1, COL)


def test_la_pestana_se_lee_igual_con_la_fila_nueva(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    v = lay['valores']
    assert vista.leer_preoperativa(v, ent.reglas) == 'Sin estrategia'
    assert vista.leer_compuerta(v, ent.reglas) == ['', '', '', '']
    assert vista.leer_mercado(v, ent.reglas) == '-'
    assert vista._preoperativa(v, ent.reglas)[1] == lay['preop_fila']
    manual, _, ok = vista.parsear_hoja(v)
    assert ok and 'Estado del mercado' not in manual


def test_leer_mercado_en_la_pestana_antigua_sin_la_fila(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    antigua = [r for i, r in enumerate(lay['valores']) if i != lay['mercado_fila']]
    assert vista.leer_mercado(antigua, ent.reglas) is None
    assert vista.leer_preoperativa(antigua, ent.reglas) == 'Sin estrategia'     # y la estrategia se encuentra igual


@pytest.mark.parametrize('celda, esperado', [('Balance', 'Balance'), ('imbalance', 'Imbalance'), (' BALANCE ', 'Balance'),
                                             ('-', '-'), ('', '-'), (None, '-'), ('otra cosa', '-')])
def test_valor_leido_normalizado(R, celda, esperado):
    fila = ['Estado del mercado'] + [''] * (COL - 1) + [celda]
    assert vista.valor_mercado(fila, R) == esperado


@pytest.mark.parametrize('fila', [[], ['Estrategia a usar'], ['estado del mercado'], ['x', '', 'Balance']])
def test_fila_que_no_es_la_del_mercado_no_se_lee(R, fila):
    assert vista.valor_mercado(fila, R) is None


# ===========================================================================
# Registro por decisión
# ===========================================================================

@pytest.mark.parametrize('apertura, esperado', [
    ('2026-09-22 14:00:00.000', '-'),                   # sin lecturas anteriores
    ('2026-09-22 15:30:00.000', 'Imbalance'),           # justo en el instante de la lectura: cuenta
    ('2026-09-22 15:30:00.001', 'Imbalance'),
    ('2026-09-22 15:29:59.999', '-'),
    ('2026-09-22 16:30:00.000', 'Balance'),             # la última anterior, no la primera
    ('2026-09-22 20:59:59.000', 'Balance'),             # aún la sesión del 22/09 (hasta las 22:00 UTC)
    ('2026-09-22 22:00:00.000', '-'),                   # sesión nueva: la fila se reseteó aunque no haya lectura propia
])
def test_mercado_en_la_ultima_lectura_anterior_a_la_apertura(R, apertura, esperado):
    lecturas = [('2026-09-22 15:30:00.000', 'Imbalance'), ('2026-09-22 16:30:00.000', 'Balance')]
    assert motor.mercado_en(apertura, lecturas, R) == esperado


def test_cada_decision_guarda_el_mercado_de_su_apertura_y_la_seguidora_el_de_su_lider(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    ds = sorted((f for f in res.filas if not f.es_seguidora), key=lambda f: f.t_apertura_utc)
    corte = ds[len(ds) // 2].t_apertura_utc                           # una lectura de Imbalance a mitad de la sesión
    con.execute('DELETE FROM mercado_lecturas')
    con.execute('INSERT INTO mercado_lecturas VALUES (?, ?)', (corte, 'Imbalance'))
    con.commit()
    res, lay = s.sync(AHORA)
    datos = _datos(con)
    reloj = res.reloj
    pc = reloj.pc_a_utc(almacen.lecturas_mercado(con)[0][0])
    for f in res.filas:
        propia = f.lider or f
        assert datos[f.op_id]['mercado'] == ('Imbalance' if propia.t_apertura_utc >= pc else '-')
    assert {d['mercado'] for d in datos.values()} == {'-', 'Imbalance'}
    seg = [f for f in res.filas if f.es_seguidora]
    assert seg and all(datos[f.op_id]['mercado'] == datos[f.lider.op_id]['mercado'] for f in seg)


def test_sync_lee_el_valor_del_sheet_y_registra_solo_los_cambios(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    assert almacen.lecturas_mercado(con) == [(AHORA, '-')]
    _mercado_hoja(s, lay, 'Balance')
    _, lay = s.sync('2026-09-22 18:10:00.000')
    assert almacen.lecturas_mercado(con) == [(AHORA, '-'), ('2026-09-22 18:10:00.000', 'Balance')]
    s.sync('2026-09-22 18:20:00.000')                                      # sin cambios: nada nuevo
    assert len(almacen.lecturas_mercado(con)) == 2
    assert s.hoja.valores[lay['mercado_fila']][COL] == 'Balance'            # y se vuelve a escribir tal cual


def test_el_reseteo_por_sesion_cme_vuelve_a_guion(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    _mercado_hoja(s, lay, 'Imbalance')
    s.sync('2026-09-22 18:10:00.000')
    assert s.valor_mercado() == 'Imbalance'
    _, lay = s.sync(SIGUIENTE)                                              # sesión del 23/09: ni se lee lo viejo del sheet
    assert s.hoja.valores[lay['mercado_fila']][COL] == '-' and s.valor_mercado() == '-'
    assert almacen.lecturas_mercado(con)[-1] == (SIGUIENTE, '-')


def test_no_es_de_la_compuerta_ni_de_los_objetivos(entorno_hoy):
    ent, con, s = entorno_hoy
    ent.reglas['plan4']['desde'] = __import__('datetime').date(2026, 9, 22)
    _, lay = s.sync(AHORA)
    antes = [r['obs'] for r in (json.loads(x['datos']) for x in con.execute('SELECT datos FROM operaciones'))]
    comp_antes = almacen.lecturas_compuerta(con)
    _mercado_hoja(s, lay, 'Imbalance')
    _, lay = s.sync('2026-09-22 18:10:00.000')
    despues = [r['obs'] for r in (json.loads(x['datos']) for x in con.execute('SELECT datos FROM operaciones'))]
    assert despues == antes and 'Imbalance' not in ' '.join(despues)         # ninguna etiqueta en Observaciones demonio
    assert almacen.lecturas_compuerta(con) == comp_antes                     # la compuerta ni se entera
    assert 'mercado' not in ' '.join(ent.reglas['objetivos'].keys())


# ===========================================================================
# Lectura periódica
# ===========================================================================

def test_la_lectura_periodica_lee_mercado_estrategia_y_compuerta_en_una_peticion(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    llamadas, leer = [], s.hoja.leer_filas
    s.hoja.leer_filas = lambda *a: (llamadas.append(a), leer(*a))[1]
    _mercado_hoja(s, lay, 'Imbalance')
    s.hoja.valores[lay['preop_fila']][lay['preop_col']] = 'Cazas de soporte/resistencia en 204s'
    s.hoja.valores[lay['compuerta_fila']][lay['preop_col']] = 'SÍ'
    assert s.vigilar_preoperativa('2026-09-22 18:00:05.000') == 'Cazas de soporte/resistencia en 204s'
    assert llamadas == [(lay['preop_fila'] - 1, lay['preop_col'], 6)]
    assert s.valor_mercado() == 'Imbalance' and almacen.lecturas_mercado(con)[-1] == ('2026-09-22 18:00:05.000', 'Imbalance')
    assert s.valores_compuerta() == ['SÍ', '', '', '']


def test_lectura_periodica_con_la_pestana_antigua_sigue_leyendo_la_estrategia(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    antigua = [r for i, r in enumerate(lay['valores']) if i != lay['mercado_fila']]
    s.hoja.valores = [list(r) for r in antigua]
    almacen.set_estado(con, 'preop_celda', vista.celda_a1(lay['preop_fila'] - 1, lay['preop_col']))     # donde estaba antes
    s.hoja.valores[lay['preop_fila'] - 1][lay['preop_col']] = 'Cazas de soporte/resistencia en 204s'
    n = len(almacen.lecturas_mercado(con))
    assert s.vigilar_preoperativa('2026-09-22 18:00:05.000') == 'Cazas de soporte/resistencia en 204s'
    assert len(almacen.lecturas_mercado(con)) == n and s.valor_mercado() == '-'          # sin la fila, no se anota nada


def test_lectura_periodica_con_la_etiqueta_del_mercado_movida_no_anota_el_mercado(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    _mercado_hoja(s, lay, 'Balance')
    s.hoja.valores[lay['mercado_fila']][0] = 'Otra cosa'
    n = len(almacen.lecturas_mercado(con))
    s.vigilar_preoperativa('2026-09-22 18:00:05.000')
    assert len(almacen.lecturas_mercado(con)) == n and s.valor_mercado() == '-'
    s.hoja.valores[lay['preop_fila']][0] = 'Otra cosa'                                    # y con la de la estrategia movida, nada
    assert s.vigilar_preoperativa('2026-09-22 18:00:10.000') is None


def test_lectura_periodica_antes_de_escribir_la_sesion_nueva_no_anota_el_mercado(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    _mercado_hoja(s, lay, 'Imbalance')
    n = len(almacen.lecturas_mercado(con))
    s.vigilar_preoperativa(SIGUIENTE)                           # la sesión del 23/09 aún no se escribió: el sheet es de la anterior
    assert len(almacen.lecturas_mercado(con)) == n and s.valor_mercado() == '-'


def test_primer_arranque_con_la_pestana_antigua(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    s.hoja.valores = [list(r) for i, r in enumerate(lay['valores']) if i != lay['mercado_fila']]       # pestaña de antes
    almacen.set_estado(con, 'preop_celda', vista.celda_a1(lay['preop_fila'] - 1, lay['preop_col']))
    con.execute('DELETE FROM mercado_lecturas')
    con.commit()
    _, lay2 = s.sync('2026-09-22 18:10:00.000')                  # sin la fila: se escribe con el defecto y no se lee nada
    assert lay2['valores'][lay2['mercado_fila']][COL] == '-' and s.hoja.valores[lay2['mercado_fila']][0] == 'Estado del mercado'
    assert almacen.get_estado(con, 'preop_celda') == vista.celda_a1(lay2['preop_fila'], lay2['preop_col'])
    assert s.vigilar_preoperativa('2026-09-22 18:10:05.000') == 'Sin estrategia'


# ===========================================================================
# Reglas, no código
# ===========================================================================

def test_ninguna_literal_del_mercado_en_el_codigo(R):
    M = R['preoperativa']['mercado']
    textos = {M['etiqueta'], 'Balance', 'Imbalance'}
    assert M['defecto'] == '-' and M['valores'] == ['Balance', 'Imbalance', '-']
    constantes = set()
    for modulo in ('motor', 'vista', 'sheets', 'sync', 'almacen'):
        arbol = ast.parse(open(os.path.join(RAIZ, 'atasv4', modulo + '.py'), encoding='utf-8').read())
        constantes |= {n.value for n in ast.walk(arbol) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert textos & constantes == set()
