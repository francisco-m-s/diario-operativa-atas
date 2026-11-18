"""Tests V4 con fixtures extraídos de datos reales (tests/extraer_fixtures.py).

Casos reales:
  - Entrada escalonada + copia PA-16 -> PA-15, 21/09 16:06:41 UTC (18:06 Madrid).
  - Orden de 2 contratos PA-16, 16/09 15:00:14 UTC.
  - Giro fsern…249, 16/09 14:02 UTC (16:02 Madrid): largo 1 -> venta de 3 -> corto 2 con dos salidas.
  - SL cancelado con posición abierta: fsern2776_sim 22/09 10:03 UTC (12:03 Madrid), estrategia ATM detenida.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from atasv4 import almacen, cuentas, fuentes, motor, vista
from conftest import filas_por

AHORA = '2026-09-22 12:00:00.000'          # 14:00 Madrid, sesión CME 22/09
PA16, PA15 = 'PA-APEX-91120-16', 'PA-APEX-91120-15'
E249, SIM = 'fsern2776tcp25d152249', 'fsern2776_sim'


def R_(entorno):
    return entorno[0].reglas


@pytest.fixture
def res(entorno):
    ent, con, s = entorno
    return s.calcular(AHORA)


# ---------------------------------------------------------------------------
# Hora y sesión
# ---------------------------------------------------------------------------

def test_conversion_hora_utc_madrid():
    # horario de invierno (CET, +1) y de verano (CEST, +2): zoneinfo, nada de +2h fijo
    assert fuentes.utc_a_madrid('2026-03-16 10:22:40.000') == datetime(2026, 3, 16, 11, 22, 40)
    assert fuentes.utc_a_madrid('2026-09-21 16:06:41.651') == datetime(2026, 9, 21, 18, 6, 41, 651000)
    assert fuentes.cdb_utc('09/21/2026 16:06:41') == '2026-09-21 16:06:41.000'


def test_sesion_cme_cambia_a_las_17_chicago():
    assert motor.sesion_cme('2026-09-21 21:59:59.999') == date(2026, 9, 21)
    assert motor.sesion_cme('2026-09-21 22:00:00.000') == date(2026, 9, 22)     # 17:00 CDT
    assert motor.sesion_cme('2026-03-08 21:59:59.000') == date(2026, 3, 8)      # domingo, ya en CDT
    assert motor.sesion_cme('2026-03-08 22:00:00.000') == date(2026, 3, 9)
    assert motor.sesion_cme('2026-01-12 22:30:00.000') == date(2026, 1, 12)     # CST: cambio a las 23:00 UTC
    assert motor.sesion_cme('2026-01-12 23:00:00.000') == date(2026, 1, 13)
    assert almacen.inicio_madrid(date(2026, 9, 22)) == '2026-09-22 00:00:00.000'


def test_linea_fill_del_log():
    line = ('2026-09-22 11:27:45,675 [Process   ] DEBUG tchingProcessor: fsern2776_sim NQZ6@CME. '
            'Processing trade 1006795 Buy 2026.09.22 09:27:45.636 1x30768,25')
    tipo, d = fuentes.parse_linea(line)
    assert tipo == 'fill'
    assert d['fill_id'] == '1006795' and d['precio'] == 30768.25 and d['qty'] == 1
    assert d['t_exch'] == '2026-09-22 09:27:45.636' and d['t_log'] == '2026-09-22 11:27:45.675'


def test_linea_orden_tif_day_doble_espacio():
    line = ('2026-09-22 12:35:58,952 [Process   ] INFO  hmic[fsern2776]: Order registered 2635334009/2635334009 '
            'Sell Market NQZ6@CME on fsern2776_sim at 0/0 vol 1/1 tif DAY  state Active .')
    tipo, d = fuentes.parse_linea(line)
    assert tipo == 'orden' and d['tipo'] == 'Market' and d['etiqueta'] == ''


def test_playbook_y_copytrading(entorno):
    ent = entorno[0]
    pb = fuentes.leer_playbook(ent.db_dir)
    assert pb[0] == 'Continuación de M204s confirmada por M17' and 'Ruptura de estructura M208' in pb
    assert (PA16, PA15) in fuentes.leer_copytrading(ent.atas_dir)


# ---------------------------------------------------------------------------
# Construcción de operaciones
# ---------------------------------------------------------------------------

def test_entrada_escalonada(res):
    fs = [f for f in filas_por(res, PA16) if f.sesion == date(2026, 9, 21)]
    assert len(fs) == 2                                   # cada entrada que aumenta la posición = una fila
    a, b = fs
    assert (a.entrada, a.contratos, a.ticks, a.neto) == (30625.75, 1, 102, 49.98)
    assert (b.entrada, b.contratos, b.ticks, b.neto) == (30641.5, 1, 39, 18.48)
    assert a.comision == 1.02                            # Apex MNQ 0,51 por lado
    # 01/10/2026 (spec §4.2): la amplitud es |MAE| + |MFE| en ticks; el fill de salida más alejado queda interno (Tipo cierre)
    assert motor._salida_max_amplitud(a)['precio'] == motor._salida_max_amplitud(b)['precio'] == 30651.25
    assert (a.mae, a.mfe, a.max_amplitud) == (-20, 157, 177) and (b.mae, b.mfe, b.max_amplitud) == (0, 157, 157)
    assert (a.tipo_apertura, a.tipo_cierre) == ('Mercado', 'SL')
    assert (a.sl_ini, a.sl_max, a.tp_final) == (88, 88, 174)
    assert (b.sl_ini, b.sl_max, b.tp_final) == (120, 120, 111)
    assert 'SL 88→57→-95→-102, TP 143→174' in a.obs      # modificaciones en vivo (cancelación + alta)
    assert not a.sl_cancelado and not b.sl_cancelado     # SL repuesto en < 5 s = modificación
    assert a.balance_tras == b.balance_tras
    assert a.telemetria and a.mae is not None


def test_orden_de_2_contratos(res):
    (f,) = [f for f in filas_por(res, PA16) if f.sesion == date(2026, 9, 16)]
    assert f.contratos == 2 and f.ticks == -89 and f.bruto == -89.0
    assert f.comision == 2.04 and f.neto == -91.04
    assert (f.tipo_apertura, f.tipo_cierre, motor._salida_max_amplitud(f)['precio']) == ('Mercado', 'SL', 29486)
    assert (f.mae, f.mfe, f.max_amplitud) == (-89, 20, 109)          # amplitud en ticks desde el 01/10/2026
    assert f.duracion == 160


def test_giro_son_dos_operaciones(res):
    a, b = filas_por(res, E249)[:2]
    assert (a.direccion, a.contratos, a.ticks, a.neto) == (1, 1, -63, -319.08)       # E2T 2,04 por lado
    assert (b.direccion, b.contratos, b.ticks, b.neto) == (-1, 2, 33, 321.84)
    assert a.t_cierre_utc == b.t_apertura_utc                                         # mismo fill
    assert a.tipo_apertura == 'Límite' and a.tipo_cierre == 'Mercado'
    # salida más alejada (29429, a mercado); la última es el SL: el Tipo cierre sigue saliendo de la más alejada
    assert motor._salida_max_amplitud(b)['precio'] == 29429 and b.tipo_cierre == 'Mercado'
    assert (b.mae, b.mfe, b.max_amplitud) == (-31.5, 65, 96.5)       # amplitud en ticks desde el 01/10/2026
    assert 'SL 85→-1→-15, TP 87→76→87' in b.obs
    # margen en la apertura del corto: 500 − (−63×0,2 + 63) = 424,4. Hasta el 30/09/2026 daba 1 contrato permitido y
    # abrió 2. E249 es una prueba (spec §9.2–§9.3): el margen informa pero no limita, y 2 NQ son 2 NQ-equivalentes,
    # justo el máximo: no hay infracción
    assert b.margen_apertura == 424.4 and (b.neq, b.neq_max, b.permitidos) == (2, 2, None)
    assert 'CONTRATOS > PERMITIDOS' not in b.obs


def test_copia_pa16_a_pa15(res):
    lideres = [f for f in filas_por(res, PA16) if f.sesion == date(2026, 9, 21)]
    seg = filas_por(res, PA15)
    assert len(seg) == 2
    assert [s.lider for s in seg] == lideres
    assert [c.cuenta for c in lideres[0].copias] == [PA15]
    # la seguidora mueve su balance con su propio precio (deslizamiento en la 2ª entrada)
    assert (seg[0].entrada, seg[0].ticks, seg[0].neto) == (30625.75, 102, 49.98)
    assert (seg[1].entrada, seg[1].ticks, seg[1].neto) == (30638.25, 52, 24.98)
    assert seg[0].sl_ini == 88 and seg[1].sl_ini == 120                  # SL heredado de la líder


def test_sl_cancelado_con_posicion_abierta(res):
    a, b = filas_por(res, SIM)
    assert not a.sl_cancelado                              # SL/TP cancelados después del cierre
    assert b.sl_cancelado and 'SL CANCELADO CON POSICIÓN ABIERTA' in b.obs
    assert (b.sl_ini, b.sl_max, b.tp_final, b.tp_llenado) == (120, 120, 88, False)
    assert b.ticks == -148 and b.duracion == 1977
    assert 'LARGA AGUANTANDO PÉRDIDA' in b.obs             # |MAE| > 50 % del SL
    assert b.ratio == pytest.approx(88 / 120)              # pérdida con TP: TP final / SL más amplio
    assert 'MICRO-RUIDO' in a.obs and a.obs.endswith('PÉRDIDA: NO OPERAR HASTA 11:38:01')


def test_ingesta_idempotente(entorno):
    ent, con, s = entorno
    cuenta = lambda t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    antes = {t: cuenta(t) for t in ('fills', 'fills_log', 'patas', 'ordenes', 'cancelaciones', 'equity')}
    con.execute('DELETE FROM log_offsets')
    con.execute('DELETE FROM cdb_mtime')
    s.ingerir(forzar_cdb=True)
    assert {t: cuenta(t) for t in antes} == antes
    assert antes['fills'] == 22 and antes['patas'] == 13


def test_ids_estables(entorno):
    ent, con, s = entorno
    r1 = s.calcular(AHORA)
    r2 = s.calcular(AHORA)
    assert sorted(f.op_id for f in r1.filas) == sorted(f.op_id for f in r2.filas)
    ids = {f.op_id for f in r1.filas}
    assert {'PA16-20260921-001', 'PA16-20260921-002', 'PA16-20260916-001', 'E2T249-20260916-002'} <= ids


# ---------------------------------------------------------------------------
# Reglas
# ---------------------------------------------------------------------------

def _fila(sesion, t_ap, t_ci, consumo, neto=0.0):
    return SimpleNamespace(sesion=sesion, t_apertura_utc=t_ap, t_cierre_utc=t_ci, consumo=consumo, neto=neto)


def test_margen_bono_habil_festivo_y_fin_de_semana(entorno):
    R = R_(entorno)
    # Alta viernes 18/09 con consumo −10: lunes 21 sin bono; lunes sin operaciones -> martes 22 +140
    f = _fila(date(2026, 9, 18), '2026-09-18 14:00:00.000', '2026-09-18 14:05:00.000', -10)
    en, actual, _ = motor.margen_timeline([f], date(2026, 9, 18), date(2026, 9, 22), R)
    assert actual == 500 - 10 + 140
    assert en('2026-09-21 12:00:00.000') == 490                  # sábado y domingo no suman
    # Festivo CME (Acción de Gracias, jueves 26/11) cuenta como hábil "no se operó"
    en, actual, _ = motor.margen_timeline([], date(2026, 11, 25), date(2026, 11, 30), R)
    assert actual == 500 + 140 * 3                                # jue 26, vie 27, lun 30
    # Consumo ≥ 0 la sesión anterior -> bono
    g = _fila(date(2026, 9, 21), '2026-09-21 14:00:00.000', '2026-09-21 14:05:00.000', 0.4)
    _, actual, _ = motor.margen_timeline([g], date(2026, 9, 21), date(2026, 9, 22), R)
    assert actual == pytest.approx(500 + 0.4 + 140)


def test_margen_formula_y_contratos(entorno):
    R = R_(entorno)
    assert motor.consumo_margen(102, -20, R) == pytest.approx(0.4)
    assert motor.consumo_margen(-63, -63, R) == pytest.approx(-75.6)      # pérdida cuenta dos veces
    assert motor.consumo_margen(10, None, R) == pytest.approx(2.0)        # sin telemetría: sin MAE
    # §10.2 (desde plan4.desde): 141 o menos -> 0 contratos; antes del Plan 4, 141 daba 1
    assert [motor.contratos_permitidos(m, R) for m in (750, 749.9, 500, 499.9, 141.01, 141, 140.9)] == [3, 2, 2, 1, 1, 0, 0]
    assert [motor.contratos_permitidos(m, R, nuevo=False) for m in (750, 749.9, 500, 499.9, 141, 140.9)] == [3, 2, 2, 1, 1, 0]


def test_margen_en_operaciones_reales(res):
    pa16 = [f for f in filas_por(res, PA16)]
    # 16/09: 500 + (−89×0,2 − 89) = 393,2; 17/09 sin bono (consumo < 0); 18/09 y 21/09 +140 -> 673,2
    assert pa16[0].margen_tras == 393.2
    assert pa16[1].margen_apertura == 673.2
    bal = {b['cuenta']: b for b in res.bal}
    assert bal[PA16]['margen'] == pytest.approx(681.4 + 140)              # 21/09 consumo 8,2 ≥ 0 -> +140 el 22/09


def test_threshold_tope_ancla_y_manual(entorno):
    apex25 = {'drawdown': 1000, 'tope': 25100}
    assert motor.threshold(None, 24453.03, 25000, apex25) == 24453.03   # ancla > pico − dd
    assert motor.threshold(None, 24453.03, 25600, apex25) == 24600      # sube con el pico
    assert motor.threshold(None, 24453.03, 26500, apex25) == 25100      # tope
    assert motor.threshold(25200, 24453.03, 26500, apex25) == 25200     # manual mayor manda
    assert motor.threshold(24000, 24800, 25000, apex25) == 24800        # manual menor no baja el ancla


def test_threshold_manual_se_vuelve_ancla_y_nunca_baja(entorno):
    ent, con, s = entorno
    hoja = s.hoja
    s.sync(AHORA)
    fila = next(i for i, r in enumerate(hoja.valores) if r[0] == PA16)
    assert 'ANCLA ESTIMADA' in hoja.valores[fila][vista.BC['Observaciones demonio']]
    hoja.valores[fila][vista.BC['Threshold Rithmic (manual)']] = 24500.5
    s.sync(AHORA)
    fila = next(i for i, r in enumerate(hoja.valores) if r[0] == PA16)
    assert hoja.valores[fila][vista.BC['Threshold']] == 24500.5
    assert 'ANCLA ESTIMADA' not in hoja.valores[fila][vista.BC['Observaciones demonio']]
    hoja.valores[fila][vista.BC['Threshold Rithmic (manual)']] = ''           # el operador lo borra
    s.sync(AHORA)
    fila = next(i for i, r in enumerate(hoja.valores) if r[0] == PA16)
    assert hoja.valores[fila][vista.BC['Threshold']] == 24500.5               # el ancla no baja


def test_ratio_br_todos_los_casos(entorno):
    R = R_(entorno)
    r = lambda *a: motor.ratio_br(*a, R)
    assert r(174, 88, 174, True, 500) == pytest.approx(174 / 88)     # ganancia con TP llenado: TP / SL
    assert r(102, 88, 174, False, 500) == pytest.approx(102 / 88)    # ganancia con TP sin llenar: ticks / SL
    assert r(50, 88, None, False, 500) == pytest.approx(50 / 88)     # ganancia sin TP: ticks / SL
    assert r(-148, 120, 88, False, 500) == pytest.approx(88 / 120)   # pérdida con TP: TP / SL
    assert r(-63, 85, None, False, 500) == pytest.approx(136 / 85)   # pérdida sin TP: TP implícito 136
    assert r(-63, None, None, False, 424.4) == pytest.approx(136 / 424.4)   # sin SL: margen en la apertura
    assert r(30, None, 60, True, 500) == pytest.approx(60 / 500)
    assert r(0, 40, None, False, 500) == pytest.approx(136 / 40)     # 0 no es ganancia


def test_racha(entorno):
    R = R_(entorno)
    racha, out = 0, []
    for neto in (60, 50, 20, -5, -10, -30, 49.99, 100):
        racha = motor.siguiente_racha(racha, neto, R)
        out.append(racha)
    assert out == [1, 2, 2, 2, -1, -2, -2, 1]


def test_etiquetas_duracion(entorno):
    R = R_(entorno)
    e = lambda d, mae=-5, mfe=5, sl=40, tp=80: motor.etiqueta_duracion(d, mae, mfe, sl, tp, R)
    assert [e(9), e(10), e(59), e(60), e(300)] == ['MICRO-RUIDO', 'CORTA', 'CORTA', 'CORRECTA', 'CORRECTA']
    assert e(301, mae=-25) == 'LARGA AGUANTANDO PÉRDIDA'
    assert e(301, mae=-5, mfe=70) == 'LARGA DEJANDO CORRER'
    assert e(301) == 'LARGA SIN GESTIÓN'
    assert motor.etiqueta_duracion(400, None, None, 40, 80, R) == 'LARGA'


def test_inferencia_de_cuentas(entorno):
    R = R_(entorno)
    assert cuentas.inferir('PA-APEX-91120-17', R)['tipo'] == 'PA'
    assert cuentas.inferir('APEX-91120-80', R)['tipo'] == 'evaluacion'
    assert cuentas.inferir('fsern2776tcp25d152251', R) == {'firma': 'E2T', 'tipo': 'TCP', 'tamano': 25000}
    assert cuentas.inferir('BX78918-51', R)['firma'] == 'Bulenox'
    assert cuentas.inferir('XYZ-1', R) is None
    assert cuentas.tamano_por_balance('Apex', 26083.04, R) == 25000
    assert cuentas.regla_firma('Bulenox', 50000, R) == {'drawdown': 2000, 'tope': 50100}
    assert motor.cuenta_corta('fsern2776tcp25d152250', R) == 'E2T250'
    assert motor.cuenta_corta('PA-APEX-91120-16', R) == 'PA16'


# ---------------------------------------------------------------------------
# Vista, columnas manuales y acciones del operador
# ---------------------------------------------------------------------------

def test_layout_y_columnas_manuales(entorno):
    ent, con, s = entorno
    res, lay = s.sync(AHORA)
    v = lay['valores']
    assert v[0][:len(vista.BAL_COLS)] == vista.BAL_COLS         # 19 columnas desde el 30/09/2026 (antes 15)
    assert [r[0] for r in v[1:1 + lay['n_bal']]] == [PA15, PA16, E249]       # sim fuera del BAL, orden por nombre
    n_comp = 0                  # V5 (spec §16.3): las filas SÍ/NO de la compuerta ya no se muestran
    assert v[lay['preop_fila'] - 3] == [''] * vista.N_COLS == v[lay['preop_fila'] - 2]       # y encima de la estrategia, el estado del mercado (§12)
    preop = v[lay['preop_fila']]                          # preoperativa encima del encabezado (spec §7.2) y, bajo ella, la compuerta (§10.6)
    assert lay['preop_fila'] == lay['ops_header'] - 1 - n_comp and lay['preop_col'] == vista.OC['Estrategia']
    assert preop[0] == 'Estrategia a usar' and preop[vista.OC['Estrategia']] == 'Sin estrategia'
    assert lay['compuerta_fila'] == lay['preop_fila'] + 1 and lay['compuerta_n'] == n_comp
    assert v[lay['ops_header']] == vista.OPS_COLS
    ops = v[lay['ops_ini']:lay['ops_fin']]
    assert len(ops) == 10                                  # 12 operaciones por cuenta − 2 copias
    aperturas = [r[vista.OC['Fecha y hora apertura']] for r in ops]
    assert aperturas == sorted(aperturas, reverse=True)    # más recientes arriba
    fila = next(r for r in ops if r[0] == 'PA16-20260921-001')
    assert fila[vista.OC['Copias']] == PA15 and fila[vista.OC['Estrategia']] == 'Sin estrategia'
    assert fila[vista.OC['OrderId']] == '2900484753'
    assert 'Sin estrategia' == lay['estrategias'][0] and len(lay['estrategias']) == 10
    # el operador escribe; el demonio lo conserva por ID al regenerar
    i = next(k for k, r in enumerate(s.hoja.valores) if r[0] == 'PA16-20260921-001')
    s.hoja.valores[i][vista.OC['Estrategia']] = 'Cazas de soporte/resistencia en 204s'
    s.hoja.valores[i][vista.OC['Observaciones']] = 'entré tarde'
    s.sync(AHORA)
    fila = next(r for r in s.hoja.valores if r[0] == 'PA16-20260921-001')
    assert fila[vista.OC['Estrategia']] == 'Cazas de soporte/resistencia en 204s'
    assert fila[vista.OC['Observaciones']] == 'entré tarde'


def test_bal_del_dia(entorno):
    ent, con, s = entorno
    res = s.calcular('2026-09-21 18:00:00.000')             # sesión 21/09
    b = {x['cuenta']: x for x in res.bal}[PA16]
    assert b['n_ops'] == 2 and b['n_ganadas'] == 0           # 49,98 y 18,48 < +50
    assert b['resultado_dia'] == 68.46
    # (Σ ticks − Σ|MAE|) / media SL = (141 − Σ|MAE|) / 104
    hoy = [f for f in res.filas if f.cuenta == PA16 and f.sesion == date(2026, 9, 21)]
    esperado = (141 - sum(abs(f.mae) for f in hoy)) / 104
    assert b['ratio_dia'] == pytest.approx(esperado)
    assert b['ultimos_validos'] == [date(2026, 9, 21)]
    fila = vista.fila_bal(b)
    assert fila[vista.BC['Últimos 2 días +50 $']] == '21/09/2026' and fila[vista.BC['Día semana']] == 'Lunes'


def test_retiro_y_quemar(entorno):
    ent, con, s = entorno
    antes = {b['cuenta']: b for b in s.calcular(AHORA).bal}[PA16]
    nombre = cuentas.retiro(ent, con, PA16, 100)
    assert nombre == 'PA-APEX-91120-16_retiros_1'
    despues = {b['cuenta']: b for b in s.calcular(AHORA).bal}[PA16]
    assert despues['balance'] == pytest.approx(antes['balance'] - 100)
    assert despues['nombre'] == nombre
    assert despues['margen'] == antes['margen']                      # hereda margen
    assert despues['threshold'] == antes['threshold']                # el ancla nunca baja
    cuentas.quemar(ent, con, PA15)
    assert PA15 not in {b['cuenta'] for b in s.calcular(AHORA).bal}


def test_sim_ignorada_si_no_esta_activa(entorno):
    ent, con, s = entorno
    cuentas.sim(ent, SIM, False)
    assert not [f for f in s.calcular(AHORA).filas if f.cuenta == SIM]


def test_sin_telemetria_no_consume_margen_por_mae(entorno):
    ent, con, s = entorno
    con.execute("DELETE FROM fills_log WHERE cuenta=? AND fill_id='1478215'", (PA16,))   # log rotado
    con.commit()
    (f,) = [f for f in filas_por(s.calcular(AHORA), PA16) if f.sesion == date(2026, 9, 16)]
    assert not f.telemetria and f.mae is None and f.sl_ini is None and f.ratio is None
    assert f.obs.startswith('[SIN TELEMETRÍA]') and 'SIN SL' not in f.obs
    assert f.margen_tras == pytest.approx(500 - 89 * 0.2)          # solo resultado × 0,2
