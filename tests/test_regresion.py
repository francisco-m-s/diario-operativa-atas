"""Regresiones de la verificación independiente del 22/09/2026 (un test por punto, datos reales de fixtures)."""

import sys

import pytest

from atasv4 import almacen, cuentas, fuentes, motor, vista
from conftest import RAIZ, filas_por

sys.path.insert(0, RAIZ)
import demonio_v4  # noqa: E402

AHORA = '2026-09-22 12:00:00.000'
PA16, PA15, SIM = 'PA-APEX-91120-16', 'PA-APEX-91120-15', 'fsern2776_sim'
E249 = 'fsern2776tcp25d152249'
APEX25 = {'drawdown': 1000, 'tope': 25100}


def _bal(s, cuenta, t=AHORA):
    return {b['cuenta']: b for b in s.calcular(t).bal}[cuenta]


def _borrar_fill(con, cuenta, fill_id, log=True):
    con.execute('DELETE FROM fills WHERE cuenta=? AND fill_id=?', (cuenta, fill_id))
    if log:
        con.execute('DELETE FROM fills_log WHERE cuenta=? AND fill_id=?', (cuenta, fill_id))
    con.commit()


# --- 1 (ALTO): el 'Bal:' del log ya es equity; el pico no suma el PnL abierto -------------------

def test_1_pico_es_maximo_bal_del_log(entorno):
    ent, con, s = entorno
    linea = ('2026-09-21 18:10:08,014 [Process   ] INFO  mic[APEX-91120]: Portfolio changed PA-APEX-91120-16 '
             'Bal:24856,66/0/0 Avl:0 PnL:75,500/70,5 L:1 C:0.')
    tipo, d = fuentes.parse_linea(linea)
    assert (tipo, d['bal'], d['pnl_abierto']) == ('equity', 24856.66, 75.5)   # PnL abierto viejo tras el cierre
    assert con.execute('SELECT COUNT(*) FROM equity WHERE cuenta=? AND t_log=? AND bal=24856.66 '
                       'AND pnl_abierto=75.5', (PA16, '2026-09-21 18:10:08.014')).fetchone()[0] == 1
    # PA-15: máximo Bal 26093,06 (18:10:05) -> threshold 25093,06, el ancla real. Antes: 26158,53 -> 25100.
    assert _bal(s, PA15)['threshold'] == 25093.06
    max16 = con.execute('SELECT MAX(bal) FROM equity WHERE cuenta=?', (PA16,)).fetchone()[0]
    assert _bal(s, PA16)['threshold'] == round(max16 - 1000, 2)


# --- 2 (MEDIO): plano = sin posición por fills, no 'PnL abierto = 0' ----------------------------

def test_2_plano_por_posicion_no_por_pnl_cero(entorno):
    ent, con, s = entorno
    # 18:06:45,576 'Bal:24786,69 … PnL:0/0' llega con la posición ya abierta (fill a las 18:06:45,566)
    # (antes devolvía ese 24786,69; ahora la última muestra plana anterior es la del 16/09 tras su cierre)
    t, bal = almacen.ultimo_bal_plano(con, PA16, hasta='2026-09-21 18:07:00.000')
    assert (t, bal) == ('2026-09-16 17:02:53.756', 24754.72)
    assert almacen.ultimo_bal_plano(con, PA16) == ('2026-09-21 18:10:08.014', 24856.66)
    # dos muestras en el mismo ms: gana la última del fichero (balance ya neto de comisión)
    assert almacen.ultimo_bal_plano(con, PA15) == ('2026-09-21 18:10:08.162', 26083.04)
    s.calcular(AHORA)
    base = con.execute('SELECT bal_base FROM cuentas_estado WHERE cuenta=?', (PA15,)).fetchone()
    assert base['bal_base'] == 26083.04
    assert 'DESCUADRE' not in _bal(s, PA15)['obs']


