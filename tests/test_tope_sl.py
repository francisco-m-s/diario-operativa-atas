"""Tope del SL por el Loss Limit de Rithmic en las pruebas (spec §9.10, decisión del operador del 07/10/2026).

Tope (ticks por contrato) = `pruebas.tope_sl.limite_usd` (375) ÷ (contratos de la fila × valor del tick): 1 NQ 75 t, 2 NQ 37,5 t,
1 MNQ 750 t. Cada SL en riesgo (> 0 t) de una prueba (también seguidoras) con sesión CME ≥ 2026-10-07 pasa a mín(SL, tope)
en todo lo que usa el SL; los SL en beneficio no se tocan; el SL de la orden queda en SQLite; la nota de SL de Observaciones
demonio pasa a `SL BE +1→+12→+30`. Todo sintético: fills de MyTrade y del log, órdenes SL/TP y flotante escritos en SQLite
(el log va en hora de Madrid, UTC + 2 h en octubre).
"""

import json
import os
from datetime import date, timedelta

import pytest

from atasv4 import config, cortafuegos, fuentes, motor, vista
from conftest import RAIZ
from test_apex_pruebas import (AP76, AP77, BX, D1, D2, E250, FICHAS, MNQ, NQ, PA15, PA16, _base, _bal, _entorno, _op,
                               _t)

AHORA = _t(D2, '20:00:00')                      # sesión CME del 07/10 (15:00 CT)
SL_COL, OBS_COL = vista.OC['SL (ticks)'], vista.OC['Observaciones demonio']
X = 'Continuación de M204s confirmada por M17'


def _tel(con, cuenta, simbolo, qty, entrada, salida, t_in, t_out, n, sls=(), tps=(), flot=(), lado='Buy', cancel=()):
    """Operación con telemetría (fills de MyTrade y del log). t_in y t_out en UTC. sls: [(segundos tras la entrada, ticks de
    riesgo del SL)], negativo = beneficio asegurado; tps: [(segundos, ticks del TP)]; flot: [(segundos, flotante $)];
    cancel: [segundos] de cancelaciones de SL."""
    _op(con, cuenta, simbolo, qty, entrada, salida, t_in, t_out, n, lado)
    sal, d = ('Sell', 1) if lado == 'Buy' else ('Buy', -1)

    def lg(t, s=0):
        return fuentes.fmt_ts(fuentes.parse_ts(t) + timedelta(hours=2, seconds=s))

    for fid, lado_f, px, t in ((f'{n}e', lado, entrada, t_in), (f'{n}s', sal, salida, t_out)):
        con.execute('INSERT INTO fills_log (cuenta, simbolo, fill_id, lado, t_exch, t_log, qty, precio, order_id) '
                    'VALUES (?,?,?,?,?,?,?,?,?)', (cuenta, simbolo, fid, lado_f, None, lg(t), qty, px, None))
    for i, (seg, riesgo) in enumerate(sls):
        con.execute('INSERT INTO ordenes (cuenta, simbolo, ext_id, lado, tipo, limite, stop, etiqueta, t_log, origen) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (cuenta, simbolo, f'SL{n}-{i}', sal, 'Stop', None, entrada - d * riesgo * 0.25, 'SL', lg(t_in, seg),
                     'registered'))
    for i, (seg, ticks) in enumerate(tps):
        con.execute('INSERT INTO ordenes (cuenta, simbolo, ext_id, lado, tipo, limite, stop, etiqueta, t_log, origen) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (cuenta, simbolo, f'TP{n}-{i}', sal, 'Limit', entrada + d * ticks * 0.25, None, 'TP', lg(t_in, seg),
                     'registered'))
    for i, seg in enumerate(cancel):
        con.execute('INSERT INTO cancelaciones (cuenta, simbolo, ext_id, lado, etiqueta, t_log) VALUES (?,?,?,?,?,?)',
                    (cuenta, simbolo, f'SL{n}-{i}', sal, 'SL', lg(t_in, seg)))
    for i, (seg, usd) in enumerate(flot):
        con.execute('INSERT INTO equity (cuenta, t_log, bal, pnl_abierto, pnl_cerrado) VALUES (?,?,?,?,0)',
                    (cuenta, lg(t_in, seg), 25000.0, usd))
    con.commit()


