"""ATAS 8.0.15 (01/10/2026, arrancó a las 12:08:07): Id del fill <OrderId>_<número> y decimales con punto.

'Order trade 2642617452/43707906/2642617452_1025723 …' no casaba con RE_ORDER_TRADE (exigía solo dígitos en el tercer
campo): fills_log sin fills desde entonces y E2T250-20261001-001 y AP82-20261001-001 [SIN TELEMETRÍA]. MyTrade.cdb trae
el mismo Id nuevo; hasta 8.0.14, el número solo. Se normaliza al número (fuentes.id_fill) en las dos fuentes.
Casos reales: fixtures/ATAS_20261001 (extraer_fixtures.py --8015, de la copia del log del 01/10 y de MyTrade) y líneas
copiadas tal cual de los logs del 30/09 (8.0.14) y del 01/10 (8.0.15).
"""

import os
import shutil

import pytest

from atasv4 import almacen, config, fuentes, reloj, sync
from conftest import RAIZ, HojaFalsa

AQUI = os.path.dirname(os.path.abspath(__file__))
FIXT_8015 = os.path.join(AQUI, 'fixtures', 'ATAS_20261001')
FIXT_28 = os.path.join(AQUI, 'fixtures', 'ATAS_28')
LOG = 'app_20261001.log'
E250, AP82 = 'fsern2776tcp25d152250', 'APEX-91120-82'
AHORA = '2026-10-01 13:44:00.000'                    # 15:44 Madrid, reloj del PC
DESDE = '2026-09-22 00:00:00.000'
CUENTAS = {'cuentas': {
    E250: {'firma': 'E2T', 'tipo': 'TCP', 'tamano': 25000, 'sin_regla': False, 'alta': '2026-10-01', 'retiros': 0,
           'ancla_inicial': None, 'ancla_estimada': True},
    AP82: {'firma': 'Apex', 'tipo': 'evaluacion', 'tamano': 25000, 'sin_regla': False, 'alta': '2026-10-01',
           'retiros': 0, 'ancla_inicial': None, 'ancla_estimada': True}},
    'sim_activas': []}

# Líneas reales (sin tocar). ATAS 8.0.15, 01/10/2026:
OT = ('2026-10-01 15:37:47,978 [Process   ] INFO  hmic[fsern2776]: Order trade 2642617452/43707906/2642617452_1025723 '
      'Sell NQZ6@CME at 30821.25 vol 1.')
REG = ('2026-10-01 15:37:47,976 [Process   ] INFO  hmic[fsern2776]: Order registered 2642617452/43707906 Sell Market '
       'NQZ6@CME on fsern2776tcp25d152250 at 0/0 vol 1/1 tif GTC state Active .')
SL = ('2026-10-01 15:37:48,112 [Process   ] INFO  hmic[fsern2776]: Order registered 2642617461/43707907 Buy Stop '
      'NQZ6@CME on fsern2776tcp25d152250 at 0/31071.00 vol 1/1 tif GTC state Active SL.')
CANCEL_SL = ('2026-10-01 15:37:48,514 [57        ] INFO  hmic[fsern2776]: Cancel order 2642617461/43707907 Buy Stop '
             'NQZ6@CME on fsern2776tcp25d152250 at 0/31071.00 vol 1/1 tif GTC state Active SL.')
BAL = ('2026-10-01 15:37:58,460 [Process   ] INFO  hmic[fsern2776]: Portfolio changed fsern2776tcp25d152250 '
       'Bal:25418.08/25006.09/0 Avl:0 PnL:-135.00/0 L:1 C:0.')
BAL_CIERRE = ('2026-10-01 15:39:16,538 [Process   ] INFO  mic[APEX-91120]: Portfolio changed APEX-91120-82 '
              'Bal:24684.03/0/0 Avl:0 PnL:-90.00/-105 L:1 C:0.')
POS = ('2026-10-01 15:37:47,989 [Process   ] INFO  hmic[fsern2776]: Position changed fsern2776tcp25d152250/NQZ6@CME '
       'vol -1 avg 30821.25 pnl 0/0.')
LAT = ('2026-10-01 15:30:16,073 [Process   ] WARN  Latency manager: Rithmic[APEX-91120] market data latency: median '
       '7548 ms, peak 7548 ms.')
LAT_NORMAL = ('2026-10-01 15:30:42,002 [Process   ] INFO  Latency manager: Rithmic[APEX-91120] market data latency back '
              'to normal: median 710 ms, peak was 8183 ms.')
