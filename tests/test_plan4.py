"""Plan 4 (grill del operador, 07/10/2026; spec §10): E2T real, margen y bono de sesión, contratos de las pruebas de 25K por la
distancia al threshold, pérdida de una decisión y bloqueo por pérdidas seguidas, etiqueta ESPEJO y compuerta de la
preoperativa.

Todo sintético y mínimo, como test_apex_pruebas y test_tope_sl: fills de MyTrade (y, donde hace falta telemetría, del log, con
órdenes SL/TP y flotante) escritos en la SQLite. Comisión de Apex: NQ 1,99 y MNQ 0,51 por lado (donde el importe tiene que
ser exacto se cambia, como en test_apex_pruebas). Las horas de los fills son UTC; en octubre, Madrid va 2 h por delante.
"""

import ast
import json
import os
from datetime import date

import pytest

from atasv4 import almacen, config, cortafuegos, cuentas, motor, sheets, vista
from conftest import RAIZ, filas_por
from test_apex_pruebas import (AP76, AP77, BX, D, D1, D2, E250, FICHAS, MNQ, NQ, PA15, PA16, Avisos, _base, _entorno, _ficha,
                               _mae_manual, _op, _t)
from test_tope_sl import _tel

D3, D4 = date(2026, 10, 8), date(2026, 10, 9)                  # jueves y viernes: desde D3 rige el bono de sesión nuevo
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


LIVE = 'livesimT_franeznaco50e'                                # la última E2T real (spec §10.1)
FICHAS[LIVE] = _ficha('E2T', 'real', 25000, '2026-10-05')
SEP = ' · '
AHORA = _t(D2, '20:00:00')                                     # 22:00 de Madrid, sesión CME del 07/10 (anterior a plan4.desde)
AHORA3 = _t(D3, '20:00:00')                                    # y la del 08/10, la primera con las reglas del Plan 4
X = 'Continuación de M204s confirmada por M17'


def _bal_en(res, cuenta):
    return {b['cuenta']: b for b in res.bal}[cuenta]


def _formatos(ent, lay):
    hoja = sheets.HojaV4.__new__(sheets.HojaV4)
    hoja.tab_id, hoja.filas, hoja.R = 0, len(lay['valores']) + 50, ent.reglas
    return hoja._formatos(lay)


# ===========================================================================
# 10.1 Cuentas reales: Apex PA y E2T real
# ===========================================================================

def test_patron_livesim_es_e2t_real_y_las_demas_e2t_siguen_siendo_pruebas(tmp_path):
    ent, con, s = _entorno(tmp_path, [LIVE, E250, PA15])
    assert cuentas.inferir(LIVE, ent.reglas) == {'firma': 'E2T', 'tipo': 'real', 'tamano': None}
    assert cuentas.inferir('livesimX', ent.reglas)['tipo'] == 'real' and cuentas.inferir('xlivesim', ent.reglas) is None
    assert [ent.es_real(c) for c in (LIVE, E250, PA15)] == [True, False, True]
    assert [ent.es_prueba(c) for c in (LIVE, E250, PA15)] == [False, True, False]
    assert not ent.es_sim(LIVE)                                 # 'livesim' no es '_sim'


def test_alta_automatica_de_la_e2t_real_con_el_patron(tmp_path):
    ent, con, s = _entorno(tmp_path, [PA15])
    _op(con, LIVE, MNQ, 1, 20000, 20001, _t(D, '08:00:00'), _t(D, '08:01:00'), 1)
    assert cuentas.registrar_nuevas(ent, con, D, []) == [LIVE]
    ficha = ent.ficha(LIVE)
    assert (ficha['firma'], ficha['tipo']) == ('E2T', 'real')      # el tamaño se completa con el primer Bal: plano, como en las demás
    assert ent.es_real(LIVE) and not ent.es_prueba(LIVE)


def test_e2t_real_tiene_la_fila_bal_de_las_reales(tmp_path):
    """§8 para ella: contratos por símbolo, días +50 y +100 (las columnas de las reales). La inactividad, desde el 11/11/2026,
    es la de E2T (§10.1): 5 días hábiles seguidos sin operar; sin operaciones cuenta desde el alta (lunes 05/10 = día 1)."""
    ent, con, s = _entorno(tmp_path, [LIVE, E250])
    for c in (LIVE, E250):
        _base(con, c, 25000.0)
    res = s.calcular(_t(D, '12:00:00'))
    live, prueba = _bal_en(res, LIVE), _bal_en(res, E250)
    assert live['permitidos'] == '0 NQ / 2 MNQ'                 # margen 500 -> 2 MNQ; NQ 0 siempre (§10.1)
    assert live['inactividad'] == 'día 1 de 5 sin operar · operar como tarde el 09/10 · OK'
    assert live['dias_100'] == 0 and live['dias_50_30d'] == 0
    assert live['prueba'] is None
    assert prueba['inactividad'] is None and prueba['prueba'] is not None      # la prueba sigue con las suyas


@pytest.mark.parametrize('cuenta', [LIVE, PA15])
def test_reales_nq_cero_y_mnq_mas_de_141(tmp_path, cuenta):
    R = _entorno(tmp_path, [cuenta])[0].reglas
    lim = lambda s, m: motor.limite_real(s, m, False, R)
    assert [lim(NQ, m) for m in (3000, 750, 500, 141.01, 141, -50)] == [0] * 6
    assert [lim(MNQ, m) for m in (750, 749.99, 500, 499.99, 141.01, 141, 140.99)] == [3, 2, 2, 1, 1, 0, 0]
    old = lambda s, m: motor.limite_real(s, m, False, R, nuevo=False)         # lo anterior a plan4.desde
    assert [old(NQ, m) for m in (3000, 141, 140.99)] == [1, 1, 0] and [old(MNQ, m) for m in (141, 140.99)] == [1, 0]


def test_e2t_real_infraccion_de_contratos_por_simbolo(tmp_path):
    ent, con, s = _entorno(tmp_path, [LIVE], inicio='2026-10-08')            # margen 500 el 08/10
    _base(con, LIVE, 25000.0)
    _op(con, LIVE, NQ, 1, 20000, 20001, _t(D3, '08:00:00'), _t(D3, '08:01:00'), 1)
    _op(con, LIVE, MNQ, 3, 20000, 20001, _t(D3, '09:00:00'), _t(D3, '09:01:00'), 2)
    _op(con, LIVE, MNQ, 2, 20000, 20001, _t(D3, '10:00:00'), _t(D3, '10:01:00'), 3)
    a, b, c = filas_por(s.calcular(_t(D3, '12:00:00')), LIVE)
    assert a.real and a.permitidos == 0 and cortafuegos.detalle_exceso(a, ent.reglas) == '1 contratos con 0 permitidos'
    assert cortafuegos.detalle_exceso(b, ent.reglas) == '3 contratos con 2 permitidos'      # margen 500: 2 MNQ
    assert cortafuegos.detalle_exceso(c, ent.reglas) is None


@pytest.mark.parametrize('margen, levantado', [(141, True), (141.01, False), (140.99, True)])
def test_e2t_real_margen_levantado_hasta_141(tmp_path, margen, levantado):
    """§10.1: el margen levantado llega hasta 141 incluido (antes, por debajo de 141); contador 25 por la semilla."""
    sem = {'cuentas': {LIVE: {'ultimos_dias_50': ['2026-09-10', '2026-09-14']}}}
    ent, con, s = _entorno(tmp_path, [LIVE], semillas=sem, margen_inicial=margen, inicio='2026-10-08')
    _base(con, LIVE, 25000.0)
    _op(con, LIVE, MNQ, 1, 20000, 20001, _t(D3, '08:00:00'), _t(D3, '08:01:00'), 1)
    (f,) = filas_por(s.calcular(_t(D3, '12:00:00')), LIVE)
    assert f.levantado is levantado
    assert f.permitidos == (1 if (levantado or margen > 141) else 0) and 'CONTRATOS > PERMITIDOS' not in f.obs


def test_e2t_real_suena_como_una_real(tmp_path):
    ent, con, s = _entorno(tmp_path, [LIVE, E250])
    _op(con, LIVE, MNQ, 1, 20000, 19990, _t(D, '08:00:00'), _t(D, '08:01:00'), 1)          # −40 t: −21,02 $
    _op(con, E250, NQ, 1, 20000, 19990, _t(D, '09:00:00'), _t(D, '09:01:00'), 2)           # la prueba: muda
    a = Avisos()
    s.vigilar(a, _t(D, '08:01:30'))
    assert a == ['NO OPERAR HASTA 10:11:12 (PÉRDIDA: NO OPERAR HASTA 10:11:12)']
    a = Avisos()
    s.vigilar(a, _t(D, '09:01:30'))
    assert a == []


def test_margen_rojo_hasta_141_incluido(tmp_path):
    ent, con, s = _entorno(tmp_path, [PA15])
    _base(con, PA15, 25000.0)
    res, lay = s.sync(_t(D, '12:00:00'))
    reglas = [r['addConditionalFormatRule']['rule'] for r in _formatos(ent, lay) if 'addConditionalFormatRule' in r]
    rojo = next(r for r in reglas if r['ranges'][0]['startColumnIndex'] == vista.BC['Margen de operativa']
                and r['ranges'][0]['startRowIndex'] == 1)
    assert rojo['booleanRule']['condition'] == {'type': 'NUMBER_LESS_THAN_EQ', 'values': [{'userEnteredValue': '141'}]}


# ===========================================================================
# 10.2 Margen de operativa: ≤ 141 -> 0 contratos y bono de sesión nuevo
# ===========================================================================