def _una(tmp_path, cuenta=E250, **kw):
    """Entorno con una cuenta y la operación de siempre: 1 NQ largo, +20 ticks, de 08:00 a 08:05 UTC de la sesión del 07/10."""
    ent, con, s = _entorno(tmp_path, [cuenta])
    _base(con, cuenta, 25000.0)
    return ent, con, s


def _op1(con, cuenta=E250, n=1, sls=((1, 999),), d=D2, hms='08:00:00', fin='08:05:00', qty=1, simbolo=NQ, **kw):
    """+20 ticks por contrato en NQ y en MNQ: 5 puntos."""
    _tel(con, cuenta, simbolo, qty, 20000, 20005, _t(d, hms), _t(d, fin), n, sls=sls, **kw)


def _f(res, cuenta=E250, i=0):
    return sorted((f for f in res.filas if f.cuenta == cuenta), key=lambda f: f.t_apertura_utc)[i]


def _ops(con):
    return {r['op_id']: json.loads(r['datos']) for r in con.execute('SELECT op_id, datos FROM operaciones')}


def _celda(s, op_id, col):
    return next(r for r in s.hoja.valores if r and r[0] == op_id)[col]


# ===========================================================================
# Tope por contratos y símbolo
# ===========================================================================

def test_yaml_con_los_valores_de_la_spec():
    T = config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'))['pruebas']['tope_sl']
    assert (T['limite_usd'], str(T['desde']), T['prefijo_nota'], T['beneficio_min_ticks']) == (375, '2026-10-07', 'SL BE', 1)


def test_tope_con_1_y_2_nq_y_1_mnq(tmp_path):
    ent, con, s = _entorno(tmp_path, [E250, AP76, AP77])
    for c in (E250, AP76, AP77):
        _base(con, c, 25000.0)
    _op1(con, E250, 1, hms='08:00:00', fin='08:05:00')                                # 1 NQ
    _op1(con, AP76, 2, hms='08:30:00', fin='08:35:00', qty=2)                         # 2 NQ
    _op1(con, AP77, 3, hms='09:00:00', fin='09:05:00', simbolo=MNQ)                   # 1 MNQ
    res = s.calcular(AHORA)
    assert [(_f(res, c).sl_ini, _f(res, c).sl_max) for c in (E250, AP76, AP77)] == [(75, 75), (37.5, 37.5), (750, 750)]
    assert [_f(res, c).tope_sl for c in (E250, AP76, AP77)] == [75, 37.5, 750]
    assert [(_f(res, c).sl_ini_orden, _f(res, c).sl_max_orden) for c in (E250, AP76, AP77)] == [(999, 999)] * 3


def test_tope_con_3_nq_es_25_y_se_redondea_a_dos_decimales(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, qty=3)
    assert _f(s.calcular(AHORA)).tope_sl == 25
    ent, con, s = _una(tmp_path / 'b')
    _op1(con, qty=7)
    assert _f(s.calcular(AHORA)).tope_sl == 10.71                                    # 375 ÷ 35 = 10,714…


@pytest.mark.parametrize('cuenta', [E250, AP76, BX])
def test_aplica_a_cualquier_prueba_de_cualquier_firma(tmp_path, cuenta):
    ent, con, s = _una(tmp_path, cuenta)
    _op1(con, cuenta)
    assert _f(s.calcular(AHORA), cuenta).sl_ini == 75