INICIO = '2026-10-01 12:08:07,016 [11        ] INFO  App            : Started v8.0.15.302'
# ATAS 8.0.14, 30/09/2026 (coma decimal):
OT_14 = ('2026-09-30 18:13:47,640 [Process   ] INFO  hmic[fsern2776]: Order trade 2642003069/41932828/1020511 Buy '
         'NQZ6@CME at 30884,75 vol 1.')
SL_14 = ('2026-09-30 18:13:47,745 [Process   ] INFO  hmic[fsern2776]: Order registered 2642003076/41932829 Sell Stop '
         'NQZ6@CME on fsern2776tcp25d152250 at 0/30864,25 vol 1/1 tif GTC state Active SL.')
BAL_14 = ('2026-09-30 20:39:11,933 [Process   ] INFO  mic[APEX-91120]: Portfolio changed APEX-91120-82 '
          'Bal:24783,01/0/0 Avl:0 PnL:-200,0/0 L:1 C:0.')
POS_14 = ('2026-09-30 18:13:47,606 [Process   ] INFO  hmic[fsern2776]: Position changed fsern2776tcp25d152250/NQZ6@CME '
          'vol 1 avg 30884,75 pnl 0/0.')
# Repetición real al reconectar tras el reinicio de ATAS de las 16:05:38 (app_2026100120261001.log), en el formato nuevo
OT_REPETIDA = ('2026-10-01 16:06:25,079 [Process   ] INFO  hmic[fsern2776]: Order trade '
               '2642617452/43707906/2642617452_1025723 Sell NQZ6@CME at 30821.25 vol 1.')
# Derivada de OT: la misma línea como la escribiría 8.0.14 (no hay ningún fill real escrito en los dos formatos)
OT_COMO_14 = OT.replace('/2642617452_1025723 ', '/1025723 ')


def _entorno(tmp_path, atas_dir=FIXT_8015, inicio='2026-10-01', log=None):
    base = str(tmp_path / 'base')
    os.makedirs(base, exist_ok=True)
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), os.path.join(base, 'reglas.yaml'))
    cfg = config.cargar_yaml(os.path.join(RAIZ, 'config.yaml'), {})
    cfg.update({'atas_dir': atas_dir, 'sqlite': os.path.join(base, 't.sqlite'), 'backups_dir': os.path.join(base, 'b'),
                'inicio_sesion': inicio, 'quemadas_iniciales': [], 'logs_respaldo': None})
    config.guardar_yaml(os.path.join(base, 'config.yaml'), cfg)
    config.guardar_yaml(os.path.join(base, 'cuentas.yaml'), CUENTAS)
    ent = config.cargar_entorno(base)
    con = almacen.conectar(ent.sqlite_path)
    return ent, con, sync.Sincronizador(ent, con, HojaFalsa(), log=log if log is not None else (lambda m: None))


def _escribir_log(d, lineas, modo='w', nombre=LOG):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, nombre), modo, encoding='utf-8') as f:
        f.write(''.join(l + '\n' for l in lineas))


def _filas(res):
    return {f.op_id: f for f in res.filas}


# ===========================================================================
# 1. Parseo de las dos versiones
# ===========================================================================

def test_parse_order_trade_de_8015_y_de_8014():
    assert fuentes.parse_linea(OT) == ('fill_ot', {
        'order_id': '2642617452', 'fill_id': '1025723', 'lado': 'Sell', 'simbolo': 'NQZ6@CME', 'precio': 30821.25,
        'qty': 1.0, 't_log': '2026-10-01 15:37:47.978'})
    assert fuentes.parse_linea(OT_14) == ('fill_ot', {
        'order_id': '2642003069', 'fill_id': '1020511', 'lado': 'Buy', 'simbolo': 'NQZ6@CME', 'precio': 30884.75,
        'qty': 1.0, 't_log': '2026-09-30 18:13:47.640'})
    assert fuentes.parse_linea(INICIO) == ('inicio', {'version': '8.0.15.302', 't_log': '2026-10-01 12:08:07.016'})


def test_el_mismo_fill_tiene_el_mismo_id_en_las_dos_versiones_y_en_mytrade():
    assert fuentes.parse_linea(OT)[1]['fill_id'] == fuentes.parse_linea(OT_COMO_14)[1]['fill_id'] == '1025723'
    assert fuentes.id_fill('2642617452_1025723') == '1025723'          # MyTrade.cdb de 8.0.15 (fila real)
    assert fuentes.id_fill('1020511') == '1020511'                     # 8.0.14: no cambia
    assert fuentes.id_fill('pata:1633') == 'pata:1633'                 # otro formato: tal cual