def test_tabla_general_141_o_menos_cero_contratos(tmp_path):
    """Sim y símbolos sin tabla propia (§5): desde plan4.desde el 141 justo ya no da contrato; antes, sí."""
    ent, con, s = _entorno(tmp_path, [PA15], inicio='2026-10-08')
    R = ent.reglas
    assert [motor.contratos_permitidos(m, R) for m in (750, 500, 141.01, 141, 0, -5)] == [3, 2, 1, 0, 0, 0]
    assert [motor.contratos_permitidos(m, R, nuevo=False) for m in (750, 500, 141.01, 141, 140.99)] == [3, 2, 1, 1, 0]
    assert motor.limite_real('ESZ6@CME', 141, False, R) == 0 and motor.limite_real('ESZ6@CME', 141.01, False, R) == 1
    ent.cuentas['cuentas']['DEMO1'] = dict(FICHAS[AP76])
    ent.cuentas['sim_activas'] = ['DEMO1']
    ent.reglas['margen']['inicial'] = 141
    _op(con, 'DEMO1', NQ, 1, 20000, 20001, _t(D3, '08:00:00'), _t(D3, '08:01:00'), 1)
    (f,) = filas_por(s.calcular(_t(D3, '12:00:00')), 'DEMO1')
    assert f.permitidos == 0 and 'CONTRATOS > PERMITIDOS' in f.obs


def _margen(tmp_path, cuenta, ops, t, comision=None, **kw):
    """Margen de la cuenta en `t` con las operaciones `ops`: [(fecha, 'hh:mm:ss', puntos de NQ)] de 1 NQ."""
    ent, con, s = _entorno(tmp_path, [cuenta], comision=comision, **kw)
    _base(con, cuenta, 25000.0)
    for i, (d, hms, puntos) in enumerate(ops):
        h, m, sec = map(int, hms.split(':'))
        fin = f"{h:02d}:{m + 1:02d}:{sec:02d}"
        _op(con, cuenta, NQ, 1, 20000, 20000 + puntos, _t(d, hms), _t(d, fin), i + 1)
    return _bal_en(s.calcular(t), cuenta)['margen']


SIN_COM = {('Apex', 'NQ'): 0.0}


@pytest.mark.parametrize('cuenta', [AP76, PA15])
def test_bono_sesion_sin_operaciones_140(tmp_path, cuenta):
    # 500 + 140 (06/10) + 140 (07/10) + 140 (08/10, la primera con la regla nueva; la anterior estuvo sin operar)
    assert _margen(tmp_path, cuenta, [], _t(D3, '12:00:00')) == 920


@pytest.mark.parametrize('puntos, esperado', [(5.0, 854), (4.75, 783.8)])
def test_bono_sesion_70_si_el_neto_del_dia_llega_a_20_ticks_de_nq(tmp_path, puntos, esperado):
    """Neto exacto con comisión 0: 20 t de NQ = 100,00 $ -> +70; 19 t (95 $) -> +0. El consumo de la operación (ticks × 0,2)
    va aparte: 4 y 3,8. Con la regla de antes (consumo ≥ 0 -> +140) saldría 924 y 923,8."""
    m = _margen(tmp_path, AP76, [(D2, '08:00:00', puntos)], _t(D3, '12:00:00'), comision=SIN_COM)
    assert m == pytest.approx(esperado)


def test_bono_sesion_umbral_al_centimo(tmp_path):
    """100,00 $ justos -> +70; 99,99 $ (comisión de 0,005 por lado) -> +0."""
    a = _margen(tmp_path / 'a', AP76, [(D2, '08:00:00', 5.0)], _t(D3, '12:00:00'), comision={('Apex', 'NQ'): 0.0})
    b = _margen(tmp_path / 'b', AP76, [(D2, '08:00:00', 5.0)], _t(D3, '12:00:00'), comision={('Apex', 'NQ'): 0.005})
    assert (a, b) == (pytest.approx(854), pytest.approx(784))


def test_bono_sesion_dia_perdedor_0(tmp_path):
    # −20 t: consumo −4; el día fue negativo -> +0
    assert _margen(tmp_path, AP76, [(D2, '08:00:00', -5.0)], _t(D3, '12:00:00'), comision=SIN_COM) == pytest.approx(776)


def test_sesiones_anteriores_a_la_fecha_conservan_la_regla_de_antes(tmp_path):
    """El 07/10 recibe +140 por el consumo ≥ 0 del 06/10 (regla de §5); con la nueva sería +0 (neto de 4 t < 20 t)."""
    m = _margen(tmp_path, AP76, [(D1, '08:00:00', 1.0)], _t(D2, '12:00:00'), comision=SIN_COM)
    assert m == pytest.approx(500 + 140 + 0.8 + 140)
    # y la fecha es de reglas.yaml: moviéndola al 07/10 esa misma sesión ya usa la nueva
    ent, con, s = _entorno(tmp_path / 'b', [AP76], comision=SIN_COM)
    ent.reglas['plan4']['desde'] = date(2026, 10, 7)
    _base(con, AP76, 25000.0)
    _op(con, AP76, NQ, 1, 20000, 20001, _t(D1, '08:00:00'), _t(D1, '08:01:00'), 1)
    assert _bal_en(s.calcular(_t(D2, '12:00:00')), AP76)['margen'] == pytest.approx(500 + 140 + 0.8 + 0)


def test_bono_sesion_viernes_y_fin_de_semana(tmp_path):
    """Un viernes con 20 t o más da +70 el lunes; sábado y domingo no suman."""
    ent, con, s = _entorno(tmp_path, [AP76], comision=SIN_COM)
    _base(con, AP76, 25000.0)
    _op(con, AP76, NQ, 1, 20000, 20005, _t(D4, '08:00:00'), _t(D4, '08:01:00'), 1)
    sabado = _bal_en(s.calcular(_t(date(2026, 10, 10), '12:00:00')), AP76)['margen']
    lunes = _bal_en(s.calcular(_t(date(2026, 10, 12), '12:00:00')), AP76)['margen']
    assert sabado == pytest.approx(500 + 140 * 4 + 4)          # 06, 07, 08 y 09/10 sin operar; +4 del viernes
    assert lunes == pytest.approx(sabado + 70)


def test_el_bono_es_por_cuenta(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76, AP77], comision=SIN_COM)
    for c in (AP76, AP77):
        _base(con, c, 25000.0)
    _op(con, AP76, NQ, 1, 20000, 20005, _t(D2, '08:00:00'), _t(D2, '08:01:00'), 1)         # solo AP76 opera el 07/10
    res = s.calcular(_t(D3, '12:00:00'))
    assert (_bal_en(res, AP76)['margen'], _bal_en(res, AP77)['margen']) == (pytest.approx(854), pytest.approx(920))


def test_margen_levantado_conserva_su_140(tmp_path):
    """Día +50 (≥ 10 t, < 20 t) con el margen levantado: suma lo máximo y la sesión siguiente recibe +140 (no el +0 del día
    con menos de 20 t). Contador 23 por la semilla; margen −100 al empezar el 07/10."""
    sem = {'cuentas': {PA15: {'ultimos_dias_50': ['2026-09-10', '2026-09-14']}}}
    ent, con, s = _entorno(tmp_path, [PA15], semillas=sem, inicio='2026-10-07', margen_inicial=-100)
    ent.cuentas['cuentas'][PA15]['alta'] = '2026-10-07'
    _base(con, PA15, 25000.0, '2026-10-06 23:30:00.000')
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(D2, '08:00:00'), _t(D2, '08:05:00'), 1)        # +120 t: 58,98 $ = 11,8 t de NQ
    res = s.calcular(_t(D2, '20:00:00'))
    (f,) = filas_por(res, PA15)
    assert f.levantado and f.consumo == pytest.approx(24) and _bal_en(res, PA15)['margen'] == pytest.approx(-76)
    assert _bal_en(s.calcular(_t(D3, '12:00:00')), PA15)['margen'] == pytest.approx(-76 + 140)
    # el mismo día sin el margen levantado (contador bajo) recibe el +0 de la regla nueva (neto < 20 t)
    ent, con, s = _entorno(tmp_path / 'b', [PA15], inicio='2026-10-07', margen_inicial=-100)
    ent.cuentas['cuentas'][PA15]['alta'] = '2026-10-07'
    _base(con, PA15, 25000.0, '2026-10-06 23:30:00.000')
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(D2, '08:00:00'), _t(D2, '08:05:00'), 1)
    assert _bal_en(s.calcular(_t(D3, '12:00:00')), PA15)['margen'] == pytest.approx(-100 + 24 + 0)


def test_valores_del_bono_en_reglas_yaml(tmp_path):
    Y = config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'))
    B = Y['margen']['bono']
    assert (str(Y['plan4']['desde']), B['sin_operaciones'], B['neto_ticks'], B['con_neto'], B['sin_neto']) == (
        '2026-10-08', 140, 20, 70, 0) and 'desde' not in B                                  # una sola fecha para todo el Plan 4
    ent, con, s = _entorno(tmp_path, [AP76, AP77], comision=SIN_COM)
    for c in (AP76, AP77):
        _base(con, c, 25000.0)
    _op(con, AP76, NQ, 1, 20000, 20005, _t(D2, '08:00:00'), _t(D2, '08:01:00'), 1)         # 100 $: con_neto
    _op(con, AP77, NQ, 1, 20000, 20002.5, _t(D2, '08:00:00'), _t(D2, '08:01:00'), 2)       # 50 $: sin_neto
    B = ent.reglas['margen']['bono']
    B.update(con_neto=11, sin_neto=22, neto_ticks=10)
    res = s.calcular(_t(D3, '12:00:00'))
    assert (_bal_en(res, AP76)['margen'], _bal_en(res, AP77)['margen']) == (
        pytest.approx(780 + 4 + 11), pytest.approx(780 + 2 + 11))                          # 50 $ = 10 t ya llega
    B.update(neto_ticks=20)
    res = s.calcular(_t(D3, '12:00:00'))
    assert (_bal_en(res, AP76)['margen'], _bal_en(res, AP77)['margen']) == (
        pytest.approx(780 + 4 + 11), pytest.approx(780 + 2 + 22))
    B.update(sin_operaciones=33)
    ent.reglas['plan4']['desde'] = '2026-10-08'                                            # la fecha, como cadena también
    res = s.calcular(_t(D4, '12:00:00'))                    # el 09/10 la sesión anterior (08/10) no tuvo operaciones
    assert _bal_en(res, AP76)['margen'] == pytest.approx(780 + 4 + 11 + 33)