def test_el_tope_no_depende_del_neto_del_dia(tmp_path):
    ent, con, s = _una(tmp_path)
    _tel(con, E250, NQ, 1, 20000, 19975, _t(D2, '08:00:00'), _t(D2, '08:05:00'), 1, sls=[(1, 999)])    # −100 t = −500 $
    _op1(con, n=2, hms='09:00:00', fin='09:05:00')
    res = s.calcular(AHORA)
    assert [_f(res, i=i).sl_ini for i in (0, 1)] == [75, 75]


# ===========================================================================
# Todo lo que usa el SL: columna, ratio de la operación, ratio del día, AGUANTANDO PÉRDIDA
# ===========================================================================

def test_sl_999_en_la_columna_el_ratio_de_la_operacion_y_el_ratio_del_dia(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, n=1, sls=[(1, 999)])
    _op1(con, n=2, sls=[(1, 50)], hms='09:00:00', fin='09:05:00')
    res, _ = s.sync(AHORA)
    a, b = _f(res, i=0), _f(res, i=1)
    assert (a.ratio, b.ratio) == pytest.approx((20 / 75, 20 / 50))                  # 20 ticks de resultado, sin TP
    assert _bal(res, E250)['ratio_dia'] == pytest.approx((20 + 20) / ((75 + 50) / 2))
    assert (_celda(s, a.op_id, SL_COL), _celda(s, b.op_id, SL_COL)) == (75, 50)    # la columna muestra el tope, no el 999


def test_ratio_de_la_operacion_usa_el_sl_mas_amplio_ya_topado(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, sls=[(1, 30), (60, 999)])                                              # SL inicial 30, luego 999
    f = _f(s.calcular(AHORA))
    assert (f.sl_ini, f.sl_max, f.ratio) == (30, 75, pytest.approx(20 / 75))
    assert (f.sl_ini_orden, f.sl_max_orden) == (30, 999)


def test_sl_por_debajo_del_tope_no_cambia(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, sls=[(1, 50)])
    f = _f(s.calcular(AHORA))
    assert (f.sl_ini, f.sl_max, f.tope_sl) == (50, 50, 75)


def test_sl_en_beneficio_no_se_toca(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, sls=[(1, -5)])                                                         # primer SL ya en beneficio
    f = _f(s.calcular(AHORA))
    assert (f.sl_ini, f.sl_max) == (-5, -5)
    assert f.ratio == pytest.approx(20 / f.margen_apertura)                          # sin SL en riesgo: denominador = margen
    ent, con, s = _una(tmp_path / 'b')
    _op1(con, sls=[(1, 999), (60, -1), (120, -12)])
    f = _f(s.calcular(AHORA))
    assert (f.sl_ini, f.sl_max, f.sl_hist) == (75, 75, [999, -1, -12])               # el historial de la orden no se toca


def test_aguantando_perdida_con_el_tope_y_la_calificacion(tmp_path):
    """MAE −50 t con SL 999: antes LARGA SIN GESTIÓN (50 < 499,5); con el tope 75, 50 > 37,5. La misma operación en una
    PA sigue como antes."""
    ent, con, s = _entorno(tmp_path, [E250, PA16])
    for c in (E250, PA16):
        _base(con, c, 25000.0)
    flot = [(60, -250.0)]                                                            # −250 $ / 5 $ = −50 t
    _op1(con, E250, 1, sls=[(1, 999)], fin='08:10:00', flot=flot)
    _op1(con, PA16, 2, sls=[(1, 999)], hms='09:00:00', fin='09:10:00', flot=flot)
    res = s.calcular(AHORA)
    p, a = _f(res, E250), _f(res, PA16)
    assert (p.mae, a.mae) == (-50, -50)
    assert (p.etq_duracion, a.etq_duracion) == ('LARGA AGUANTANDO PÉRDIDA', 'LARGA SIN GESTIÓN')
    manual = {f.decision_id: {'estrategia': X} for f in res.filas}
    motor.calificar(res.filas, res.cortafuegos, manual, set(), ent.reglas)
    assert (p.calificacion, a.calificacion) == ('Trade Peligroso', 'Trade Perfecto')