def test_lineas_nuevas_de_latency_manager_no_se_leen():
    """8.0.15 escribe la latencia mediana sin time/event/diff/delay: no da salto ni desfase, no se lee."""
    assert fuentes.parse_linea(LAT) is None and fuentes.parse_linea(LAT_NORMAL) is None


def test_stop_precio_y_balance_con_punto_decimal():
    """8.0.15 escribe los decimales con punto (8.0.14, con coma): los mismos números."""
    tipo, d = fuentes.parse_linea(SL)
    assert (tipo, d['tipo'], d['limite'], d['stop'], d['etiqueta'], d['origen']) == (
        'orden', 'Stop', 0.0, 31071.0, 'SL', 'registered')
    tipo, d = fuentes.parse_linea(CANCEL_SL)
    assert (tipo, d['ext_id'], d['etiqueta']) == ('cancel', '2642617461', 'SL')
    assert fuentes.parse_linea(BAL)[1] == {'cuenta': E250, 't_log': '2026-10-01 15:37:58.460', 'bal': 25418.08,
                                           'pnl_abierto': -135.0, 'pnl_cerrado': 0.0}
    d = fuentes.parse_linea(BAL_CIERRE)[1]
    assert (d['bal'], d['pnl_abierto'], d['pnl_cerrado']) == (24684.03, -90.0, -105.0)
    assert fuentes.parse_linea(POS)[1]['volumen'] == -1.0
    # 8.0.14
    assert fuentes.parse_linea(SL_14)[1]['stop'] == 30864.25
    d = fuentes.parse_linea(BAL_14)[1]
    assert (d['bal'], d['pnl_abierto']) == (24783.01, -200.0)
    assert fuentes.parse_linea(POS_14)[1]['volumen'] == 1.0


# ===========================================================================
# 2. Ingesta: un fill por Id, cuenta pendiente, cruce con MyTrade
# ===========================================================================

@pytest.mark.parametrize('lineas', [[REG, OT, OT_REPETIDA], [REG, OT_REPETIDA, OT], [REG, OT_COMO_14, OT_REPETIDA],
                                    [REG, OT_REPETIDA, OT_COMO_14]])
def test_repeticion_al_reconectar_en_formato_nuevo_una_fila_con_la_primera_hora(tmp_path, lineas):
    """Repetición en el formato de 8.0.15 (real: 16:06:25, tras reiniciar ATAS), también de un fill cuya primera
    aparición fuera en el de 8.0.14 (reconexión tras actualizar ATAS), leída en otra ingesta y en cualquier orden: una
    fila, con la hora de la primera."""
    con = almacen.conectar(':memory:')
    d = str(tmp_path)
    _escribir_log(d, lineas[:2])
    almacen.ingerir_logs(con, d, DESDE)
    _escribir_log(d, lineas[2:], modo='a')
    almacen.ingerir_logs(con, d, DESDE)
    assert [tuple(r) for r in con.execute('SELECT cuenta, fill_id, t_log, order_id FROM fills_log')] == [
        (E250, '1025723', '2026-10-01 15:37:47.978', '2642617452')]
    assert con.execute('SELECT COUNT(*) FROM fills_ot_pendientes').fetchone()[0] == 0


def test_cuenta_pendiente_se_resuelve_con_el_fill_de_mytrade_de_8015(tmp_path):
    """'Order trade' sin su orden en el log: espera en fills_ot_pendientes y la cuenta sale del fill de MyTrade.cdb,
    cuyo Id (2642617452_1025723) se guarda normalizado: los dos casan."""
    con = almacen.conectar(':memory:')
    d = str(tmp_path / 'Logs')
    _escribir_log(d, [OT])
    almacen.ingerir_logs(con, d, DESDE)
    assert [tuple(r) for r in con.execute('SELECT fill_id, order_id FROM fills_ot_pendientes')] == [
        ('1025723', '2642617452')]
    assert con.execute('SELECT COUNT(*) FROM fills_log').fetchone()[0] == 0
    # 4 fills y 2 patas nuevos, y el pendiente resuelto
    assert almacen.ingerir_cdb(con, os.path.join(FIXT_8015, 'Database'), '2026-09-30 22:00:00.000') == 4 + 2 + 1
    assert [tuple(r) for r in con.execute('SELECT cuenta, fill_id, t_log FROM fills_log')] == [
        (E250, '1025723', '2026-10-01 15:37:47.978')]
    assert con.execute('SELECT COUNT(*) FROM fills_ot_pendientes').fetchone()[0] == 0
    assert sorted(r[0] for r in con.execute('SELECT fill_id FROM fills')) == ['1025596', '1025723', '1804983', '1805112']