# ===========================================================================
# 10.3 Pruebas de 25K: contratos según la distancia al threshold en la apertura
# ===========================================================================

CLEAN = {('Apex', 'NQ'): 2.5, ('Apex', 'MNQ'): 0.25}          # 5,00 $ y 0,50 $ por operación: importes exactos


def _con_distancia(tmp_path, perdida, cuenta=AP76, **kw):
    """Prueba de 25K (balance 25.000, threshold 24.000) que el 08/10 pierde `perdida` ticks de NQ (1 contrato, comisión 5 $) y
    queda a (1000 − 5 × ticks − 5) ÷ 5 = 199 − ticks de su threshold: 124 -> 75,0 t y 125 -> 74,0 t."""
    ent, con, s = _entorno(tmp_path, [cuenta], comision=CLEAN, **kw)
    _base(con, cuenta, 25000.0)
    _op(con, cuenta, NQ, 1, 20000, 20000 - perdida * 0.25, _t(D3, '06:00:00'), _t(D3, '06:01:00'), 90)
    return ent, con, s


@pytest.mark.parametrize('perdida, simbolo, qty, exceso', [
    (124, NQ, 1, None), (124, MNQ, 1, None), (124, NQ, 2, '2 NQ con distancia 75 t'),
    (124, MNQ, 2, '2 MNQ con distancia 75 t'),                                   # lejos: 1 contrato en total, no 2
    (125, NQ, 1, '1 NQ con distancia 74 t'), (125, MNQ, 1, None), (125, MNQ, 2, '2 MNQ con distancia 74 t'),
    (125, NQ, 2, '2 NQ con distancia 74 t')])
def test_prueba_25k_contratos_segun_la_distancia(tmp_path, perdida, simbolo, qty, exceso):
    ent, con, s = _con_distancia(tmp_path, perdida)
    _op(con, AP76, simbolo, qty, 20000, 20001, _t(D3, '07:00:00'), _t(D3, '07:01:00'), 91)
    perdedora, f = filas_por(s.calcular(AHORA3), AP76)
    assert f.dist_ap == (75.0 if perdida == 124 else 74.0)                       # >= 75: lejos; < 75: cerca
    assert cortafuegos.detalle_exceso(f, ent.reglas) == exceso
    assert ('CONTRATOS > PERMITIDOS' in f.obs) == (exceso is not None)
    assert perdedora.dist_ap == 200                                              # la primera abre con el threshold lejos


def test_prueba_25k_nq_y_mnq_a_la_vez_son_dos_contratos(tmp_path):
    ent, con, s = _con_distancia(tmp_path, 124)
    _op(con, AP76, NQ, 1, 20000, 20001, _t(D3, '07:00:00'), _t(D3, '07:30:00'), 91)
    _op(con, AP76, MNQ, 1, 20000, 20001, _t(D3, '07:10:00'), _t(D3, '07:20:00'), 92)    # con el NQ abierto
    _, nq, mnq = filas_por(s.calcular(AHORA3), AP76)
    assert cortafuegos.detalle_exceso(nq, ent.reglas) is None
    assert cortafuegos.detalle_exceso(mnq, ent.reglas) == '1 NQ / 1 MNQ con distancia 75 t'   # el balance es el último plano


@pytest.mark.parametrize('perdida, permitidos', [(124, '1 NQ / 1 MNQ'), (125, '0 NQ / 1 MNQ')])
def test_prueba_25k_columna_bal_segun_la_distancia_actual(tmp_path, perdida, permitidos):
    ent, con, s = _con_distancia(tmp_path, perdida)
    b = _bal_en(s.calcular(AHORA3), AP76)
    assert b['permitidos'] == permitidos and b['distancia'] == 199 - perdida


def test_prueba_25k_e2t_tambien_y_la_de_50k_sigue_con_nq_equivalentes(tmp_path):
    ent, con, s = _entorno(tmp_path, [E250, BX], comision={('E2T', 'NQ'): 2.5})
    _base(con, E250, 25000.0)
    _base(con, BX, 50000.0)
    _op(con, E250, NQ, 2, 20000, 20001, _t(D3, '07:00:00'), _t(D3, '07:01:00'), 1)
    _op(con, BX, NQ, 2, 20000, 20001, _t(D3, '07:00:00'), _t(D3, '07:01:00'), 2)
    res = s.calcular(AHORA3)
    (e,), (b,) = filas_por(res, E250), filas_por(res, BX)
    assert cortafuegos.detalle_exceso(e, ent.reglas) == '2 NQ con distancia 300 t'     # E2T 25K: 1.500 de drawdown
    assert cortafuegos.detalle_exceso(b, ent.reglas) is None and (b.neq, b.neq_max) == (2, 2)   # 50K: 2 NQ-eq (§9.3)
    assert _bal_en(res, BX)['permitidos'] == '2 NQ / 20 MNQ'


def test_distancia_de_la_apertura_con_el_pico_hasta_ese_momento(tmp_path):
    """A gana 595 $ (balance 25.595: el threshold sube a 24.595) y B pierde 645 $: C abre a (24.950 − 24.595) ÷ 5 = 71 t. Con el
    threshold fijo en 24.000 serían 190 t. Un pico posterior (D) no cambia la distancia de C."""
    ent, con, s = _entorno(tmp_path, [AP76], comision=CLEAN)
    _base(con, AP76, 25000.0)
    _op(con, AP76, NQ, 1, 20000, 20030, _t(D3, '06:00:00'), _t(D3, '06:01:00'), 1)         # A: +120 t = +595
    _op(con, AP76, NQ, 1, 20000, 19968, _t(D3, '07:00:00'), _t(D3, '07:01:00'), 2)         # B: −128 t = −645
    _op(con, AP76, NQ, 1, 20000, 20001, _t(D3, '08:00:00'), _t(D3, '08:01:00'), 3)         # C
    a, b, c = filas_por(s.calcular(AHORA3), AP76)
    assert (a.dist_ap, b.dist_ap, c.dist_ap) == (200, 200, 71)
    assert cortafuegos.detalle_exceso(c, ent.reglas) == '1 NQ con distancia 71 t'
    _op(con, AP76, NQ, 1, 20000, 20050, _t(D3, '09:00:00'), _t(D3, '09:01:00'), 4)         # D: +995, pico nuevo
    _op(con, AP76, NQ, 1, 20000, 20001, _t(D3, '10:00:00'), _t(D3, '10:01:00'), 5)         # E
    a, b, c, d, e = filas_por(s.calcular(AHORA3), AP76)
    assert (c.dist_ap, d.dist_ap) == (71, 74) and e.dist_ap == 200


def test_distancia_con_el_pico_de_equity_del_log(tmp_path):
    """Un 'Bal:' del log con el flotante (equity 25.800 a las 08:05 de Madrid) sube el threshold a 24.800: lo que abre después
    está a 40 t; lo que abrió antes, a 200 t, y un pico posterior tampoco lo cambia."""
    ent, con, s = _entorno(tmp_path, [AP76], comision=CLEAN)
    _base(con, AP76, 25000.0)
    for t_log, bal in (('2026-10-08 08:05:00.000', 25800.0), ('2026-10-08 14:00:00.000', 26000.0)):
        con.execute('INSERT INTO equity (cuenta, t_log, bal, pnl_abierto, pnl_cerrado) VALUES (?, ?, ?, 0, 0)',
                    (AP76, t_log, bal))
    con.commit()
    _op(con, AP76, NQ, 1, 20000, 20000, _t(D3, '05:00:00'), _t(D3, '05:01:00'), 1)         # 07:00 de Madrid: antes del pico
    _op(con, AP76, NQ, 1, 20000, 20000, _t(D3, '08:00:00'), _t(D3, '08:01:00'), 2)         # 10:00 de Madrid
    a, b = filas_por(s.calcular(AHORA3), AP76)
    assert (a.dist_ap, b.dist_ap) == (200, 39) and cortafuegos.detalle_exceso(b, ent.reglas) == '1 NQ con distancia 39 t'


def test_prueba_25k_la_posicion_abierta_avisa_en_vivo_pero_sin_sonar(tmp_path):
    ent, con, s = _con_distancia(tmp_path, 125)                                  # distancia 74 t
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (AP76, NQ, 'a', 'Ea', 'Buy', 20000, 1, _t(D3, '07:00:00')))
    con.commit()                                                                  # 1 NQ abierto, sin salida
    res = s.calcular(AHORA3)
    (f,) = res.filas_abiertas
    assert f.dist_ap == 74 and cortafuegos.detalle_exceso(f, ent.reglas) == '1 NQ con distancia 74 t'
    eventos = [(k, texto, real) for k, t, texto, real, _son in cortafuegos.eventos_vivos(con, res, AHORA3, ent.reglas)]
    assert [e for e in eventos if e[0].startswith('CONTRATOS')] == [
        (f'CONTRATOS > PERMITIDOS|{f.clave}', 'INFRACCIÓN: CONTRATOS > PERMITIDOS (APEX-91120-76 NQZ6) 1 NQ con distancia 74 t', False)]


# --- Tope del SL con la distancia ---

def _sl_tras_perdida(tmp_path, perdida, simbolo=NQ, **kw):
    ent, con, s = _con_distancia(tmp_path, perdida, **kw)
    _tel(con, AP76, simbolo, 1, 20000, 20005, _t(D3, '07:00:00'), _t(D3, '07:05:00'), 91, sls=[(1, 999)])
    return sorted(s.calcular(AHORA3).filas, key=lambda f: f.t_apertura_utc)[1]


@pytest.mark.parametrize('perdida, tope', [(139, 60), (124, 75), (100, 75)])
def test_tope_del_sl_es_la_distancia_si_es_menor_que_375(tmp_path, perdida, tope):
    """Distancia 60 t (300 $) -> 300 ÷ 5 = 60; 75 t justos (375 $) y más -> 75, como antes."""
    f = _sl_tras_perdida(tmp_path, perdida)
    assert (f.sl_ini, f.sl_max, f.tope_sl, f.sl_ini_orden) == (tope, tope, tope, 999)