# ===========================================================================
# Ámbito: fecha, tipo de cuenta, sim
# ===========================================================================

def test_frontera_de_fecha_sesion_del_06_sin_tope_y_del_07_con_tope(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, n=1, d=D1, hms='08:00:00', fin='08:05:00')                             # sesión 06/10 a mitad
    _op1(con, n=2, d=D1, hms='21:59:00', fin='21:59:50')                             # 16:59 CT: aún sesión del 06/10
    _op1(con, n=3, d=D1, hms='22:00:00', fin='22:05:00')                             # 17:00 CT: sesión del 07/10
    _op1(con, n=4, d=D2, hms='08:00:00', fin='08:05:00')
    res = s.calcular(AHORA)
    assert [(f.sesion, f.sl_ini) for f in (_f(res, i=i) for i in range(4))] == [
        (D1, 999), (D1, 999), (D2, 75), (D2, 75)]
    assert [f.sl_ini_orden for f in (_f(res, i=i) for i in range(4))] == [None, None, 999, 999]


def test_la_sesion_que_cuenta_es_la_de_la_apertura(tmp_path):
    """Abre a las 21:50 UTC del 06/10 (sesión del 06/10) y cierra a las 22:20 (ya sesión del 07/10): conserva su SL real."""
    ent, con, s = _una(tmp_path)
    _op1(con, d=D1, hms='21:50:00', fin='22:20:00')
    f = _f(s.calcular(AHORA))
    assert (f.sesion, f.sl_ini, f.tope_sl) == (D1, 999, None)


def test_pa_y_sim_activada_sin_cambio(tmp_path):
    ent, con, s = _entorno(tmp_path, [PA16])
    ent.cuentas['cuentas']['DEMO1'] = dict(FICHAS[AP76])
    ent.cuentas['sim_activas'] = ['DEMO1']
    for c in (PA16, 'DEMO1'):
        _base(con, c, 25000.0)
    _op1(con, PA16, 1, sls=[(1, 999), (60, -1)], tps=[(1, 100), (60, 80)])
    _op1(con, 'DEMO1', 2, sls=[(1, 999), (60, -1)], hms='09:00:00', fin='09:05:00')
    res = s.calcular(AHORA)
    pa, sim = _f(res, PA16), _f(res, 'DEMO1')
    assert (pa.sl_ini, pa.sl_max, pa.tope_sl, pa.sl_ini_orden) == (999, 999, None, None)
    assert (sim.sl_ini, sim.sl_max, sim.tope_sl, sim.sl_ini_orden) == (999, 999, None, None)
    assert pa.obs == 'SL 999→-1, TP 100→80 · CORRECTA'                               # la nota de antes, intacta
    assert sim.obs == 'SL 999→-1 · CORRECTA'
    assert pa.ratio == pytest.approx(20 / 999) and _bal(res, PA16)['ratio_dia'] == pytest.approx(20 / 999)


# ===========================================================================
# Seguidoras
# ===========================================================================

def test_seguidora_con_otros_contratos_recibe_el_tope_con_los_suyos(tmp_path):
    """E250 (1 NQ) copiada en AP76 (2 NQ), las dos con el SL 999 de la líder: 75 y 37,5. La nota y el ratio, con su tope."""
    ent, con, s = _entorno(tmp_path, [E250, AP76], pares=[(E250, AP76)])
    for c in (E250, AP76):
        _base(con, c, 25000.0)
    _op1(con, E250, 1, sls=[(1, 999), (60, -3)])
    _op1(con, AP76, 2, qty=2)                                                        # copia por posición: sin órdenes SL
    res = s.calcular(AHORA)
    lid, seg = _f(res, E250), _f(res, AP76)
    assert seg.lider is lid
    assert [(f.sl_ini, f.sl_max, f.tope_sl) for f in (lid, seg)] == [(75, 75, 75), (37.5, 37.5, 37.5)]
    assert [(f.sl_ini_orden, f.sl_max_orden) for f in (lid, seg)] == [(999, 999)] * 2
    assert [f.ratio for f in (lid, seg)] == pytest.approx([20 / 75, 20 / 37.5])
    assert lid.obs == seg.obs == 'SL BE +3 · CORRECTA'
    assert (lid.sin_sl, seg.sin_sl) == (False, False)                                # hereda: ni SIN SL ni cambios en lo demás