# --- 3 (MEDIO): tamaño de Apex reintentado hasta que haya Bal: plano ----------------------------

def test_3_tamano_se_reintenta(entorno):
    ent, con, s = entorno
    del ent.cuentas['cuentas'][PA15]
    con.execute('DELETE FROM equity WHERE cuenta=?', (PA15,))
    con.commit()
    avisos = []
    cuentas.registrar_nuevas(ent, con, motor.sesion_cme(AHORA), avisos)
    assert ent.ficha(PA15)['tamano'] is None and ent.ficha(PA15)['sin_regla']
    con.execute('DELETE FROM log_offsets')                   # vuelve a llegar el log con sus Bal:
    s.ingerir()
    assert ent.ficha(PA15)['tamano'] == 25000 and not ent.ficha(PA15)['sin_regla']


# --- 4 (MEDIO): fills ausentes en MyTrade.cdb ---------------------------------------------------

def test_4a_fill_solo_en_log(entorno):
    ent, con, s = entorno
    assert con.execute("SELECT order_id FROM fills_log WHERE fill_id='1132174'").fetchone()[0] == '2900494968'
    _borrar_fill(con, PA16, '1132174', log=False)                  # salida de 2 contratos, solo en el log
    fs = [f for f in filas_por(s.calcular(AHORA), PA16) if f.sesion.day == 21]
    assert [(f.ticks, f.neto) for f in fs] == [(102, 49.98), (39, 18.48)]
    assert all(f.tipo_cierre == 'SL' and not f.incompleta for f in fs)


def test_4b_salida_ausente_se_toma_de_historymytrade(entorno):
    ent, con, s = entorno
    _borrar_fill(con, SIM, '1006776')                              # salida 11:27:49 ni en cdb ni en log
    a, b = filas_por(s.calcular(AHORA), SIM)
    assert a.rescatada and not a.incompleta and (a.ticks, a.neto) == (-12, -60.0)
    assert 'SALIDA TOMADA DE HistoryMyTrade' in a.obs
    assert b.entrada == 30835.75 and not b.incompleta              # la siguiente operación no se contamina


def test_4c_incompleta_se_escribe_marcada(entorno):
    ent, con, s = entorno
    _borrar_fill(con, SIM, '1006776')
    con.execute("DELETE FROM patas WHERE cuenta=? AND open_utc LIKE '2026-09-22 09:27:45%'", (SIM,))
    con.commit()
    a = filas_por(s.calcular(AHORA), SIM)[0]
    assert a.incompleta and a.faltan == 1 and 'OPERACIÓN INCOMPLETA' in a.obs


def test_4d_cola_abierta_espera_n_minutos(entorno):
    ent, con, s = entorno
    _borrar_fill(con, SIM, '1006945')                              # última salida (12:35:59 Madrid)
    con.execute("DELETE FROM patas WHERE cuenta=? AND open_utc LIKE '2026-09-22 10:03:02%'", (SIM,))
    con.commit()
    # ATAS dio la cuenta por plana a las 12:35:58,954 Madrid (10:35:58 UTC)
    assert len(filas_por(s.calcular('2026-09-22 10:38:00.000'), SIM)) == 1      # 2 min: aún no
    fs = filas_por(s.calcular('2026-09-22 10:42:00.000'), SIM)                   # > 5 min
    assert len(fs) == 2 and fs[1].incompleta and fs[1].faltan == 1


def test_4e_pendiente_evalua_plano_por_simbolo(entorno):
    ent, con, s = entorno
    s.sync(AHORA)
    assert not s.pendiente(AHORA)
    # PA-16 con +2 MNQ abiertos y −2 NQ: la suma global es 0 pero la cuenta NO está plana
    _borrar_fill(con, PA16, '1132174')
    con.execute("DELETE FROM posiciones_hist WHERE cuenta=? AND t_log>='2026-09-21 18:10:00'", (PA16,))
    con.execute("INSERT INTO fills VALUES (?, 'NQZ6@CME', 'x1', 'x1', 'Sell', 30000, 2, '2026-09-21 16:12:00.000')",
                (PA16,))
    con.commit()
    pos, _ = motor.posiciones_cuenta(ent, con, PA16, AHORA)
    assert pos == {'MNQZ6@CME': 2.0, 'NQZ6@CME': -2.0}
    assert not s.pendiente(AHORA)