def test_tope_del_sl_con_la_distancia_en_mnq(tmp_path):
    f = _sl_tras_perdida(tmp_path, 139, simbolo=MNQ)                  # 300 $ ÷ (1 × 0,5 $) = 600 t
    assert (f.sl_ini, f.tope_sl) == (600, 600)


@pytest.mark.parametrize('perdida', [199, 250])
def test_tope_del_sl_con_distancia_cero_o_negativa_es_el_limite(tmp_path, perdida):
    f = _sl_tras_perdida(tmp_path, perdida)                           # balance en el threshold (0 t) o por debajo
    assert f.dist_ap <= 0 and (f.sl_ini, f.tope_sl) == (75, 75)


def test_tope_del_sl_con_distancia_desconocida_es_el_limite(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])                          # sin Bal: base
    _tel(con, AP76, NQ, 1, 20000, 20005, _t(D3, '07:00:00'), _t(D3, '07:05:00'), 1, sls=[(1, 999)])
    (f,) = filas_por(s.calcular(AHORA3), AP76)
    assert f.dist_ap is None and (f.sl_ini, f.tope_sl) == (75, 75)
    assert cortafuegos.detalle_exceso(f, ent.reglas) is None          # y la distancia desconocida cuenta como lejos


def test_tope_del_sl_mas_estrecho_cambia_ratio_y_aguantando_perdida(tmp_path):
    """Con 60 t de distancia el SL que cuenta es 60: el ratio es 20/60 y un MAE de −40 t (> 50 %) es AGUANTANDO PÉRDIDA."""
    ent, con, s = _con_distancia(tmp_path, 139)
    _tel(con, AP76, NQ, 1, 20000, 20005, _t(D3, '07:00:00'), _t(D3, '07:10:00'), 91, sls=[(1, 999)], flot=[(60, -200.0)])
    f = sorted(s.calcular(AHORA3).filas, key=lambda f: f.t_apertura_utc)[1]
    assert f.ratio == pytest.approx(20 / 60) and 'LARGA AGUANTANDO PÉRDIDA' in f.obs


# ===========================================================================
# 10.4 Pérdida de una decisión y bloqueo por pérdidas seguidas
# ===========================================================================

def _nq(con, cuenta, t_in, t_out, ticks, n, qty=1, simbolo=NQ):
    """Una operación de `qty` contratos que mueve `ticks` ticks por contrato (compra a 20.000), el 08/10."""
    _op(con, cuenta, simbolo, qty, 20000, 20000 + ticks * 0.25, _t(D3, t_in), _t(D3, t_out), n)


def _perdidas(con, cuenta, horas, ticks=-20, simbolo=NQ):
    """Una operación por (apertura, cierre) de `horas` (UTC, el 08/10), con ids consecutivos."""
    for i, (a, c) in enumerate(horas):
        _nq(con, cuenta, a, c, ticks, 100 + i, simbolo=simbolo)


@pytest.mark.parametrize('ticks, pausa', [(-20, True), (-19, False), (-80, True)])
def test_perdida_de_una_prueba_es_20_ticks_por_contrato(tmp_path, ticks, pausa):
    """Antes (−10 $ netos siempre) los tres eran pérdida: con −19 t una prueba de 1 NQ pierde 98,98 $, pero solo 19 ticks."""
    ent, con, s = _entorno(tmp_path, [AP76])
    _nq(con, AP76, '08:00:00', '08:02:00', ticks, 1)
    (f,) = filas_por(s.calcular(_t(D3, '12:00:00')), AP76)
    assert ('PÉRDIDA: NO OPERAR HASTA 10:12:12' in f.obs) is pausa           # cierre 10:02:00 + 612 s


def test_la_pausa_de_612_s_de_una_prueba_ya_no_salta_con_una_perdida_pequena(tmp_path):
    """−8 t (−43,98 $): pérdida de las de antes, ya no lo es para una prueba; y la reentrada de 5 min después no es infracción."""
    ent, con, s = _entorno(tmp_path, [AP76])
    _nq(con, AP76, '08:00:00', '08:02:00', -8, 1)
    _nq(con, AP76, '08:05:00', '08:06:00', 4, 2)
    a, b = filas_por(s.calcular(_t(D3, '12:00:00')), AP76)
    assert 'PÉRDIDA' not in a.obs and 'REENTRADA' not in b.obs


@pytest.mark.parametrize('ticks, pausa', [(-18, True), (-17, False)])
def test_perdida_de_una_real_es_10_dolares_netos(tmp_path, ticks, pausa):
    """MNQ con comisión de 1,00 $ por operación: −18 t = −9,00 − 1,00 = −10,00 $ justos."""
    ent, con, s = _entorno(tmp_path, [PA15], comision={('Apex', 'MNQ'): 0.5})
    _nq(con, PA15, '08:00:00', '08:02:00', ticks, 1, simbolo=MNQ)
    (f,) = filas_por(s.calcular(_t(D3, '12:00:00')), PA15)
    assert f.neto == (-10.0 if ticks == -18 else -9.5)
    assert ('PÉRDIDA: NO OPERAR HASTA 10:12:12' in f.obs) is pausa


def test_cuenta_que_mide_la_decision_real_si_la_hay(tmp_path):
    """La líder es una prueba (AP76, MNQ −24 t = −13,02 $) y su copia real (PA15) pierde −10 t (−6,02 $): mide la real, que no
    es pérdida. Antes la decisión se medía siempre con la líder (−13,02 $ -> pérdida)."""
    ent, con, s = _entorno(tmp_path, [AP76, PA15])
    _op(con, AP76, MNQ, 1, 20000, 19994, _t(D3, '08:00:00'), _t(D3, '08:02:00'), 1)
    _op(con, PA15, MNQ, 1, 20000, 19997.5, _t(D3, '08:00:00'), _t(D3, '08:02:00'), 2)
    res = s.calcular(_t(D3, '12:00:00'))
    lider, copia = filas_por(res, AP76)[0], filas_por(res, PA15)[0]
    assert copia.lider is lider and cortafuegos.cuenta_que_mide(lider) is copia
    (d,) = cortafuegos.decisiones(res.filas, res.filas_abiertas, ent.reglas)
    assert (d.perdedora, d.ganadora) == (False, False) and 'PÉRDIDA' not in lider.obs


def test_cuenta_que_mide_la_primera_real_por_nombre_y_sin_reales_la_lider(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76, PA15, PA16])
    _nq(con, AP76, '08:00:00', '08:02:00', -30, 1)               # la líder (prueba): −30 t
    _nq(con, PA16, '08:00:00', '08:02:00', -30, 2)               # real, la segunda por nombre: pierde
    _nq(con, PA15, '08:00:00', '08:02:00', 12, 3)                # real, la primera por nombre: gana
    res = s.calcular(_t(D3, '12:00:00'))
    lider = filas_por(res, AP76)[0]
    assert {c.cuenta for c in lider.copias} == {PA15, PA16} and cortafuegos.cuenta_que_mide(lider).cuenta == PA15
    (d,) = cortafuegos.decisiones(res.filas, res.filas_abiertas, ent.reglas)
    assert (d.perdedora, d.ganadora) == (False, True)
    ent, con, s = _entorno(tmp_path / 'b', [AP76, AP77])         # sin reales: la líder
    _nq(con, AP76, '08:00:00', '08:02:00', -30, 1)
    _nq(con, AP77, '08:00:00', '08:02:00', 12, 2)
    res = s.calcular(_t(D3, '12:00:00'))
    assert cortafuegos.cuenta_que_mide(filas_por(res, AP76)[0]).cuenta == AP76
    (d,) = cortafuegos.decisiones(res.filas, res.filas_abiertas, ent.reglas)
    assert (d.perdedora, d.ganadora) == (True, False)