def test_seguidora_prueba_de_lider_real_y_seguidora_real_de_lider_prueba(tmp_path):
    ent, con, s = _entorno(tmp_path, [PA16, AP76], pares=[(PA16, AP76)])
    for c in (PA16, AP76):
        _base(con, c, 25000.0)
    _op1(con, PA16, 1, sls=[(1, 100)])
    _op1(con, AP76, 2)
    res = s.calcular(AHORA)
    lid, seg = _f(res, PA16), _f(res, AP76)
    assert seg.lider is lid and (lid.sl_ini, lid.tope_sl) == (100, None)             # la líder real conserva su 100
    assert (seg.sl_ini, seg.sl_ini_orden) == (75, 100)                               # la seguidora prueba, topada
    ent, con, s = _entorno(tmp_path / 'b', [PA15, E250], pares=[(E250, PA15)])
    for c in (PA15, E250):
        _base(con, c, 25000.0)
    _op1(con, E250, 1, sls=[(1, 999)])
    _op1(con, PA15, 2)
    res = s.calcular(AHORA)
    lid, seg = _f(res, E250), _f(res, PA15)
    assert seg.lider is lid and (lid.sl_ini, seg.sl_ini, seg.tope_sl) == (75, 999, None)


# ===========================================================================
# Nota de SL en Observaciones demonio
# ===========================================================================

NOTAS = {
    'secuencia': ([(1, 999), (60, -1), (120, -12), (180, -30)], 'SL BE +1→+12→+30'),
    'un solo nivel': ([(1, 999), (60, -1)], 'SL BE +1'),
    'un solo SL en beneficio': ([(1, -4)], 'SL BE +4'),
    'nunca llega a +1': ([(1, 999), (60, 500)], None),
    'solo el SL de partida': ([(1, 999)], None),
    'el salto −999 → BE no se anota': ([(1, 999), (60, -2)], 'SL BE +2'),
    'BE exacto (0 t) no empieza la nota': ([(1, 999), (60, 0), (120, -3)], 'SL BE +3'),
    'menos de +1 no empieza la nota': ([(1, 999), (60, -0.75)], None),
    'ni el SL en riesgo que sigue a otro en riesgo': ([(1, 999), (60, 500), (120, 100)], None),
    'sin repeticiones consecutivas': ([(1, 999), (60, -1), (120, -1), (180, -5)], 'SL BE +1→+5'),
    'SL en riesgo tras empezar se ignora': ([(1, 999), (60, -1), (120, 40), (180, -8)], 'SL BE +1→+8'),
    'riesgo pequeño de partida': ([(1, 50), (60, -2)], 'SL BE +2'),
    'decimales': ([(1, 999), (60, -1.5), (120, -12.25)], 'SL BE +1.5→+12.25'),
}


@pytest.mark.parametrize('sls, nota', NOTAS.values(), ids=NOTAS.keys())
def test_nota_de_sl_de_pruebas(tmp_path, sls, nota):
    ent, con, s = _una(tmp_path)
    _op1(con, sls=sls)
    f = _f(s.calcular(AHORA))
    assert f.obs == (f'{nota} · CORRECTA' if nota else 'CORRECTA')