# --- 5 (BAJO): el threshold nunca baja, tampoco desde un manual por encima del tope --------------

def test_5_threshold_no_baja_tras_manual_sobre_tope(entorno):
    assert motor.threshold(None, 25200, 26500, APEX25) == 25200
    ent, con, s = entorno
    s.sync(AHORA)
    i = next(k for k, r in enumerate(s.hoja.valores) if r[0] == PA15)
    s.hoja.valores[i][vista.BC['Threshold Rithmic (manual)']] = 25200
    s.sync(AHORA)
    i = next(k for k, r in enumerate(s.hoja.valores) if r[0] == PA15)
    assert s.hoja.valores[i][vista.BC['Threshold']] == 25200
    s.hoja.valores[i][vista.BC['Threshold Rithmic (manual)']] = ''
    s.sync(AHORA)
    i = next(k for k, r in enumerate(s.hoja.valores) if r[0] == PA15)
    assert s.hoja.valores[i][vista.BC['Threshold']] == 25200


# --- 6 (BAJO): cálculo antes de leer el sheet; manuales por nombre de encabezado ----------------

def test_6a_calculo_antes_de_leer_manuales(entorno):
    ent, con, s = entorno
    orden = []
    calc, leer = s.calcular, s.hoja.leer
    s.calcular = lambda *a, **k: (orden.append('calcular'), calc(*a, **k))[1]
    s.hoja.leer = lambda: (orden.append('leer'), leer())[1]
    s.sync(AHORA)
    assert orden[:2] == ['calcular', 'leer']


def test_6b_manuales_por_nombre_de_encabezado(entorno):
    ent, con, s = entorno
    s.sync(AHORA)
    v = s.hoja.valores
    h = next(k for k, r in enumerate(v) if r and r[0] == 'ID')
    ie, io = vista.OC['Estrategia'], vista.OC['Observaciones']
    for r in v[h:]:                                   # el operador intercambia dos columnas de sitio
        r[ie], r[io] = r[io], r[ie]
    fila = next(r for r in v[h + 1:] if r[0] == 'PA16-20260921-001')
    fila[ie], fila[io] = 'nota', 'Lunes'              # ahora Estrategia está en la columna de Observaciones
    manual, _, ok = vista.parsear_hoja(v)
    assert ok and manual['PA16-20260921-001']['estrategia'] == 'Lunes'
    assert manual['PA16-20260921-001']['observaciones'] == 'nota'


# --- 7 (BAJO): tras un retiro el threshold manual se empareja por cuenta interna ----------------

def test_7_threshold_manual_sobrevive_al_retiro(entorno):
    ent, con, s = entorno
    s.sync(AHORA)
    i = next(k for k, r in enumerate(s.hoja.valores) if r[0] == PA16)
    s.hoja.valores[i][vista.BC['Threshold Rithmic (manual)']] = 24100
    s.sync(AHORA)
    cuentas.retiro(ent, con, PA16, 100)
    s.sync(AHORA)                                      # el sheet aún tiene la fila con el nombre antiguo
    assert s.thresholds_manuales()[PA16] == 24100
    nombres = [r[0] for r in s.hoja.valores]
    assert 'PA-APEX-91120-16_retiros_1' in nombres
    s.sync(AHORA)                                      # y ahora con el nombre nuevo
    assert s.thresholds_manuales()[PA16] == 24100


# --- 8 (BAJO): un fallo en el ciclo no tumba el demonio -----------------------------------------