def test_decision_ganadora_es_10_ticks_o_mas_de_la_cuenta_que_mide(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    for i, ticks in enumerate((10, 9, -19)):
        _nq(con, AP76, f'{8 + i:02d}:00:00', f'{8 + i:02d}:01:00', ticks, i + 1)
    res = s.calcular(_t(D3, '12:00:00'))
    ds = cortafuegos.decisiones(res.filas, res.filas_abiertas, ent.reglas)
    assert [(d.ganadora, d.perdedora) for d in ds] == [(True, False), (False, False), (False, False)]


def test_una_sim_activada_conserva_el_criterio_del_neto(tmp_path):
    ent, con, s = _entorno(tmp_path, [PA15])
    ent.cuentas['cuentas']['DEMO1'] = dict(FICHAS[AP76])
    ent.cuentas['sim_activas'] = ['DEMO1']
    _nq(con, 'DEMO1', '08:00:00', '08:02:00', -8, 1)             # −8 t = −43,98 $: pérdida por el neto
    (f,) = filas_por(s.calcular(_t(D3, '12:00:00')), 'DEMO1')
    assert 'PÉRDIDA: NO OPERAR HASTA 10:12:12' in f.obs


# --- racha global de pérdidas ---

def test_dos_perdidas_seguidas_preventivo_una_hora_desde_el_cierre(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    _perdidas(con, AP76, [('08:00:00', '08:02:00'), ('08:30:00', '08:32:00')])
    a, b = filas_por(s.calcular(_t(D3, '12:00:00')), AP76)
    assert 'PÉRDIDAS SEGUIDAS' not in a.obs                                  # una sola: nada
    assert b.obs.split(SEP)[-1] == '2 PÉRDIDAS SEGUIDAS: NO OPERAR HASTA 11:32:00' and b.cortafuegos   # 10:32 + 1 h; rojo


def test_tres_perdidas_seguidas_cada_una_desde_su_cierre(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    _perdidas(con, AP76, [('08:00:00', '08:02:00'), ('08:30:00', '08:32:00'), ('09:40:00', '09:42:00')])
    a, b, c = filas_por(s.calcular(_t(D3, '12:00:00')), AP76)
    assert '2 PÉRDIDAS SEGUIDAS: NO OPERAR HASTA 11:32:00' in b.obs
    assert '3 PÉRDIDAS SEGUIDAS: NO OPERAR HASTA 12:42:00' in c.obs and 'REENTRADA TRAS PÉRDIDAS' not in c.obs   # 09:40 ≥ 09:32


def test_reentrada_tras_perdidas_seguidas_antes_de_la_hora_es_infraccion(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    _perdidas(con, AP76, [('08:00:00', '08:02:00'), ('08:30:00', '08:32:00')])
    _nq(con, AP76, '09:31:59', '09:33:00', 12, 1)                              # 11:31:59, un segundo antes
    _nq(con, AP76, '10:35:00', '10:36:00', 12, 2)                              # 12:35, pasada la hora
    res = s.calcular(_t(D3, '12:00:00'))
    _, _, antes, despues = filas_por(res, AP76)
    assert 'REENTRADA TRAS PÉRDIDAS SEGUIDAS' in antes.obs.split(SEP) and antes.cortafuegos
    assert 'REENTRADA TRAS PÉRDIDAS SEGUIDAS' not in despues.obs
    inf = [i for i in res.cortafuegos[antes.clave]['infracciones'] if i['tipo'] == 'REENTRADA TRAS PÉRDIDAS SEGUIDAS']
    assert len(inf) == 1 and inf[0]['hora_permitida'].strftime('%H:%M:%S') == '11:32:00'
    # a la hora exacta ya se puede abrir
    ent, con, s = _entorno(tmp_path / 'b', [AP76])
    _perdidas(con, AP76, [('08:00:00', '08:02:00'), ('08:30:00', '08:32:00')])
    _nq(con, AP76, '09:32:00', '09:33:00', 12, 1)
    assert 'REENTRADA TRAS PÉRDIDAS SEGUIDAS' not in filas_por(s.calcular(_t(D3, '12:00:00')), AP76)[-1].obs


def test_una_ganadora_rompe_la_racha_y_una_neutra_no_suma_ni_rompe(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    for i, (ticks, a, c) in enumerate([(-20, '08:00:00', '08:02:00'), (10, '08:20:00', '08:22:00'),
                                       (-20, '08:40:00', '08:42:00')]):
        _nq(con, AP76, a, c, ticks, i + 1)
    assert not any('SEGUIDAS' in f.obs for f in filas_por(s.calcular(_t(D3, '12:00:00')), AP76))   # pérdida, ganadora, pérdida
    ent, con, s = _entorno(tmp_path / 'b', [AP76])
    for i, (ticks, a, c) in enumerate([(-20, '08:00:00', '08:02:00'), (9, '08:20:00', '08:22:00'),
                                       (-20, '08:40:00', '08:42:00')]):
        _nq(con, AP76, a, c, ticks, i + 1)
    a, n, b = filas_por(s.calcular(_t(D3, '12:00:00')), AP76)
    assert 'SEGUIDAS' not in a.obs + n.obs and '2 PÉRDIDAS SEGUIDAS: NO OPERAR HASTA 11:42:00' in b.obs    # +9 t: neutra


def test_el_cambio_de_sesion_cme_pone_la_racha_a_cero(tmp_path):
    """21:30 y 21:48 UTC son de la sesión del 08/10 (16:48 CT) y 22:10 UTC (17:10 CT), de la del 09/10."""
    ent, con, s = _entorno(tmp_path, [AP76])
    _perdidas(con, AP76, [('21:30:00', '21:32:00'), ('21:48:00', '21:50:00'), ('22:10:00', '22:12:00')])
    a, b, c = filas_por(s.calcular(_t(D4, '12:00:00')), AP76)
    assert '2 PÉRDIDAS SEGUIDAS' in b.obs and 'SEGUIDAS' not in c.obs        # la tercera es la 1ª de la sesión, y abre dentro de la hora
    ent, con, s = _entorno(tmp_path / 'b', [AP76])
    _perdidas(con, AP76, [('21:55:00', '21:57:00'), ('22:10:00', '22:12:00')])               # a caballo del cambio
    a, b = filas_por(s.calcular(_t(D4, '12:00:00')), AP76)
    assert 'SEGUIDAS' not in a.obs + b.obs


def test_la_racha_es_global_y_la_copia_no_cuenta(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76, AP77, PA15])
    # AP76 y PA15 son una sola decisión (abren a la vez); AP77, otra, más tarde: 2 decisiones perdedoras = racha 2, no 3
    _nq(con, AP76, '08:00:00', '08:02:00', -20, 1)
    _nq(con, PA15, '08:00:00', '08:02:00', -20, 2)
    _nq(con, AP77, '08:30:00', '08:32:00', -20, 3)
    res = s.calcular(_t(D3, '12:00:00'))
    (l1,), (l2,) = filas_por(res, AP76), filas_por(res, AP77)
    assert '2 PÉRDIDAS SEGUIDAS: NO OPERAR HASTA 11:32:00' in l2.obs and 'SEGUIDAS' not in l1.obs
    assert filas_por(res, PA15)[0].lider is l1


def test_reentrada_tras_perdidas_seguidas_registro_sonido_y_calificacion(tmp_path):
    """Una real (PA15, MNQ) con dos pérdidas seguidas y una tercera operación dentro de la hora: va a cortafuegos_saltados,
    suena, pone la celda en rojo y es proceso incorrecto en la Calificación aunque lleve estrategia."""
    ent, con, s = _entorno(tmp_path, [PA15])
    _perdidas(con, PA15, [('08:00:00', '08:02:00'), ('08:30:00', '08:32:00')], ticks=-40, simbolo=MNQ)
    _nq(con, PA15, '09:00:00', '09:02:00', 40, 1, simbolo=MNQ)
    res = s.calcular(_t(D3, '12:00:00'))
    l1, l2, x = filas_por(res, PA15)
    assert 'REENTRADA TRAS PÉRDIDAS SEGUIDAS' in x.obs.split(SEP) and x.cortafuegos
    cortafuegos.registrar(con, res.filas, res.cortafuegos, ent.reglas)
    assert [(r['decision_id'], r['tipo'], r['hora_permitida']) for r in con.execute(
        "SELECT * FROM cortafuegos_saltados WHERE tipo LIKE '%SEGUIDAS'")] == [
        (x.op_id, 'REENTRADA TRAS PÉRDIDAS SEGUIDAS', '2026-10-08 11:32:00.000')]
    a = Avisos()
    s.vigilar(a, _t(D3, '09:03:00'))
    assert 'INFRACCIÓN: REENTRADA TRAS PÉRDIDAS SEGUIDAS (PA-APEX-91120-15 MNQZ6). No operar hasta 11:32:00' in a
    motor.calificar(res.filas, res.cortafuegos, {f.decision_id: {'estrategia': X} for f in res.filas}, set(), ent.reglas)
    assert [(f.proceso_ok, f.calificacion) for f in (l1, l2, x)] == [
        (True, 'Buen Trade'), (True, 'Buen Trade'), (False, 'Trade Peligroso')]


# ===========================================================================
# 10.5 Etiqueta ESPEJO
# ===========================================================================

def _con_espejo(tmp_path, cuenta=PA16, simbolo=MNQ, flot=(), tps=(), qty=1, base=25000.0, n=1):
    """Operación con telemetría el 07/10 de 08:00 a 08:10 (UTC): +20 ticks, SL 100, el flotante y los TP que se pidan."""
    ent, con, s = _entorno(tmp_path, [cuenta])
    _base(con, cuenta, base)
    _tel(con, cuenta, simbolo, qty, 20000, 20005, _t(D2, '08:00:00'), _t(D2, '08:10:00'), n, sls=[(1, 100)],
         tps=list(tps), flot=list(flot))
    return ent, con, s


@pytest.mark.parametrize('flot, tps, espejo', [
    ([(60, -40.0)], [(120, 0)], True),                  # −80 t justos y TP en el precio de entrada, después
    ([(60, -39.5)], [(120, 0)], False),                 # −79 t: no llega al disparo
    ([(60, -40.0)], [(30, 0)], False),                  # el TP es anterior al disparo
    ([(60, -40.0)], [(60, 0)], False),                  # en el mismo instante: no es después
    ([(60, -40.0)], [(120, -10)], False),               # TP a −10 t: peor que el precio de entrada
    ([(60, -40.0)], [(30, 100), (120, 20)], True),      # TP puesto antes y modificado después a +20 t
    ([(60, -40.0)], [], False),                         # sin TP
    ([], [(120, 0)], False)])                           # sin flotante
def test_espejo_automatico_en_una_real(tmp_path, flot, tps, espejo):
    ent, con, s = _con_espejo(tmp_path, flot=flot, tps=tps)
    (f,) = filas_por(s.calcular(AHORA), PA16)
    assert f.espejo_auto is espejo


@pytest.mark.parametrize('cuenta, usd, espejo', [(AP76, -200.0, True), (AP76, -195.0, False),    # prueba de 25K: −40 t
                                                 (BX, -200.0, False), (BX, -400.0, True)])       # prueba de 50K: −80 t
def test_espejo_automatico_segun_el_disparo_de_la_prueba(tmp_path, cuenta, usd, espejo):
    ent, con, s = _con_espejo(tmp_path, cuenta=cuenta, simbolo=NQ, flot=[(60, usd)], tps=[(120, 0)],
                              base=50000.0 if cuenta == BX else 25000.0)
    (f,) = filas_por(s.calcular(AHORA), cuenta)
    assert f.espejo_auto is espejo


def test_disparos_en_reglas_yaml():
    E = config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'))['espejo']
    assert (E['etiqueta'], E['disparo_ticks'], E['palabra'], E['negacion'], E['tp_min_ticks']) == (
        'ESPEJO', {'real': -80, 'prueba': {25000: -40, 50000: -80}}, 'espejo', 'no espejo', 0)


def _celda_op(s, op_id, columna, texto):
    next(r for r in s.hoja.valores if r and r[0] == op_id)[vista.OC[columna]] = texto


def _calificada(tmp_path, tps):
    """La operación de _con_espejo (610 s, MAE −80 sobre un SL de 100: LARGA AGUANTANDO PÉRDIDA) con estrategia del operador."""
    ent, con, s = _con_espejo(tmp_path, flot=[(60, -40.0)], tps=tps)
    res, lay = s.sync(AHORA)
    (f,) = filas_por(res, PA16)
    _celda_op(s, f.op_id, 'Estrategia', X)
    res, lay = s.sync(AHORA)
    (f,) = filas_por(res, PA16)
    d = {r['op_id']: json.loads(r['datos']) for r in con.execute('SELECT op_id, datos FROM operaciones')}[f.op_id]
    return ent, con, s, res, lay, f, d


def test_espejo_es_de_analisis_sin_rojo_sin_sonido_y_sin_cambiar_la_calificacion(tmp_path):
    """Es proceso incorrecto (LARGA AGUANTANDO PÉRDIDA) aunque haya espejo: la misma Calificación que sin él."""
    ent, con, s, res, lay, f, d = _calificada(tmp_path / 'con', [(120, 0)])
    assert f.espejo and f.espejo_origen == 'auto' and f.obs.split(SEP)[-1] == 'ESPEJO'
    assert not f.cortafuegos and (lay['ops_ini'], vista.OC['Observaciones demonio']) not in lay['celdas_cortafuegos']
    assert (d['calificacion'], d['proceso_ok'], d['espejo'], d['espejo_origen']) == ('Trade Peligroso', False, True, 'auto')
    a = Avisos()
    s.vigilar(a, AHORA)
    assert a == []
    _, _, _, _, _, g, e = _calificada(tmp_path / 'sin', [(120, -10)])                       # el mismo caso sin espejo
    assert not g.espejo and 'ESPEJO' not in g.obs
    assert (e['calificacion'], e['proceso_ok'], e['espejo'], e['espejo_origen']) == ('Trade Peligroso', False, False, 'auto')


def _observaciones(s, op_id, texto):
    _celda_op(s, op_id, 'Observaciones', texto)


@pytest.mark.parametrize('flot, tps, texto, espejo, origen', [
    ([(60, -40.0)], [(120, -10)], 'ESPEJO', True, 'manual'),                  # sin espejo automático: lo pone el operador
    ([(60, -40.0)], [(120, -10)], 'salí con espejo, ojo', True, 'manual'),
    ([(60, -40.0)], [(120, 0)], 'No   Espejo', False, 'manual'),              # lo quita (manda lo manual)
    ([(60, -40.0)], [(120, 0)], 'espejo pero NO ESPEJO al final', False, 'manual'),
    ([(60, -40.0)], [(120, 0)], 'espejos', True, 'auto'),                     # otra palabra: no es manual
    ([(60, -40.0)], [(120, 0)], 'nada', True, 'auto'),
    ([], [], 'nada', False, 'auto')])
def test_espejo_manual_en_observaciones_manda_sobre_el_automatico(tmp_path, flot, tps, texto, espejo, origen):
    ent, con, s = _con_espejo(tmp_path, flot=flot, tps=tps)
    res, lay = s.sync(AHORA)
    op_id = filas_por(res, PA16)[0].op_id
    _observaciones(s, op_id, texto)
    res, lay = s.sync(AHORA)
    (f,) = filas_por(res, PA16)
    assert (f.espejo, f.espejo_origen) == (espejo, origen)
    assert ('ESPEJO' in f.obs.split(SEP)) is espejo
    fila = next(r for r in lay['valores'] if r and r[0] == op_id)
    assert ('ESPEJO' in fila[vista.OC['Observaciones demonio']].split(SEP)) is espejo
    assert fila[vista.OC['Observaciones']] == texto                           # la columna del operador no se toca
    d = {r['op_id']: json.loads(r['datos']) for r in con.execute('SELECT op_id, datos FROM operaciones')}[op_id]
    assert (d['espejo'], d['espejo_origen']) == (espejo, origen)


def test_la_seguidora_usa_el_espejo_de_su_lider(tmp_path):
    ent, con, s = _entorno(tmp_path, [PA15, PA16])
    for c in (PA15, PA16):
        _base(con, c, 25000.0)
    _tel(con, PA15, MNQ, 1, 20000, 20005, _t(D2, '08:00:00'), _t(D2, '08:10:00'), 1, sls=[(1, 100)], tps=[(120, 0)],
         flot=[(60, -40.0)])
    _op(con, PA16, MNQ, 1, 20000, 20005, _t(D2, '08:00:00'), _t(D2, '08:10:00'), 2)         # copia por posición: sin órdenes
    res, lay = s.sync(AHORA)
    lider, copia = filas_por(res, PA15)[0], filas_por(res, PA16)[0]
    assert copia.lider is lider and (lider.espejo, copia.espejo, copia.espejo_origen) == (True, True, 'auto')
    d = {r['op_id']: json.loads(r['datos']) for r in con.execute('SELECT op_id, datos FROM operaciones')}
    assert (d[copia.op_id]['espejo'], d[copia.op_id]['espejo_origen']) == (True, 'auto')
    _observaciones(s, lider.op_id, 'no espejo')                                            # lo manual de la decisión
    res, lay = s.sync(AHORA)
    lider, copia = filas_por(res, PA15)[0], filas_por(res, PA16)[0]
    assert (lider.espejo, copia.espejo, copia.espejo_origen) == (False, False, 'manual')


# ===========================================================================
# 10.6 Preoperativa, paso 2: compuerta
# ===========================================================================

TEXTOS = ['Sueño ≥ 6 h (reloj)', 'Ni enfermo ni con conflicto personal abierto', 'Plan leído hoy', 'Ayer sin infracción']


def _gate(s, lay, valores):
    """El operador pone los valores en las 4 celdas de la compuerta (columna de Estrategia)."""
    for i, v in enumerate(valores):
        s.hoja.valores[lay['compuerta_fila'] + i][lay['preop_col']] = v


def _etiquetas_compuerta(res):
    return {f.op_id: 'COMPUERTA SIN SÍ' in f.obs.split(SEP) for f in res.filas if not f.es_seguidora}


def test_compuerta_filas_textos_y_desplegables(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    res, lay = s.sync(AHORA3)
    assert ent.reglas['preoperativa']['compuerta']['filas'] == TEXTOS
    assert lay['compuerta_fila'] == lay['preop_fila'] + 1 and lay['ops_header'] == lay['compuerta_fila'] + 4
    for i, t in enumerate(TEXTOS):
        fila = lay['valores'][lay['compuerta_fila'] + i]
        assert fila[0] == t and fila[lay['preop_col']] == '' and not any(fila[1:lay['preop_col']])
    validaciones = [r['setDataValidation'] for r in _formatos(ent, lay) if 'setDataValidation' in r]
    assert len(validaciones) == 3                                              # la estrategia, el estado del mercado (§12) y la compuerta
    comp = validaciones[2]
    assert [v['userEnteredValue'] for v in comp['rule']['condition']['values']] == ['SÍ', 'NO'] and comp['rule']['strict']
    assert comp['range'] == {'sheetId': 0, 'startRowIndex': lay['compuerta_fila'], 'endRowIndex': lay['compuerta_fila'] + 4,
                             'startColumnIndex': vista.OC['Estrategia'], 'endColumnIndex': vista.OC['Estrategia'] + 1}
    manual, _, ok = vista.parsear_hoja(lay['valores'])                         # no se confunde con BAL ni operaciones
    assert ok and not any(t in manual for t in TEXTOS)


def test_la_lectura_periodica_pide_las_cinco_filas_en_una_sola_peticion(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    res, lay = s.sync(AHORA3)
    llamadas, leer = [], s.hoja.leer_filas
    s.hoja.leer_filas = lambda *a: (llamadas.append(a), leer(*a))[1]
    s.vigilar_preoperativa(_t(D3, '20:00:05'))
    assert llamadas == [(lay['preop_fila'] - 1, lay['preop_col'], 6)]           # mercado (§12) + estrategia + 4 de la compuerta


def test_leer_filas_devuelve_siempre_n_filas(tmp_path):
    class Cliente:
        def spreadsheets(self):
            return self

        def values(self):
            return self

        def get(self, **kw):
            return self

        def execute(self):
            return {'values': [['Estrategia a usar', '', 'Sin estrategia'], ['Sueño ≥ 6 h (reloj)', '', 'SÍ']]}   # la API omite las vacías finales

    h = sheets.HojaV4.__new__(sheets.HojaV4)
    h.svc_rapido, h.sid, h.q = Cliente(), 'id', "'V4'"
    assert h.leer_filas(8, 2, 5) == [['Estrategia a usar', '', 'Sin estrategia'], ['Sueño ≥ 6 h (reloj)', '', 'SÍ'], [], [], []]


def test_leer_compuerta_pestana_antigua_y_textos_cambiados(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    res, lay = s.sync(AHORA3)
    _gate(s, lay, ['SÍ', 'sí', 'NO', ''])
    v = [list(r) for r in s.hoja.valores]
    assert vista.leer_compuerta(v, ent.reglas) == ['SÍ', 'SÍ', 'NO', ''] and vista.leer_preoperativa(v, ent.reglas) == 'Sin estrategia'
    viejo = v[:lay['compuerta_fila']] + v[lay['compuerta_fila'] + 4:]            # pestaña anterior: sin las 4 filas
    assert vista.leer_compuerta(viejo, ent.reglas) is None and vista.leer_preoperativa(viejo, ent.reglas) == 'Sin estrategia'
    ent.reglas['preoperativa']['compuerta']['filas'][0] = 'Dormí bien'            # el operador cambió el texto en reglas.yaml
    assert vista.leer_compuerta(v, ent.reglas) is None                           # hasta el siguiente sync no se lee


def test_compuerta_la_ultima_lectura_anterior_a_la_apertura(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    _base(con, AP76, 25000.0)
    abre = {'A': '06:00:00', 'B': '07:05:00', 'C': '07:20:00', 'D': '07:30:03', 'E': '07:40:00'}
    for i, hms in enumerate(abre.values()):
        h, m, sec = map(int, hms.split(':'))
        _nq_d3(con, f'{h:02d}:{m:02d}:{sec:02d}', f'{h:02d}:{m + 1:02d}:{sec:02d}', 10, i + 1)
    res, lay = s.sync(_t(D3, '07:00:00'))                    # primera lectura de todas: la compuerta vacía
    _gate(s, lay, ['SÍ'] * 4)
    s.vigilar_preoperativa(_t(D3, '07:10:05'))
    _gate(s, lay, ['SÍ', 'NO', 'SÍ', 'SÍ'])
    s.vigilar_preoperativa(_t(D3, '07:30:05'))
    assert [(t[11:19], v) for t, v in almacen.lecturas_compuerta(con)] == [
        ('07:00:00', ['', '', '', '']), ('07:10:05', ['SÍ'] * 4), ('07:30:05', ['SÍ', 'NO', 'SÍ', 'SÍ'])]
    res, lay = s.sync(_t(D3, '08:00:00'))
    ops = {f.t_apertura_utc[11:19]: f for f in res.filas if not f.es_seguidora}
    etiquetadas = {k: 'COMPUERTA SIN SÍ' in ops[v].obs.split(SEP) for k, v in abre.items()}
    # A abrió antes de la primera lectura (no se juzga); B con la compuerta vacía; C con todo en SÍ; D, 2 s antes de la lectura que
    # pone un NO (cuenta la anterior: SÍ); E con un NO
    assert etiquetadas == {'A': False, 'B': True, 'C': False, 'D': False, 'E': True}


def test_compuerta_los_valores_sobreviven_a_la_reescritura_de_la_pestana(tmp_path):
    """Cada sync reescribe la pestaña entera: las 4 celdas vuelven a ponerse con lo último leído (y no se pisan con vacío)."""
    ent, con, s = _entorno(tmp_path, [AP76])
    res, lay = s.sync(_t(D3, '07:00:00'))
    _gate(s, lay, ['SÍ', 'NO', 'SÍ', ''])
    res, lay = s.sync(_t(D3, '07:30:00'))
    celdas = lambda: [s.hoja.valores[lay['compuerta_fila'] + i][lay['preop_col']] for i in range(4)]
    assert celdas() == ['SÍ', 'NO', 'SÍ', ''] and [lay['valores'][lay['compuerta_fila'] + i][lay['preop_col']] for i in range(4)] == celdas()
    s.sync(_t(D3, '07:40:00'))
    assert celdas() == ['SÍ', 'NO', 'SÍ', ''] and [t[11:19] for t, _ in almacen.lecturas_compuerta(con)] == ['07:00:00', '07:30:00']


def test_compuerta_una_lectura_en_el_mismo_instante_de_la_apertura_cuenta(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    _base(con, AP76, 25000.0)
    res, lay = s.sync(_t(D3, '07:00:00'))
    _gate(s, lay, ['SÍ'] * 4)
    s.vigilar_preoperativa(_t(D3, '07:10:05'))
    _nq_d3(con, '07:10:05', '07:11:00', 10, 1)                                 # abre en el instante de la lectura con todo en SÍ
    res, lay = s.sync(_t(D3, '08:00:00'))
    assert _etiquetas_compuerta(res) == {res.filas[0].op_id: False}


def _nq_d3(con, t_in, t_out, ticks, n):
    _op(con, AP76, NQ, 1, 20000, 20000 + ticks * 0.25, _t(D3, t_in), _t(D3, t_out), n)


def test_compuerta_se_vacia_al_empezar_la_sesion_y_no_se_leen_valores_viejos(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    _base(con, AP76, 25000.0)
    res, lay = s.sync(_t(D3, '07:00:00'))
    _gate(s, lay, ['SÍ'] * 4)
    s.vigilar_preoperativa(_t(D3, '07:10:05'))
    n = len(almacen.lecturas_compuerta(con))
    # 22:30 UTC del 08/10 ya es la sesión CME del 09/10 (17:30 CT): el sheet aún tiene los valores de la sesión anterior
    _gate(s, lay, ['SÍ', 'NO', 'SÍ', 'SÍ'])
    s.vigilar_preoperativa(_t(D3, '22:30:00'))
    assert len(almacen.lecturas_compuerta(con)) == n                           # no los anota como lectura de la sesión nueva
    _nq_d3(con, '22:40:00', '22:41:00', 10, 1)                                 # F: ya en la sesión del 09/10, sin rellenar
    res, lay = s.sync(_t(D3, '22:45:00'))
    assert [s.hoja.valores[lay['compuerta_fila'] + i][lay['preop_col']] for i in range(4)] == [''] * 4     # vaciada
    assert almacen.lecturas_compuerta(con)[-1][1] == [''] * 4
    _gate(s, lay, ['SÍ'] * 4)
    s.vigilar_preoperativa(_t(D4, '08:30:05'))
    _op(con, AP76, NQ, 1, 20000, 20001, _t(D4, '09:00:00'), _t(D4, '09:01:00'), 2)         # G: con todo en SÍ
    res, lay = s.sync(_t(D4, '10:00:00'))
    ops = {f.t_apertura_utc[:19]: f for f in res.filas}
    assert _etiquetas_compuerta(res) == {ops[f'{D3} 22:40:00'].op_id: True, ops[f'{D4} 09:00:00'].op_id: False}


def test_compuerta_sin_si_es_solo_aviso(tmp_path):
    """Sin rojo, sin sonido y sin hacer incorrecto el proceso (Calificación igual que con todo en SÍ)."""
    ent, con, s = _entorno(tmp_path, [AP76])
    _base(con, AP76, 25000.0)
    _nq_d3(con, '07:05:00', '07:07:00', 20, 1)
    res, lay = s.sync(_t(D3, '07:00:00'))
    res, lay = s.sync(_t(D3, '08:00:00'))
    (f,) = res.filas
    assert 'COMPUERTA SIN SÍ' in f.obs.split(SEP) and not f.cortafuegos
    assert (lay['ops_ini'], vista.OC['Observaciones demonio']) not in lay['celdas_cortafuegos']
    a = Avisos()
    s.vigilar(a, _t(D3, '07:07:30'))
    assert a == []
    motor.calificar(res.filas, res.cortafuegos, {f.decision_id: {'estrategia': X}}, set(), ent.reglas)
    assert (f.proceso_ok, f.calificacion) == (True, 'Trade Perfecto')


def test_sin_ninguna_lectura_previa_no_se_juzga_la_compuerta(tmp_path):
    """Antes de que existan las filas (todo lo ya anotado al desplegar este cambio) no hay etiqueta."""
    ent, con, s = _entorno(tmp_path, [AP76])
    _base(con, AP76, 25000.0)
    _nq_d3(con, '05:00:00', '05:01:00', 10, 1)
    res = s.calcular(AHORA3)
    motor.marcar_analisis(res.filas, almacen.lecturas_compuerta(con), res.reloj, {}, ent.reglas)
    assert 'COMPUERTA' not in res.filas[0].obs


# ===========================================================================
# La fecha: las reglas nuevas rigen SOLO desde la sesión CME `plan4.desde` (08/10/2026), por la sesión de la APERTURA
# ===========================================================================

def test_la_fecha_es_la_sesion_cme_no_el_dia_natural(tmp_path):
    ent = _entorno(tmp_path, [PA15])[0]
    R = ent.reglas
    assert str(R['plan4']['desde']) == '2026-10-08' and not motor.regla_nueva(D2, R) and motor.regla_nueva(D3, R)
    # 21:59:30 UTC del 07/10 son las 16:59:30 CT: aún sesión del 07/10; 22:00:30 UTC, ya la del 08/10
    assert [motor.sesion_cme(_t(D2, h)) for h in ('21:59:30', '22:00:30')] == [D2, D3]
    ent, con, s = _entorno(tmp_path / 'b', [PA15])
    _op(con, PA15, NQ, 1, 20000, 20001, _t(D2, '21:59:20'), _t(D2, '21:59:40'), 1)         # 1 NQ: la regla de antes lo permite
    _op(con, PA15, NQ, 1, 20000, 20001, _t(D2, '22:00:20'), _t(D2, '22:00:40'), 2)         # 1 NQ: NQ 0
    a, b = filas_por(s.calcular(_t(D3, '12:00:00')), PA15)
    assert (a.sesion, a.permitidos, b.sesion, b.permitidos) == (D2, 1, D3, 0)
    assert 'CONTRATOS > PERMITIDOS' not in a.obs and 'CONTRATOS > PERMITIDOS' in b.obs


def test_frontera_contratos_de_las_reales_nq_y_141(tmp_path):
    """§10.1 y §10.2: el 07/10 conserva 1 NQ desde 141 y 141 justo -> 1 contrato; el 08/10, NQ 0 y 141 o menos -> 0."""
    ent, con, s = _entorno(tmp_path, [PA15])
    _op(con, PA15, NQ, 1, 20000, 20001, _t(D2, '08:00:00'), _t(D2, '08:01:00'), 1)
    _op(con, PA15, NQ, 1, 20000, 20001, _t(D3, '08:00:00'), _t(D3, '08:01:00'), 2)
    a, b = filas_por(s.calcular(_t(D3, '12:00:00')), PA15)
    assert (a.permitidos, cortafuegos.detalle_exceso(a, ent.reglas)) == (1, None)
    assert (b.permitidos, cortafuegos.detalle_exceso(b, ent.reglas)) == (0, '1 contratos con 0 permitidos')
    for inicio, d, permitidos in (('2026-10-07', D2, 1), ('2026-10-08', D3, 0)):             # margen 141 justo
        ent, con, s = _entorno(tmp_path / inicio, [PA15], inicio=inicio, margen_inicial=141)
        ent.cuentas['cuentas']['DEMO1'] = dict(FICHAS[AP76])
        ent.cuentas['sim_activas'] = ['DEMO1']
        _op(con, PA15, MNQ, 1, 20000, 20001, _t(d, '08:00:00'), _t(d, '08:01:00'), 1)
        _op(con, 'DEMO1', NQ, 1, 20000, 20001, _t(d, '08:00:00'), _t(d, '08:01:00'), 2)      # y una sim: la tabla general
        res = s.calcular(_t(d, '12:00:00'))
        assert (filas_por(res, PA15)[0].permitidos, filas_por(res, 'DEMO1')[0].permitidos) == (permitidos, permitidos)


def test_frontera_margen_levantado_con_141(tmp_path):
    sem = {'cuentas': {PA15: {'ultimos_dias_50': ['2026-09-10', '2026-09-14']}}}
    res, margenes = [], []
    for inicio, d in (('2026-10-07', D2), ('2026-10-08', D3)):
        ent, con, s = _entorno(tmp_path / inicio, [PA15], semillas=sem, margen_inicial=141, inicio=inicio)
        _op(con, PA15, MNQ, 1, 20000, 20030, _t(d, '08:00:00'), _t(d, '08:05:00'), 1)         # día +50 (58,98 $)
        f = filas_por(s.calcular(_t(d, '12:00:00')), PA15)[0]
        res.append(f.levantado)
        _mae_manual(con, f.op_id, -80)
        margenes.append(filas_por(s.calcular(_t(d, '12:00:00')), PA15)[0].margen_tras)
    assert res == [False, True]                 # 141: con la regla de antes (< 141) no se levanta; desde el 08/10 (≤ 141), sí
    # y con el margen levantado el día +50 "suma lo máximo" (el consumo no baja de 0): 141 frente a 141 + 24 − 80
    assert margenes == [85, 141]


def test_frontera_prueba_25k_contratos_y_tope_del_sl(tmp_path):
    """2 NQ con SL 999 tras una pérdida (distancia al threshold ~60 t): el 07/10, 2 NQ-equivalentes y tope de 375 $ (37,5 t por
    contrato); el 08/10, `CONTRATOS > PERMITIDOS` por la distancia y tope con la distancia."""
    ent, con, s = _entorno(tmp_path, [AP76], comision=CLEAN)
    _base(con, AP76, 25000.0)
    _op(con, AP76, NQ, 1, 20000, 20000 - 139 * 0.25, _t(D1, '06:00:00'), _t(D1, '06:01:00'), 90)     # distancia 60 t
    _tel(con, AP76, NQ, 2, 20000, 20000, _t(D2, '07:00:00'), _t(D2, '07:05:00'), 91, sls=[(1, 999)])      # sin resultado: −10 $
    _tel(con, AP76, NQ, 2, 20000, 20005, _t(D3, '07:00:00'), _t(D3, '07:05:00'), 92, sls=[(1, 999)])
    _, a, b = filas_por(s.calcular(AHORA3), AP76)
    assert (a.sesion, a.dist_regla, a.neq, a.neq_max, cortafuegos.detalle_exceso(a, ent.reglas)) == (D2, False, 2, 2, None)
    assert (a.sl_ini, a.tope_sl) == (37.5, 37.5)
    assert (b.sesion, b.dist_regla, b.neq) == (D3, True, None)
    assert cortafuegos.detalle_exceso(b, ent.reglas).startswith('2 NQ con distancia')
    assert b.tope_sl == round(min(375, motor.usd_de_ticks(b.dist_ap, ent.reglas)) / 10, 2) and b.tope_sl < 37.5


def test_frontera_perdida_pausa_de_612_s_y_perdidas_seguidas(tmp_path):
    """Una prueba pierde −8 t (−43,98 $) y reentra 3 min después; dos pérdidas de −20 t seguidas y una entrada dentro de la hora.
    El 07/10 (regla de antes: −10 $ netos, sin racha) sí hay pausa y reentrada y no hay racha; el 08/10 es al revés."""
    ent, con, s = _entorno(tmp_path, [AP76])
    for d, base in ((D2, 0), (D3, 10)):
        _op(con, AP76, NQ, 1, 20000, 19998, _t(d, '08:00:00'), _t(d, '08:02:00'), base + 1)           # −8 t
        _op(con, AP76, NQ, 1, 20000, 20001, _t(d, '08:05:00'), _t(d, '08:06:00'), base + 2)           # reentra a los 3 min
        _op(con, AP76, NQ, 1, 20000, 19995, _t(d, '12:00:00'), _t(d, '12:02:00'), base + 3)           # −20 t
        _op(con, AP76, NQ, 1, 20000, 19995, _t(d, '12:30:00'), _t(d, '12:32:00'), base + 4)           # −20 t: 2ª seguida
        _op(con, AP76, NQ, 1, 20000, 20003, _t(d, '13:00:00'), _t(d, '13:01:00'), base + 5)           # dentro de la hora
    res = s.calcular(AHORA3)
    v = filas_por(res, AP76)
    viejas, nuevas = [f.obs for f in v[:5]], [f.obs for f in v[5:]]
    assert 'PÉRDIDA: NO OPERAR HASTA 10:12:12' in viejas[0] and 'REENTRADA TRAS PÉRDIDA' in viejas[1]
    assert not any('SEGUIDAS' in o for o in viejas)
    assert 'PÉRDIDA: NO OPERAR' not in nuevas[0] and 'REENTRADA' not in nuevas[1]
    assert '2 PÉRDIDAS SEGUIDAS' in nuevas[3] and 'REENTRADA TRAS PÉRDIDAS SEGUIDAS' in nuevas[4]
    ds = cortafuegos.decisiones(res.filas, res.filas_abiertas, ent.reglas)
    assert [d.perdedora for d in ds[:5]] == [None] * 5 and all(d.perdedora is not None for d in ds[5:])
    cortafuegos.registrar(con, res.filas, res.cortafuegos, ent.reglas)
    assert [r['decision_id'] for r in con.execute("SELECT decision_id FROM cortafuegos_saltados WHERE tipo LIKE '%SEGUIDAS'")] == [
        v[9].op_id]


def test_frontera_compuerta(tmp_path):
    """Con la compuerta vacía, una decisión del 07/10 no lleva `COMPUERTA SIN SÍ` y una del 08/10, sí."""
    ent, con, s = _entorno(tmp_path, [AP76])
    _base(con, AP76, 25000.0)
    _nq_d3(con, '08:00:00', '08:01:00', 10, 1)
    _op(con, AP76, NQ, 1, 20000, 20001, _t(D2, '07:05:00'), _t(D2, '07:06:00'), 2)
    s.sync(_t(D2, '07:00:00'))                                      # primera lectura de la compuerta (vacía), sesión del 07/10
    s.sync(_t(D2, '22:30:00'))                                      # primer sync de la sesión del 08/10: vacía otra vez
    res, lay = s.sync(_t(D3, '20:00:00'))
    ops = {f.sesion: f for f in res.filas}
    assert _etiquetas_compuerta(res) == {ops[D2].op_id: False, ops[D3].op_id: True}


def test_espejo_tambien_hacia_atras(tmp_path):
    """ESPEJO es de análisis: se marca en las operaciones anteriores a plan4.desde."""
    ent, con, s = _entorno(tmp_path, [PA16])
    _base(con, PA16, 25000.0)
    _tel(con, PA16, MNQ, 1, 20000, 20005, _t(D, '08:00:00'), _t(D, '08:10:00'), 1, sls=[(1, 100)], tps=[(120, 0)],
         flot=[(60, -40.0)])
    res, lay = s.sync(AHORA3)
    (f,) = filas_por(res, PA16)
    assert f.sesion == D and f.espejo and f.obs.split(SEP)[-1] == 'ESPEJO'


def test_lo_que_es_estado_actual_y_la_comision_no_dependen_de_la_fecha(tmp_path):
    """La columna `Contratos permitidos` del BAL usa las reglas nuevas aunque todas las operaciones sean anteriores; la comisión
    de E2T NQ (2,04) también se aplica hacia atrás."""
    ent, con, s = _entorno(tmp_path, [PA15, E250])
    for c in (PA15, E250):
        _base(con, c, 25000.0)
    _op(con, PA15, NQ, 1, 20000, 20001, _t(D, '08:00:00'), _t(D, '08:01:00'), 1)
    _op(con, E250, NQ, 1, 20000, 20000, _t(D, '08:00:00'), _t(D, '08:01:00'), 2)
    res = s.calcular(_t(D2, '12:00:00'))
    assert _bal_en(res, PA15)['permitidos'] == '0 NQ / 3 MNQ' and _bal_en(res, E250)['permitidos'] == '1 NQ / 1 MNQ'
    assert motor.comision_por_lado('E2T', NQ, ent.reglas) == 2.04
    (e,) = filas_por(res, E250)
    assert (e.comision, e.neto) == (4.08, -4.08)


def _literales(ruta):
    """Constantes (texto y números) del código de un módulo, sin docstrings ni comentarios."""
    arbol = ast.parse(open(ruta, encoding='utf-8').read())
    doc = {id(n.body[0].value) for n in ast.walk(arbol) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
           and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(arbol) if isinstance(n, ast.Constant) and id(n) not in doc]


def test_ningun_valor_de_negocio_nuevo_en_el_codigo():
    """§10: textos, umbrales y fechas nuevos viven en reglas.yaml (regla de oro del informe)."""
    literales = [x for f in ('motor.py', 'cortafuegos.py', 'sync.py', 'vista.py', 'sheets.py', 'almacen.py', 'cuentas.py')
                 for x in _literales(os.path.join(RAIZ, 'atasv4', f))]
    prohibidos = ['ESPEJO', 'COMPUERTA SIN SÍ', 'REENTRADA TRAS PÉRDIDAS SEGUIDAS', 'SÍ', 'NO', 'livesim', 'no espejo', '2026-10-08',
                  3600, -80, -40, 141, 75, 140, 70]
    assert [x for x in literales if x in prohibidos] == []
    assert not [x for x in literales if isinstance(x, str) and 'PÉRDIDAS SEGUIDAS' in x]