def test_la_nota_de_tp_no_cambia_y_va_tras_la_de_sl(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, sls=[(1, 999), (60, -1), (120, -12)], tps=[(1, 100), (60, 80)])
    f = _f(s.calcular(AHORA))
    assert f.obs == 'SL BE +1→+12, TP 100→80 · CORRECTA'
    ent, con, s = _una(tmp_path / 'b')
    _op1(con, sls=[(1, 999)], tps=[(1, 100), (60, 80)])
    assert _f(s.calcular(AHORA)).obs == 'TP 100→80 · CORRECTA'                      # sin SL en beneficio: solo la de TP


def test_operacion_anterior_al_07_conserva_la_nota_de_siempre(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, d=D1, sls=[(1, 999), (60, -1), (120, -12)])
    f = _f(s.calcular(AHORA))
    assert f.obs == 'SL 999→-1→-12 · CORRECTA' and f.tope_sl is None


# ===========================================================================
# SQLite, sheet y nada más cambia
# ===========================================================================

def test_el_sl_de_la_orden_queda_en_sqlite_y_el_topado_en_el_sheet(tmp_path):
    ent, con, s = _entorno(tmp_path, [E250, AP76], pares=[(E250, AP76)])
    for c in (E250, AP76):
        _base(con, c, 25000.0)
    _op1(con, E250, 1, sls=[(1, 999)])
    _op1(con, AP76, 2, qty=2)
    _op1(con, E250, 3, d=D1, hms='08:00:00', fin='08:05:00')                         # anterior al 07/10
    res, _ = s.sync(AHORA)
    lid, seg, vieja = _f(res, E250, 1), _f(res, AP76), _f(res, E250, 0)
    ops = _ops(con)
    assert {k: ops[lid.op_id][k] for k in ('sl_ini', 'sl_max', 'sl_ini_orden', 'sl_max_orden', 'sl_tope')} == {
        'sl_ini': 75, 'sl_max': 75, 'sl_ini_orden': 999, 'sl_max_orden': 999, 'sl_tope': 75}
    assert {k: ops[seg.op_id][k] for k in ('sl_ini', 'sl_ini_orden', 'sl_tope')} == {
        'sl_ini': 37.5, 'sl_ini_orden': 999, 'sl_tope': 37.5}
    assert ops[vieja.op_id]['sl_ini'] == 999 and not {'sl_ini_orden', 'sl_max_orden', 'sl_tope'} & set(ops[vieja.op_id])
    assert _celda(s, lid.op_id, SL_COL) == 75 and _celda(s, vieja.op_id, SL_COL) == 999
    res, _ = s.sync(AHORA)                                                           # idempotente: no se topa dos veces
    assert _ops(con)[lid.op_id]['sl_ini_orden'] == 999 and _celda(s, lid.op_id, SL_COL) == 75


def test_sin_sl_y_sin_telemetria_no_cambian(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, n=1, sls=[])                                                           # telemetría, sin SL
    _op(con, E250, NQ, 1, 20000, 20005, _t(D2, '09:00:00'), _t(D2, '09:05:00'), 2)   # sin telemetría
    res, _ = s.sync(AHORA)
    a, b = _f(res, i=0), _f(res, i=1)
    assert (a.sl_ini, a.tope_sl, a.sin_sl) == (None, None, True) and a.obs.startswith('SIN SL')
    assert a.ratio == pytest.approx(20 / a.margen_apertura)                          # denominador = margen, como en §4.2
    assert (b.sl_ini, b.tope_sl, b.ratio) == (None, None, None) and b.obs.startswith('[SIN TELEMETRÍA]')
    assert not {'sl_ini_orden', 'sl_tope'} & set(_ops(con)[a.op_id])


def test_sl_cancelado_y_cortafuegos_siguen_igual(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, sls=[(1, 999)], cancel=[60])                                           # SL cancelado y nunca repuesto
    res = s.calcular(AHORA)
    f = _f(res)
    assert f.sl_cancelado and f.sl_ini == 75
    assert ent.reglas['observaciones']['sl_cancelado'] in f.obs
    assert ent.reglas['observaciones']['sl_cancelado'] in [t for t, _ in cortafuegos.infracciones_de_entrada(f, ent.reglas)]