def test_8_ciclo_sobrevive_a_excepciones(entorno):
    ent, con, s = entorno
    logs = []

    class Roto:
        def ingerir(self):
            raise OSError('ATAS reescribiendo MyTrade.cdb')

    c = demonio_v4.Ciclo(ent, con, Roto(), logs.append)
    c.paso()
    assert logs and logs[0].startswith('[CICLO ERROR] OSError')
    s.pendiente = lambda: (_ for _ in ()).throw(RuntimeError('fallo'))
    c = demonio_v4.Ciclo(ent, con, s, logs.append)
    c.paso()
    assert logs[-1].startswith('[CICLO ERROR] RuntimeError')


# --- 9 (BAJO): símbolo sin regla no es falta de telemetría --------------------------------------

def test_9_simbolo_sin_regla_no_es_sin_telemetria(entorno):
    ent, con, s = entorno
    del ent.reglas['simbolos']['MNQ']
    fs = [f for f in filas_por(s.calcular(AHORA), PA16) if f.sesion.day == 21]
    for f in fs:
        assert f.telemetria and 'SÍMBOLO SIN REGLA' in f.obs
        assert '[SIN TELEMETRÍA]' not in f.obs and 'SIN SL' not in f.obs and f.ratio is None
    assert [f.ticks for f in fs] == [102, 39]                     # de HistoryMyTrade
    assert fs[0].tipo_cierre == 'SL'


# --- 10 (BAJO): ANCLA ESTIMADA solo se quita si el manual ≥ calculado ----------------------------

def test_10_ancla_estimada_se_mantiene_con_manual_menor(entorno):
    ent, con, s = entorno
    s.sync(AHORA)
    i = next(k for k, r in enumerate(s.hoja.valores) if r[0] == PA16)
    calculado = s.hoja.valores[i][vista.BC['Threshold']]
    s.hoja.valores[i][vista.BC['Threshold Rithmic (manual)']] = calculado - 50
    s.sync(AHORA)
    i = next(k for k, r in enumerate(s.hoja.valores) if r[0] == PA16)
    assert s.hoja.valores[i][vista.BC['Threshold']] == calculado
    assert 'ANCLA ESTIMADA' in s.hoja.valores[i][vista.BC['Observaciones demonio']]
    s.hoja.valores[i][vista.BC['Threshold Rithmic (manual)']] = calculado + 50
    s.sync(AHORA)
    i = next(k for k, r in enumerate(s.hoja.valores) if r[0] == PA16)
    assert 'ANCLA ESTIMADA' not in s.hoja.valores[i][vista.BC['Observaciones demonio']]


# --- 11 (decisión del operador): el nombre visible se congela por operación ---------------------

def test_11_nombre_por_operacion_segun_retiros_previos(entorno):
    ent, con, s = entorno
    # retiro con la cuenta real (hora actual): todas las operaciones del fixture son anteriores
    cuentas.retiro(ent, con, PA16, 100)
    res, lay = s.sync(AHORA)
    assert {f.nombre for f in res.filas if f.cuenta == PA16} == {PA16}
    assert next(b for b in res.bal if b['cuenta'] == PA16)['nombre'] == 'PA-APEX-91120-16_retiros_1'
    # un segundo retiro registrado entre las dos entradas escalonadas del 21/09 (18:06:41 y 18:07:14 Madrid)
    con.execute("UPDATE movimientos SET t='2026-09-21 18:07:00.000' WHERE cuenta=?", (PA16,))
    con.execute("INSERT INTO movimientos (cuenta, t, importe, tipo, nota) "
                "VALUES (?, '2026-09-21 18:07:10.000', -50, 'retiro', '')", (PA16,))
    con.commit()
    ent.ficha(PA16)['retiros'] = 2
    res, lay = s.sync(AHORA)
    nombres = {f.op_id: f.nombre for f in res.filas if f.cuenta == PA16}
    assert nombres == {'PA16-20260916-001': PA16, 'PA16-20260921-001': PA16,
                       'PA16-20260921-002': 'PA-APEX-91120-16_retiros_2'}
    filas_hoja = {r[0]: r[vista.OC['Cuenta']] for r in lay['valores'][lay['ops_ini']:lay['ops_fin']]}
    assert filas_hoja['PA16-20260921-001'] == PA16
    assert filas_hoja['PA16-20260921-002'] == 'PA-APEX-91120-16_retiros_2'
    seg = next(f for f in res.filas if f.cuenta == PA15 and f.lider and f.lider.op_id == 'PA16-20260921-002')
    assert seg.nombre == PA15                                    # la seguidora no tiene retiros