def test_cruce_log_mytrade_y_reloj_del_tramo_8015(tmp_path):
    """Los 4 fills del log casan con los de MyTrade (sin duplicarse: 2 por cuenta) y dan al tramo de 8.0.15 sus
    muestras (MyTrade, truncada al segundo): log − MyTrade − 0,5 = 0,478 · 0,636 · 0,883 · 0,038 -> mediana 0,557."""
    log = []
    ent, con, s = _entorno(tmp_path, log=log.append)
    s.ingerir()
    assert con.execute('SELECT COUNT(*) FROM fills_log l JOIN fills f ON f.cuenta=l.cuenta AND f.fill_id=l.fill_id'
                       ).fetchone()[0] == 4
    assert s._estado_cuentas(AHORA) == {E250: (True, 2), AP82: (True, 2)}
    rel = reloj.cargar(con, ent.reglas)
    assert [(d['inicio'], d['desfase_s'], d['muestras'], d['fuente']) for d in rel.detalle] == [
        ('2026-10-01 12:07:33.003', 0.557, 0, 'heredado de la sesión 2026-10-01 12:08:07.016'),
        ('2026-10-01 12:08:07.016', 0.557, 4, 'MyTrade (al segundo)')]
    s.sync(AHORA)
    assert '[RELOJ] Tramo desde 2026-10-01 12:08:07.016 (arranque de ATAS): log − exchange = +0.557 s ' \
           '(MyTrade (al segundo), 4 muestras)' in log


# ===========================================================================
# 3. La operación de hoy, con telemetría
# ===========================================================================

def test_operacion_real_del_01_10_con_telemetria(tmp_path):
    """E2T-250, corto de 1 NQ: 30.821,25 (15:37:47,978) -> 30.826,25 (15:39:16,383), −20 t. Flotante del log (PnL
    abierto ÷ 5 $): 0 −27 +24 +22 −41 +5 −52 −55 y el cierre −20 -> MAE −55, MFE +24. SL inicial 31.071,00 (15:37:48,112):
    999 t, que baja hasta 31.058,50 (949 t); sin TP: ratio 136 / 999. Entrada y salida a mercado. APEX-82, mismo corto
    (30.821,25 -> 30.826,50, −21 t): MAE −53 (−265 $), MFE +19 (+95 $)."""
    ent, con, s = _entorno(tmp_path)
    s.ingerir()
    f = _filas(s.calcular(AHORA))
    assert set(f) == {'E2T250-20261001-001', 'AP82-20261001-001'}
    e, a = f['E2T250-20261001-001'], f['AP82-20261001-001']
    assert all(x.telemetria for x in (e, a))
    assert all(ent.reglas['observaciones']['sin_telemetria'] not in x.obs for x in (e, a))
    assert (e.mae, e.mfe, e.sl_ini, e.sl_max, e.tp_final, e.tipo_apertura, e.tipo_cierre) == (
        -55, 24, 999, 999, None, 'Mercado', 'Mercado')
    assert e.ratio == pytest.approx(136 / 999) and e.sl_hist[-1] == 949 and e.obs.startswith('SL 999→998→')
    assert (e.ticks, e.neto, e.max_amplitud) == (-20, -104.08, 79)
    assert (a.mae, a.mfe, a.tipo_apertura, a.tipo_cierre, a.ticks, a.neto) == (-53, 19, 'Mercado', 'Mercado', -21, -108.98)
    # horas: las de MyTrade (al segundo), como antes
    assert (e.t_apertura_utc, e.t_cierre_utc) == ('2026-10-01 13:37:47.000', '2026-10-01 13:39:15.000')


# ===========================================================================
# 4. Migración (una vez, idempotente)
# ===========================================================================

TABLAS = ('fills', 'fills_log', 'fills_ot_pendientes', 'ordenes', 'cancelaciones', 'equity', 'posiciones_hist',
          'sesiones_atas', 'latencias', 'patas', 'ops_ids', 'avisos_emitidos', 'cortafuegos_saltados', 'log_offsets')


def _foto(con):
    return {t: sorted(tuple(r) for r in con.execute(f'SELECT * FROM {t}')) for t in TABLAS}