# ===========================================================================
# Los valores salen de reglas.yaml
# ===========================================================================

def test_los_valores_se_leen_de_reglas_yaml(tmp_path):
    ent, con, s = _una(tmp_path)
    _op1(con, n=1, sls=[(1, 999), (60, -1), (120, -12)])
    _op1(con, n=2, sls=[(1, 999)], d=D1, hms='08:00:00', fin='08:05:00')
    T = ent.reglas['pruebas']['tope_sl']
    T['limite_usd'], T['prefijo_nota'] = 750, 'SL ASEGURADO'
    res = s.calcular(AHORA)
    vieja, nueva = _f(res, i=0), _f(res, i=1)
    assert (nueva.sl_ini, nueva.obs) == (150, 'SL ASEGURADO +1→+12 · CORRECTA') and vieja.sl_ini == 999
    T['desde'] = '2026-10-06'                                                       # la fecha también es de la regla
    res = s.calcular(AHORA)
    assert [_f(res, i=i).sl_ini for i in (0, 1)] == [150, 150]
    T['desde'] = date(2026, 10, 8)                                                  # como lo lee el yaml: un `date`
    res = s.calcular(AHORA)
    assert [_f(res, i=i).sl_ini for i in (0, 1)] == [999, 999]
    T['desde'] = '2026-10-07'
    T['beneficio_min_ticks'], T['separador_nota'] = 5, ' > '
    res = s.calcular(AHORA)
    assert _f(res, i=1).obs == 'SL ASEGURADO +12 · CORRECTA'                         # −1 no llega a +5; un solo nivel
    T['beneficio_min_ticks'] = 1
    assert _f(s.calcular(AHORA), i=1).obs == 'SL ASEGURADO +1 > +12 · CORRECTA'


def test_ningun_valor_de_negocio_en_el_codigo():
    fuente = open(os.path.join(RAIZ, 'atasv4', 'motor.py'), encoding='utf-8').read()
    assert 'SL BE' not in fuente and '375' not in fuente and "'2026-10-07'" not in fuente


# ===========================================================================
# Huecos D1 de la verificación del 07/10/2026 (mutantes N03 y N04)
# ===========================================================================

def test_escalonada_cada_fila_con_sus_contratos(tmp_path):
    """Entrada escalonada: 1 NQ y después 2 NQ con la primera abierta (posición máxima 3). El tope es por fila, con sus
    contratos (75 y 37,5), no con la posición máxima de la cuenta (que daría 25 en las dos)."""
    ent, con, s = _una(tmp_path)
    _tel(con, E250, NQ, 1, 20000, 20005, _t(D2, '08:00:00'), _t(D2, '08:05:00'), 1, sls=[(1, 999)])
    _tel(con, E250, NQ, 2, 20002, 20006, _t(D2, '08:01:00'), _t(D2, '08:06:00'), 2, sls=[(1, 999)])
    res = s.calcular(AHORA)
    assert [(_f(res, i=i).contratos, _f(res, i=i).tope_sl, _f(res, i=i).sl_ini) for i in (0, 1)] == \
        [(1, 75, 75), (2, 37.5, 37.5)]


def test_prueba_quemada_sigue_con_el_tope(tmp_path):
    """Una prueba quemada conserva sus reglas en las operaciones que ya tiene (§9.1): el tope sigue aplicándose."""
    from atasv4 import cuentas
    ent, con, s = _una(tmp_path)
    _op1(con)
    cuentas.quemar(ent, con, E250)
    f = _f(s.calcular(AHORA))
    assert (f.tope_sl, f.sl_ini, f.sl_ini_orden) == (75, 75, 999)