# --- 12 (decisión del operador): MAE/MFE editables a mano -----------------------------------------

MAE_C, MFE_C = vista.FLOTANTES['mae'], vista.FLOTANTES['mfe']


def _celda_op(s, op_id, col):
    v = s.hoja.valores
    i = next(k for k, r in enumerate(v) if r and r[0] == op_id)
    return i, vista.OC[col]


def _editar(s, op_id, col, valor):
    i, c = _celda_op(s, op_id, col)
    s.hoja.valores[i][c] = valor


def _fila(res, op_id):
    return next(f for f in res.filas if f.op_id == op_id)


def test_12a_parseo_coma_y_signo():
    p = vista.parse_flotante
    assert [p('−161,5'), p('1.234,5'), p('+42'), p(42), p(' -3 '), p(''), p('abc'), p(None)] == \
        [-161.5, 1234.5, 42.0, 42.0, -3.0, None, None, None]


def test_12b_deteccion_nota_color_y_vuelta_al_log(entorno):
    ent, con, s = entorno
    s.sync(AHORA)
    _editar(s, 'SIM-20260922-002', MAE_C, '200,5')          # sin signo y con coma: se normaliza a −200,5
    _editar(s, 'SIM-20260922-002', MFE_C, '-50')            # MFE siempre positivo
    res, lay = s.sync(AHORA)
    f = _fila(res, 'SIM-20260922-002')
    assert (f.mae, f.mae_log, f.mfe, f.mfe_log) == (-200.5, -161.0, 50.0, 42.0)
    assert 'MAE manual (log −161)' in f.obs and 'MFE manual (log +42)' in f.obs
    i, c = _celda_op(s, 'SIM-20260922-002', MAE_C)
    assert (i, c) in lay['celdas_manuales'] and (i, vista.OC[MFE_C]) in lay['celdas_manuales']
    assert s.hoja.valores[i][c] == -200.5
    res, lay = s.sync(AHORA)                                  # sin tocar: el manual se mantiene
    assert _fila(res, 'SIM-20260922-002').mae == -200.5
    _editar(s, 'SIM-20260922-002', MAE_C, '')                 # vaciada -> vuelve al log
    res, lay = s.sync(AHORA)
    f = _fila(res, 'SIM-20260922-002')
    assert f.mae == -161.0 and not f.mae_manual and f.mfe == 50.0
    assert 'MAE manual' not in f.obs
    i, c = _celda_op(s, 'SIM-20260922-002', MAE_C)
    assert (i, c) not in lay['celdas_manuales'] and s.hoja.valores[i][c] == -161.0
    # el valor del log nunca se ha borrado de SQLite (equity intacto)
    assert con.execute("SELECT COUNT(*) FROM equity WHERE cuenta=?", (SIM,)).fetchone()[0] > 0