def _como_el_codigo_anterior(con):
    """Estado que deja el código anterior al 01/10 con ATAS 8.0.15: Id de MyTrade tal cual (<OrderId>_<número>) en
    fills y en las claves que los llevan, sin los 'Order trade' en fills_log, log leído hasta el final y sin la marca."""
    viejo = {(r['cuenta'], r['fill_id']): f"{r['order_id']}_{r['fill_id']}" for r in con.execute('SELECT * FROM fills')}
    con.execute("UPDATE fills SET fill_id = order_id || '_' || fill_id")
    for r in con.execute('SELECT clave FROM ops_ids').fetchall():
        cuenta, fid = r['clave'].split('|')
        con.execute('UPDATE ops_ids SET clave=? WHERE clave=?', (f"{cuenta}|{viejo[(cuenta, fid)]}", r['clave']))
    for r in con.execute('SELECT clave FROM avisos_emitidos').fetchall():
        tipo, cuenta, fid = r['clave'].split('|')
        con.execute('UPDATE avisos_emitidos SET clave=? WHERE clave=?',
                    (f"{tipo}|{cuenta}|{viejo[(cuenta, fid)]}", r['clave']))
    con.execute('DELETE FROM fills_log')
    con.execute('DELETE FROM estado WHERE clave=?', (almacen.MIGRACION_ATAS_8015,))
    con.commit()


def test_migracion_recupera_la_telemetria_una_vez_y_sin_duplicar(tmp_path):
    ent, con, s = _entorno(tmp_path)
    s.ingerir()
    s.vigilar(lambda titulo, texto: None, AHORA)            # ops_ids y avisos_emitidos con el Id de cada decisión
    completo = _foto(con)
    assert len(completo['fills_log']) == 4 and len(completo['avisos_emitidos']) > 0
    _como_el_codigo_anterior(con)
    assert not any(x.telemetria for x in s.calcular(AHORA).filas)        # [SIN TELEMETRÍA], como en producción
    assert con.execute("SELECT COUNT(*) FROM ops_ids WHERE clave LIKE '%\\_%' ESCAPE '\\'").fetchone()[0] == 2
    log = []
    s2 = sync.Sincronizador(ent, con, HojaFalsa(), log=log.append)       # arranque del código nuevo
    assert ("[MIGRACIÓN] ATAS 8.0.15 (Id del fill <OrderId>_<número>): 4 Id de MyTrade pasados al número; "
            "logs que se releen: app_20261001.log.") in log
    assert con.execute('SELECT COUNT(*) FROM log_offsets').fetchone()[0] == 0
    s2.ingerir()
    assert _foto(con) == completo                            # mismos Id, mismas claves, sin duplicar nada
    f = _filas(s2.calcular(AHORA))
    assert set(f) == {'E2T250-20261001-001', 'AP82-20261001-001'} and all(x.telemetria for x in f.values())
    # segunda vez: no hace nada
    assert almacen.migrar_atas_8015(con) is None
    log.clear()
    s3 = sync.Sincronizador(ent, con, HojaFalsa(), log=log.append)
    s3.ingerir()
    s3.vigilar(lambda titulo, texto: None, AHORA)
    assert not any('[MIGRACIÓN]' in m for m in log) and _foto(con) == completo


def test_migracion_sin_arranque_de_8015_no_relee_y_no_toca_los_id(tmp_path):
    """Base de antes de 8.0.15 (28/09, ATAS 8.0.14): no se saltó ninguna línea; se marca hecha sin tocar nada."""
    ent, con, s = _entorno(tmp_path, FIXT_28, inicio='2026-09-28')
    s.ingerir()
    antes = _foto(con)
    almacen.set_estado(con, almacen.MIGRACION_ATAS_8015, '0')
    con.commit()
    assert almacen.migrar_atas_8015(con) == (0, [])
    assert _foto(con) == antes and almacen.get_estado(con, almacen.MIGRACION_ATAS_8015) == '1'


def test_migracion_con_el_id_normalizado_ya_guardado_deja_uno(tmp_path):
    """Si el fill ya estaba con el Id normalizado, el del formato nuevo sobra (es el mismo fill)."""
    con = almacen.conectar(':memory:')
    for fid in ('2642617452_1025723', '1025723'):
        con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)',
                    (E250, 'NQZ6@CME', fid, '2642617452', 'Sell', 30821.25, 1.0, '2026-10-01 13:37:47.000'))
    con.execute("INSERT INTO ops_ids VALUES (?, 'E2T250-20261001-001', ?, '2026-10-01', 1)", (f'{E250}|1025723', E250))
    assert almacen.migrar_atas_8015(con) == (1, [])
    assert [tuple(r) for r in con.execute('SELECT cuenta, fill_id FROM fills')] == [(E250, '1025723')]
    assert [r[0] for r in con.execute('SELECT clave FROM ops_ids')] == [f'{E250}|1025723']