def test_12c_recalculo_en_cadena_y_seguidoras(entorno):
    ent, con, s = entorno
    res, _ = s.sync(AHORA)
    b16 = {b['cuenta']: b for b in res.bal}[PA16]
    # 30/09/2026 (spec §8.2): en las Apex PA la columna da el límite de cada símbolo (antes, un solo número: 3)
    # (07/10/2026, §10.1: la columna es el estado actual, NQ 0 en las reales; las operaciones de este test siguen con las de antes)
    assert (b16['margen'], b16['permitidos']) == (821.4, '0 NQ / 3 MNQ')
    _editar(s, 'PA16-20260921-001', MAE_C, -60)
    res, lay = s.sync(AHORA)
    lider = _fila(res, 'PA16-20260921-001')
    seg = next(f for f in res.filas if f.lider is lider)
    assert (lider.mae, seg.mae, seg.mae_manual) == (-60, -60, True)       # también en la seguidora
    # PA-16 21/09: 102×0,2−60 + 39×0,2−0 = −31,8 < 0 -> sin +140 el 22/09: 673,2 − 31,8 = 641,4 -> 2 contratos
    b = {x['cuenta']: x for x in res.bal}
    assert b[PA16]['margen'] == pytest.approx(641.4) and b[PA16]['permitidos'] == '0 NQ / 2 MNQ'
    # PA-15 (seguidora): 102×0,2−60 + 52×0,2 = −29,2 -> sin bono: 920 − 29,2
    assert b[PA15]['margen'] == pytest.approx(890.8)
    assert lider.margen_tras == pytest.approx(641.4)            # ambas filas cierran con el mismo fill


def test_12d_sin_telemetria_con_mae_manual_consume_margen(entorno):
    ent, con, s = entorno
    con.execute("DELETE FROM fills_log WHERE cuenta=? AND fill_id='1478215'", (PA16,))
    con.commit()
    s.sync(AHORA)
    _editar(s, 'PA16-20260916-001', MAE_C, '-100')
    res, _ = s.sync(AHORA)
    f = _fila(res, 'PA16-20260916-001')
    assert not f.telemetria and f.mae == -100 and 'MAE manual (log sin dato)' in f.obs
    assert f.margen_tras == pytest.approx(500 - 89 * 0.2 - 100)


def test_12e_sin_registro_de_lo_escrito_no_hay_falso_manual(entorno):
    ent, con, s = entorno
    s.sync(AHORA)
    con.execute('DELETE FROM escrito_ops')
    con.commit()
    _editar(s, 'SIM-20260922-002', MAE_C, -999)
    res, _ = s.sync(AHORA)
    assert not _fila(res, 'SIM-20260922-002').mae_manual


# --- 13 (decisión del operador): Tipo cierre = Mercado | Límite | TP | SL ------------------------

def test_13_tipo_cierre_por_orden_del_fill_de_maxima_amplitud(entorno):
    ent, con, s = entorno
    res = s.calcular(AHORA)
    pa16 = [f for f in filas_por(res, PA16) if f.sesion.day == 21]
    assert [f.tipo_cierre for f in pa16] == ['SL', 'SL']        # Stop etiquetado SL movido a beneficio
    e249 = filas_por(res, E249)
    assert e249[2].tipo_cierre == 'TP' and e249[2].tp_llenado      # Buy Limit TP 16/09 16:13:14 Madrid
    assert e249[2].ratio == pytest.approx(87 / 214)                 # TP llenado: TP final / SL más amplio (85→140→214)
    bx = filas_por(res, 'BX63983-50')
    assert [(f.tipo_apertura, f.tipo_cierre) for f in bx] == [('Stop', 'Límite'), ('Stop', 'Mercado')]
    assert filas_por(res, PA15)[0].tipo_cierre == 'Mercado'          # seguidora: salida a mercado
    # reglas puras: cualquier Stop sin etiqueta es SL; orden desconocida -> vacío
    R = ent.reglas
    o = lambda tipo, etq='': {'tipo': tipo, 'etiqueta': etq}
    assert [motor.tipo_cierre(x, R) for x in (o('Stop'), o('StopLimit'), o('Market', 'SL'), o('Limit', 'TP'),
                                              o('Limit'), o('Market'), o('Raro'), None)] == \
        ['SL', 'SL', 'SL', 'TP', 'Límite', 'Mercado', '', '']
    assert 'Stop' not in {f.tipo_cierre for f in res.filas}
