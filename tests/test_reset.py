"""Reset de una cuenta (spec §14, 03/11/2026): cuenta lógica `<ID físico>-resetN`.

Apex resetea una evaluación conservando el ID de Rithmic (APEX-91120-74 y -75 el 28/10/2026 a las 21:06:59.587). Todo evento de
la cuenta física con hora >= `desde` es de `<ID>-resetN`; lo anterior se queda en la cuenta original. Escenario sintético mínimo
(1 NQ por fase, hora de Madrid = UTC + 1, como a finales de octubre):

    27/10 15:00   cuenta ORIGINAL: corto 30000 -> 30010 (-40 t, -203,98 $): Bal 50000 -> 49796,02
    28/10 21:07   reset: Bal 50000 (el corte es 21:06:59.587)
    28/10 22:30   RESET1: largo 30000 -> 30010 (+40 t, +196,02 $): Bal 50196,02
    29/10 11:00   RESET2 (segundo corte 29/10 10:00): corto 30010 -> 30000 (+40 t, +196,02 $)

Ficheros de ATAS escritos a mano en un directorio temporal: ni la base ni el sheet vivos.
"""

import os
import shutil
from datetime import timedelta

import pytest
import yaml

import cli_v4
from atasv4 import almacen, config, cuentas, fuentes, motor, resets, sync
from conftest import RAIZ, HojaFalsa

NQ = 'NQZ6@CME'
FISICA, R1, R2 = 'APEX-91120-74', 'APEX-91120-74-reset1', 'APEX-91120-74-reset2'
OTRA, OTRA_R1 = 'APEX-91120-75', 'APEX-91120-75-reset1'
DESDE1 = '2026-10-28 21:06:59.587'
DESDE2 = '2026-10-29 10:00:00.000'
AHORA = '2026-10-29 10:30:00.000'                    # reloj del PC (UTC): sesión CME del 29/10
FICHA = {'firma': 'Apex', 'tipo': 'evaluacion', 'tamano': 50000, 'sin_regla': False, 'alta': '2026-10-26', 'retiros': 0,
         'ancla_inicial': None, 'ancla_estimada': True}


def _l(t, texto):
    return f"{t[:19]},{t[20:23]} [Process   ] INFO  mic[APEX-91120]: {texto}"


def _bal(cuenta, t, bal, pnl='0'):
    return _l(t, f"Portfolio changed {cuenta} Bal:{bal:.2f}/0/0 Avl:0 PnL:{pnl}/0 L:1 C:0.")


def _tramo(cuenta, oid, fid, t, lado, px, vol, bal):
    """Una orden de mercado llenada (alta, fill, cierre de la orden), el balance y la posición tras ella (ATAS 8/Apex: el
    'Bal:' va antes del 'Position changed' del mismo ms). Al volver a plano, un 'Bal:' bueno un segundo después."""
    o = f"{oid}/4{oid[-4:]}"
    lineas = [_l(t, f"Order registered {o} {lado} Market {NQ} on {cuenta} at 0/0 vol 1/1 tif GTC state Active ."),
              _l(t, f"Order trade {o}/{oid}_{fid} {lado} {NQ} at {px:.2f} vol 1."),
              _l(t, f"Order changed {o} {lado} Market {NQ} on {cuenta} at 0/0 vol 1/0 tif GTC state Done ."),
              _bal(cuenta, t, bal),
              _l(t, f"Position changed {cuenta}/{NQ} vol {vol} avg {px:.2f} pnl 0/0.")]
    if vol == 0:
        lineas.append(_bal(cuenta, fuentes.fmt_ts(fuentes.parse_ts(t) + timedelta(seconds=1)), bal))
    return lineas


def _mytrade(cuenta, oid, fid, t_madrid, lado, px):
    t = fuentes.fmt_ts(fuentes.madrid_a_utc(fuentes.parse_ts(t_madrid)))
    h = f"{t[5:7]}/{t[8:10]}/{t[:4]} {t[11:19]}"
    return f', "{cuenta}", "{NQ}", "{oid}_{fid}", "{oid}", "{lado}", "{px:.2f}", "{h}", "1", "0", "0.0", '


def _pata(pid, cuenta, t_ab, px_ab, vol_ab, t_ci, px_ci, pnl):
    def h(t):
        u = fuentes.fmt_ts(fuentes.madrid_a_utc(fuentes.parse_ts(t)))
        return f"{u[5:7]}/{u[8:10]}/{u[:4]} {u[11:19]}"
    return (f'"{pid}", "{cuenta}", "{NQ}", "{h(t_ab)}", "{px_ab:.2f}", "{vol_ab}", "{h(t_ci)}", "{px_ci:.2f}", "{-vol_ab}", '
            f'"{pnl:.2f}", "{pnl / 5:.0f}", "0", , , "False", "", "", "None"')


def _escribir_atas(atas):
    """Logs y .cdb de las fases (original, corte, reset1, reset2) de FISICA."""
    L27 = [_l('2026-10-27 13:00:00.000', 'App            : Started v8.0.15.306'),
           _bal(FISICA, '2026-10-27 14:59:00.000', 50000.0)]
    L27 += _tramo(FISICA, '2913000001', '1900001', '2026-10-27 15:00:00.100', 'Sell', 30000.0, -1, 50000.0)
    L27 += _tramo(FISICA, '2913000002', '1900002', '2026-10-27 15:00:40.200', 'Buy', 30010.0, 0, 49796.02)
    L28 = [_bal(FISICA, DESDE1, 50000.0)]
    L28 += _tramo(FISICA, '2913000003', '1900003', '2026-10-28 22:30:00.100', 'Buy', 30000.0, 1, 50000.0)
    L28 += _tramo(FISICA, '2913000004', '1900004', '2026-10-28 22:30:40.200', 'Sell', 30010.0, 0, 50196.02)
    L29 = _tramo(FISICA, '2913000005', '1900005', '2026-10-29 11:00:00.100', 'Sell', 30010.0, -1, 50196.02)
    L29 += _tramo(FISICA, '2913000006', '1900006', '2026-10-29 11:00:40.200', 'Buy', 30000.0, 0, 50392.04)
    os.makedirs(os.path.join(atas, 'Logs'), exist_ok=True)
    os.makedirs(os.path.join(atas, 'Database'), exist_ok=True)
    for nombre, lineas in (('app_20261027.log', L27), ('app_20261028.log', L28), ('app_20261029.log', L29)):
        with open(os.path.join(atas, 'Logs', nombre), 'w', encoding='utf-8') as f:
            f.write(''.join(x + '\n' for x in lineas))
    fills = [_mytrade(FISICA, '2913000001', '1900001', '2026-10-27 15:00:00.100', 'Sell', 30000.0),
             _mytrade(FISICA, '2913000002', '1900002', '2026-10-27 15:00:40.200', 'Buy', 30010.0),
             _mytrade(FISICA, '2913000003', '1900003', '2026-10-28 22:30:00.100', 'Buy', 30000.0),
             _mytrade(FISICA, '2913000004', '1900004', '2026-10-28 22:30:40.200', 'Sell', 30010.0),
             _mytrade(FISICA, '2913000005', '1900005', '2026-10-29 11:00:00.100', 'Sell', 30010.0),
             _mytrade(FISICA, '2913000006', '1900006', '2026-10-29 11:00:40.200', 'Buy', 30000.0)]
    patas = [_pata('1', FISICA, '2026-10-27 15:00:00.100', 30000.0, -1, '2026-10-27 15:00:40.200', 30010.0, -200.0),
             _pata('2', FISICA, '2026-10-28 22:30:00.100', 30000.0, 1, '2026-10-28 22:30:40.200', 30010.0, 200.0),
             _pata('3', FISICA, '2026-10-29 11:00:00.100', 30010.0, -1, '2026-10-29 11:00:40.200', 30000.0, 200.0)]
    with open(os.path.join(atas, 'Database', 'MyTrade.cdb'), 'w', encoding='utf-8') as f:
        f.write('Version: "1"\nEntity: "MyTrade"\nColumns: "Route", "AccountID", "SecurityId", "Id", "OrderId", '
                '"OrderDirection", "Price", "Time", "Volume", "OpenVolume", "Commission", "CommissionCurrency"\n'
                'StartData\n' + ''.join(x + '\n' for x in fills) + 'EndData\n')
    with open(os.path.join(atas, 'Database', 'HistoryMyTrade.cdb'), 'w', encoding='utf-8') as f:
        f.write('Version: "2"\nEntity: "HistoryMyTrade"\nColumns: "Id", "AccountID", "SecurityId", "OpenTime", "OpenPrice", '
                '"OpenVolume", "CloseTime", "ClosePrice", "CloseVolume", "PnL", "TicksPnL", "PricePnL", "Commission", '
                '"Comment", "Reviewed", "EnterTradeId", "ExitTradeId", "MissingDataCase"\nStartData\n'
                + ''.join(x + '\n' for x in patas) + 'EndData\n')


def _montar(tmp_path, registro=None, fichas=None):
    atas = str(tmp_path / 'ATAS')
    base = str(tmp_path / 'base')
    os.makedirs(base, exist_ok=True)
    _escribir_atas(atas)
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), os.path.join(base, 'reglas.yaml'))
    cfg = config.cargar_yaml(os.path.join(RAIZ, 'config.yaml'), {})
    cfg.update({'atas_dir': atas, 'sqlite': os.path.join(base, 't.sqlite'), 'backups_dir': os.path.join(base, 'b'),
                'inicio_sesion': '2026-10-26', 'quemadas_iniciales': [], 'logs_respaldo': None})
    config.guardar_yaml(os.path.join(base, 'config.yaml'), cfg)
    datos = {'cuentas': dict(fichas or {FISICA: dict(FICHA)}), 'sim_activas': []}
    if registro:
        datos['resets'] = registro
    config.guardar_yaml(os.path.join(base, 'cuentas.yaml'), datos)
    ent = config.cargar_entorno(base)
    con = almacen.conectar(ent.sqlite_path)
    return ent, con, sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)


def _reg1():
    return {FISICA: [{'desde': DESDE1, 'nombre': R1}]}


def _reg2():
    return {FISICA: [{'desde': DESDE1, 'nombre': R1}, {'desde': DESDE2, 'nombre': R2}]}


TABLAS = ('fills', 'patas', 'fills_log', 'ordenes', 'cancelaciones', 'equity', 'posiciones_hist', 'posiciones')


def _volcado(con):
    """Contenido de las tablas partidas por reset, sin rowid: lo que se compara entre rutas."""
    return {t: sorted(tuple(r) for r in con.execute(f'SELECT * FROM {t}')) for t in TABLAS}


def _por_cuenta(con, tabla):
    return {r[0]: r[1] for r in con.execute(f'SELECT cuenta, COUNT(*) FROM {tabla} GROUP BY cuenta')}


# ===========================================================================
# Traducción física + hora -> lógica (el único punto de verdad)
# ===========================================================================

def test_cuenta_logica_corte_exacto_y_orden():
    reg = _reg2()
    assert resets.cuenta_logica(reg, FISICA, '2026-10-28 21:06:59.586') == FISICA
    assert resets.cuenta_logica(reg, FISICA, DESDE1) == R1                          # >= desde: el instante del corte es del reset
    assert resets.cuenta_logica(reg, FISICA, '2026-10-29 09:59:59.999') == R1
    assert resets.cuenta_logica(reg, FISICA, DESDE2) == R2
    assert resets.cuenta_logica(reg, 'OTRA-CUENTA', DESDE2) == 'OTRA-CUENTA'        # sin resets, la misma
    assert resets.cuenta_logica(None, FISICA, DESDE2) == FISICA
    assert resets.logicas(reg, FISICA) == [FISICA, R1, R2] and resets.logica_actual(reg, FISICA) == R2
    assert resets.fisica_de(reg, R2) == FISICA and resets.fisica_de(reg, FISICA) == FISICA
    assert resets.es_reset(reg, R1) and not resets.es_reset(reg, FISICA)


def test_horas_utc_de_los_cdb_se_comparan_con_desde_pasado_a_utc():
    """21:06:59.587 de Madrid el 28/10 (CET, UTC+1) son las 20:06:59.587 UTC; en verano (UTC+2), 10:00 UTC las 12:00."""
    reg = {FISICA: [{'desde': DESDE1, 'nombre': R1}]}
    assert resets.desde_utc(DESDE1) == '2026-10-28 20:06:59.587'
    assert resets.cuenta_logica(reg, FISICA, '2026-10-28 20:06:59.000', utc=True) == FISICA      # el cdb trunca al segundo
    assert resets.cuenta_logica(reg, FISICA, '2026-10-28 20:07:00.000', utc=True) == R1
    verano = {FISICA: [{'desde': '2026-07-01 12:00:00.000', 'nombre': R1}]}
    assert resets.cuenta_logica(verano, FISICA, '2026-07-01 09:59:59.000', utc=True) == FISICA
    assert resets.cuenta_logica(verano, FISICA, '2026-07-01 10:00:00.000', utc=True) == R1


def test_normalizar_desde():
    assert resets.normalizar_desde('2026-10-28 21:06:59.587') == DESDE1
    assert resets.normalizar_desde('2026-10-28 21:06:59') == '2026-10-28 21:06:59.000'
    assert resets.normalizar_desde(yaml.safe_load('2026-10-28 21:06:59.587')) == DESDE1      # YAML sin comillas -> datetime
    with pytest.raises(ValueError):
        resets.normalizar_desde('ayer por la tarde')


def test_portfolio_cdb_va_a_la_logica_mas_reciente():
    """Portfolio.cdb trae el balance por el ID físico y sin hora (spec §14.1): pertenece a la lógica más reciente. El código
    no lo lee (la verificación de INFORME §8 lo cruza a mano); `logica_actual` es el punto único para hacerlo."""
    assert resets.logica_actual(_reg1(), FISICA) == R1
    assert resets.logica_actual(_reg2(), FISICA) == R2
    assert resets.logica_actual({}, FISICA) == FISICA


def test_copytrading_figura_para_la_original_y_para_sus_resets():
    """CopyTrading.cnf trae IDs físicos: el líder físico figura en todas sus cuentas lógicas, así lo anterior al reset se juzga
    como antes (spec §4.1) y la cuenta nueva también figura."""
    pares = [(FISICA, 'APEX-91120-79'), ('X', FISICA)]
    assert resets.expandir_pares({}, pares) == pares
    ex = resets.expandir_pares(_reg1(), pares)
    assert (FISICA, 'APEX-91120-79') in ex and (R1, 'APEX-91120-79') in ex
    assert ('X', FISICA) in ex and ('X', R1) in ex


# ===========================================================================
# Ingesta: el corte, en todas las fuentes
# ===========================================================================

def test_ingesta_parte_todas_las_fuentes_en_el_corte(tmp_path):
    ent, con, s = _montar(tmp_path, _reg1())
    s.ingerir()
    for tabla, esperado in (('fills', {FISICA: 2, R1: 4}), ('patas', {FISICA: 1, R1: 2}),
                            ('fills_log', {FISICA: 2, R1: 4}), ('ordenes', {FISICA: 2, R1: 4}),
                            ('posiciones_hist', {FISICA: 2, R1: 4})):
        assert _por_cuenta(con, tabla) == esperado, tabla
    # 'Bal:' (log) y 'Position changed': lo anterior al corte, en la original; el Bal: de las 21:06:59.587 ya es del reset
    assert sorted(r['bal'] for r in con.execute('SELECT bal FROM equity WHERE cuenta=?', (FISICA,))) == \
        [49796.02, 49796.02, 50000.0, 50000.0]
    assert con.execute('SELECT MIN(t_log) FROM equity WHERE cuenta=?', (R1,)).fetchone()[0] == DESDE1
    assert con.execute('SELECT t_log FROM posiciones WHERE cuenta=?', (R1,)).fetchone()[0] == '2026-10-29 11:00:40.200'
    assert con.execute('SELECT volumen FROM posiciones WHERE cuenta=?', (FISICA,)).fetchone()[0] == 0
    assert almacen.primer_bal(con, R1) == (DESDE1, 50000.0)         # el primer balance del reset, no el último
    assert almacen.ultimo_bal_plano(con, FISICA)[1] == 49796.02     # lo último de la original: el balance viejo


def test_cancelaciones_tambien_se_parten(tmp_path):
    ent, con, s = _montar(tmp_path, _reg1())
    with open(os.path.join(ent.atas_dir, 'Logs', 'app_20261028.log'), 'a', encoding='utf-8') as f:
        f.write(_l('2026-10-28 22:31:00.000', f"Cancel order 2913000099/49999 Sell Stop {NQ} on {FISICA} at 0/29900.00 "
                                              "vol 1/1 tif GTC state Active SL.") + '\n')
        f.write(_l('2026-10-27 15:05:00.000', f"Cancel order 2913000098/49998 Sell Stop {NQ} on {FISICA} at 0/29900.00 "
                                              "vol 1/1 tif GTC state Active SL.") + '\n')
    s.ingerir()
    assert _por_cuenta(con, 'cancelaciones') == {FISICA: 1, R1: 1}


# ===========================================================================
# Retroactivo e idempotente
# ===========================================================================

def test_reasignacion_retroactiva_da_lo_mismo_que_ingerir_con_el_registro(tmp_path):
    _, con_a, s_a = _montar(tmp_path / 'a', _reg1())
    s_a.ingerir()
    ent, con_b, s_b = _montar(tmp_path / 'b')               # sin reset: todo bajo el ID físico
    s_b.ingerir()
    assert _por_cuenta(con_b, 'fills') == {FISICA: 6} and _por_cuenta(con_b, 'equity') == {FISICA: 11}
    nombre, movidas, _ = cuentas.reset(ent, con_b, FISICA, DESDE1)
    assert nombre == R1 and movidas['fills'] == 4 and movidas['equity'] == 7 and movidas['fills_log'] == 4
    assert _volcado(con_b) == _volcado(con_a)
    assert ent.cuentas['resets'] == _reg1()
    assert yaml.safe_load(open(ent.cuentas_path, encoding='utf-8'))['resets'] == _reg1()     # persistido, con comillas


def test_reasignar_es_idempotente_y_la_relectura_tampoco_duplica(tmp_path):
    ent, con, s = _montar(tmp_path)
    s.ingerir()
    cuentas.reset(ent, con, FISICA, DESDE1)
    antes = _volcado(con)
    almacen.reasignar_resets(con, ent.resets)
    almacen.reasignar_resets(con, ent.resets)
    assert _volcado(con) == antes
    assert almacen.aplicar_resets(con, ent.resets) is None                 # misma huella: nada que hacer
    con.execute('DELETE FROM log_offsets')                                  # relectura entera de los logs y de los .cdb
    con.commit()
    s.ingerir(forzar_cdb=True)
    assert _volcado(con) == antes


def test_registro_editado_a_mano_se_aplica_en_la_siguiente_ingesta(tmp_path):
    """Sin pasar por el CLI: el demonio recarga cuentas.yaml y la ingesta reasigna lo ya guardado una sola vez."""
    ent, con, s = _montar(tmp_path)
    s.ingerir()
    ent.cuentas['resets'] = _reg1()
    mensajes = []
    s.log = mensajes.append
    s.ingerir()
    assert _por_cuenta(con, 'fills') == {FISICA: 2, R1: 4}
    assert any('[RESET]' in m for m in mensajes)
    mensajes.clear()
    s.ingerir()
    assert not any('[RESET]' in m for m in mensajes)


def test_segundo_reset_mueve_lo_del_primero_y_lo_de_la_original(tmp_path):
    """reset2 tras reset1: lo posterior a desde2 sale de reset1 (y de la original, si nunca pasó por reset1) hacia reset2."""
    ent, con, s = _montar(tmp_path)
    s.ingerir()
    cuentas.reset(ent, con, FISICA, DESDE1)
    assert _por_cuenta(con, 'fills') == {FISICA: 2, R1: 4}
    nombre, movidas, _ = cuentas.reset(ent, con, FISICA, DESDE2)
    assert nombre == R2 and movidas['fills'] == 2
    assert _por_cuenta(con, 'fills') == {FISICA: 2, R1: 2, R2: 2}
    assert _por_cuenta(con, 'equity') == {FISICA: 4, R1: 4, R2: 3}
    assert con.execute('SELECT t_log FROM posiciones WHERE cuenta=?', (R1,)).fetchone()[0] == '2026-10-28 22:30:40.200'
    _, con_c, s_c = _montar(tmp_path / 'c', _reg2())                       # ingiriendo con los dos cortes de golpe, igual
    s_c.ingerir()
    assert _volcado(con_c) == _volcado(con)


# ===========================================================================
# Cuenta nueva: alta automática sin herencia (spec §14.1)
# ===========================================================================

def test_alta_de_la_cuenta_nueva_como_automatica(tmp_path):
    ent, con, s = _montar(tmp_path, _reg1())
    s.ingerir()
    f = ent.ficha(R1)
    assert (f['firma'], f['tipo'], f['tamano'], f['sin_regla']) == ('Apex', 'evaluacion', 50000, False)   # patrón del ID físico
    assert f['retiros'] == 0 and f['ancla_inicial'] is None and f['ancla_estimada'] is True
    assert f['alta'] == '2026-10-28'                                       # la cuenta nace en la sesión CME del corte
    assert ent.ficha(FISICA) == FICHA                                      # la original, intacta
    assert R1 not in almacen.quemadas(con)


def test_tamano_por_el_primer_balance_tras_el_corte(tmp_path):
    """El último Bal: del reset (50.196,02) no decide el tamaño: el primero (50.000)."""
    ent, con, s = _montar(tmp_path, _reg1())
    s.ingerir()
    assert cuentas._bal_inicial(ent, con, R1) == (DESDE1, 50000.0)
    assert cuentas._bal_inicial(ent, con, FISICA)[1] == 49796.02           # la original: el último plano, como siempre
    ent.cuentas['cuentas'][R1].update(tamano=None)
    assert cuentas._completar_tamano(ent, con) and ent.ficha(R1)['tamano'] == 50000


def test_sin_herencia_de_margen_racha_ancla_threshold_manual_retiros(tmp_path):
    ent, con, s = _montar(tmp_path)
    s.ingerir()
    cuentas.retiro(ent, con, FISICA, 100.0)                                # la original ya tiene un retiro...
    con.execute('INSERT INTO threshold_manual VALUES (?, ?, ?)', (FISICA, 49000.0, 'x'))     # ...y un threshold manual
    con.commit()
    cuentas.reset(ent, con, FISICA, DESDE1)
    res, lay = s.sync(AHORA)
    bal = {b['cuenta']: b for b in res.bal}
    assert set(bal) == {FISICA, R1}                                        # fila BAL propia, con su nombre
    nuevo, viejo = bal[R1], bal[FISICA]
    assert nuevo['nombre'] == R1 and viejo['nombre'] == FISICA + '_retiros_1'
    assert nuevo['threshold_manual'] is None and viejo['threshold_manual'] == 49000.0
    assert nuevo['balance'] == 50392.04                                    # 50.000 + sus dos +196,02: ni los -203,98 ni el retiro
    assert nuevo['threshold'] == round(50392.04 - 2000, 2)                 # ancla estimada del tamaño: pico - drawdown
    assert 'ANCLA ESTIMADA' in nuevo['obs'].upper()
    f1, f2 = sorted((f for f in res.filas if f.cuenta == R1), key=lambda f: f.t_apertura_utc)
    assert f1.margen_apertura == 500.0                                     # margen inicial 500 (la original no lo condiciona)
    assert f1.res_anterior is None and f2.res_anterior == f1.ticks         # sin "resultado anterior" de la vida anterior
    assert (f1.racha, f2.racha) == (1, 2)                                  # la racha de la original (-1) no cuenta
    assert [f.racha for f in res.filas if f.cuenta == FISICA] == [-1]
    assert nuevo['margen'] == 500 + 8 + 70 + 8                             # 500 + op1 (40 t × 0,2) + bono (neto ≥ 100 $) + op2
    assert viejo['margen'] != nuevo['margen']


def test_la_original_deja_de_descuadrar(tmp_path):
    """El Bal:50000 del corte (y los siguientes) descuadran a la original si no hay reset; con él, queda en su balance viejo."""
    def base_vieja(con):         # la base de la original, guardada antes del reset (como en producción: 01/10)
        con.execute('INSERT INTO cuentas_estado VALUES (?, ?, ?)', (FISICA, 49796.02, '2026-10-27 15:00:41.200'))
        con.commit()
    ent, con, s = _montar(tmp_path)
    s.ingerir()
    base_vieja(con)
    sin, _ = s.sync(AHORA)
    assert any('DESCUADRE' in b['obs'] for b in sin.bal)
    ent2, con2, s2 = _montar(tmp_path / 'r', _reg1())
    s2.ingerir()
    base_vieja(con2)
    con_reset, _ = s2.sync(AHORA)
    bal = {b['cuenta']: b for b in con_reset.bal}
    assert bal[FISICA]['balance'] == 49796.02 and 'DESCUADRE' not in bal[FISICA]['obs']
    assert 'DESCUADRE' not in bal[R1]['obs']


def test_original_quemada_sigue_quemada_y_el_reset_no(tmp_path):
    ent, con, s = _montar(tmp_path)
    s.ingerir()
    cuentas.quemar(ent, con, FISICA, 'LOGRADA (operador)')
    antes = [tuple(r) for r in con.execute('SELECT * FROM cuentas_quemadas')]
    cuentas.reset(ent, con, FISICA, DESDE1)
    assert [tuple(r) for r in con.execute('SELECT * FROM cuentas_quemadas')] == antes     # ni quema ni desquema
    res, _ = s.sync(AHORA)
    assert [b['cuenta'] for b in res.bal] == [R1] and FISICA in almacen.quemadas(con) and R1 not in almacen.quemadas(con)


# ===========================================================================
# ID corto de operación
# ===========================================================================

def test_id_corto_de_operacion(tmp_path):
    R = config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'), {})
    for cuenta, corta in ((R1, 'AP74R1'), (OTRA_R1, 'AP75R1'), ('APEX-91120-74-reset12', 'AP74R12'),
                          ('PA-APEX-91120-15-reset1', 'PA15R1'), ('fsern2776tcp25d152250-reset2', 'E2T250R2'),
                          ('BX63983-50-reset1', 'BX50R1'), (FISICA, 'AP74')):
        assert motor.cuenta_corta(cuenta, R) == corta
    assert motor.cuenta_corta_por_patron('livesimT_franeznaco50e-reset1', R) is None         # sin patrón: RESET1 para todas
    assert motor.cuenta_corta('livesimT_franeznaco50e-reset1', R) == 'RESET1'
    ent, con, s = _montar(tmp_path, _reg1())
    s.ingerir()
    res, _ = s.sync(AHORA)
    ids = sorted(f.op_id for f in res.filas)
    assert ids == ['AP74-20261027-001', 'AP74R1-20261028-001', 'AP74R1-20261029-001']
    assert len(set(ids)) == len(ids)


def test_operaciones_ya_anotadas_tras_el_corte_cambian_de_id_y_se_avisa(tmp_path):
    """Reset registrado DESPUÉS de operar con la cuenta nueva: esas operaciones tenían ID de la original (AP74-…)."""
    ent, con, s = _montar(tmp_path)
    s.ingerir()
    res, _ = s.sync(AHORA)
    assert sorted(f.op_id for f in res.filas) == ['AP74-20261027-001', 'AP74-20261028-001', 'AP74-20261029-001']
    nombre, _, cambian = cuentas.reset(ent, con, FISICA, DESDE1)
    assert cambian == ['AP74-20261028-001', 'AP74-20261029-001']
    res, _ = s.sync(AHORA)
    assert sorted(f.op_id for f in res.filas) == ['AP74-20261027-001', 'AP74R1-20261028-001', 'AP74R1-20261029-001']


# ===========================================================================
# Copias por simultaneidad
# ===========================================================================

def _fila(cuenta, ms):
    from datetime import datetime, timedelta
    t = datetime(2026, 10, 28, 20, 6, 59) + timedelta(milliseconds=ms)
    t_exch = t.strftime('%Y-%m-%d %H:%M:%S.') + f'{t.microsecond // 1000:03d}'
    e = {'cuenta': cuenta, 'simbolo': NQ, 'fill_id': f'{cuenta}{ms}', 'order_id': 'o', 'lado': 'Buy', 'precio': 30000.0,
         'volumen': 1.0, 't_utc': t_exch[:19] + '.000', 't_exch': t_exch, 't_log': None, 'fichero': None, 'pos': None,
         't_c': None}
    f = motor.Fila(cuenta=cuenta, simbolo=NQ, direccion=1, order_id='o')
    f.entradas = [e]
    return f


def test_la_cuenta_nueva_es_otra_cuenta_para_las_copias():
    """Dos aperturas del mismo lado e instrumento a ≤ 1 s, una a cada lado del corte, son de cuentas distintas (original y
    reset1): la posterior es copia de la anterior. Antes del reset (misma cuenta) no habría sido copia de sí misma."""
    R = config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'), {})
    a, b = _fila(FISICA, 0), _fila(R1, 400)
    motor.emparejar_copias([a, b], resets.expandir_pares(_reg1(), []), R)
    assert b.lider is a and a.copias == [b]
    a, b = _fila(FISICA, 0), _fila(FISICA, 400)
    motor.emparejar_copias([a, b], [], R)
    assert b.lider is None and a.copias == []                                # misma cuenta: no hay copia


def test_el_lider_del_cnf_figura_tambien_con_el_id_logico():
    """E2T-250 figura como líder (de APEX-83): lo es de la original y del reset, y gana al que abrió antes."""
    R = config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'), {})
    reg = {FISICA: [{'desde': DESDE1, 'nombre': R1}]}
    pares = resets.expandir_pares(reg, [(FISICA, 'APEX-91120-83')])
    otra, lider = _fila('ZZZ', 0), _fila(R1, 300)
    motor.emparejar_copias([otra, lider], pares, R)
    assert otra.lider is lider and lider.lider is None
    pares_sin = [(FISICA, 'APEX-91120-83')]                                   # sin expandir (lo que habría pasado)
    otra, lider = _fila('ZZZ', 0), _fila(R1, 300)
    motor.emparejar_copias([otra, lider], pares_sin, R)
    assert lider.lider is otra


# ===========================================================================
# CLI: reset <cuenta física> <desde>
# ===========================================================================

@pytest.fixture
def cli(tmp_path, monkeypatch):
    ent, con, s = _montar(tmp_path)
    s.ingerir()
    llamadas = []
    monkeypatch.setattr(cli_v4.config, 'cargar_entorno', lambda *a, **k: ent)
    monkeypatch.setattr(cli_v4.almacen, 'conectar', lambda *_: con)
    monkeypatch.setattr(cli_v4, 'demonio_activo', lambda nombre: False)
    monkeypatch.setattr(cli_v4, 'forzar_sync', lambda e, c: llamadas.append('sync'))
    return ent, con, llamadas


def test_cli_registra_reasigna_y_sincroniza(cli, capsys):
    ent, con, llamadas = cli
    cli_v4.main(['reset', FISICA, DESDE1])
    assert 'Reset registrado: ' + R1 in capsys.readouterr().out
    assert llamadas == ['sync'] and _por_cuenta(con, 'fills') == {FISICA: 2, R1: 4}
    guardado = yaml.safe_load(open(ent.cuentas_path, encoding='utf-8'))
    assert guardado['resets'] == _reg1()
    assert guardado['cuentas'][FISICA] == FICHA                              # la ficha de la original no se toca
    cli_v4.main(['reset', FISICA] + DESDE2.split(' '))                       # sin comillas (dos argumentos) y N automático
    assert [r['nombre'] for r in ent.resets[FISICA]] == [R1, R2]


def test_cli_validaciones(cli):
    ent, con, llamadas = cli
    for args, texto in ((['reset', 'NO-EXISTE', DESDE1], 'Cuenta desconocida'),
                        (['reset', FISICA, 'ayer'], 'Hora no válida')):
        with pytest.raises(SystemExit) as e:
            cli_v4.main(args)
        assert texto in str(e.value)
    assert not ent.resets and llamadas == []
    cli_v4.main(['reset', FISICA, DESDE1])
    for desde in (DESDE1, '2026-10-20 10:00:00.000'):                        # no posterior al reset anterior
        with pytest.raises(SystemExit) as e:
            cli_v4.main(['reset', FISICA, desde])
        assert 'posterior al anterior' in str(e.value)
    with pytest.raises(SystemExit) as e:
        cli_v4.main(['reset', R1, DESDE2])                                   # sobre la lógica, no la física
    assert 'indica la cuenta física' in str(e.value)
    assert [r['nombre'] for r in ent.resets[FISICA]] == [R1]


def test_cli_rechaza_sim_demonio_en_marcha_y_cuenta_sin_patron_de_id_corto(cli, monkeypatch):
    ent, con, llamadas = cli
    ent.cuentas['cuentas']['fsern2776_sim'] = dict(FICHA, firma='SIM', tipo='sim')
    ent.cuentas['cuentas']['livesimT_x'] = dict(FICHA, firma='E2T', tipo='real')
    with pytest.raises(SystemExit) as e:
        cli_v4.main(['reset', 'fsern2776_sim', DESDE1])
    assert 'sim' in str(e.value)
    with pytest.raises(SystemExit) as e:
        cli_v4.main(['reset', 'livesimT_x', DESDE1])
    assert 'cuenta_corta' in str(e.value) and 'RESET1' in str(e.value)
    monkeypatch.setattr(cli_v4, 'demonio_activo', lambda nombre: True)
    with pytest.raises(SystemExit) as e:
        cli_v4.main(['reset', FISICA, DESDE1])
    assert 'demonio' in str(e.value).lower()
    assert not ent.resets and llamadas == []


def test_cli_avisa_de_las_operaciones_que_cambian_de_id(cli, capsys):
    ent, con, llamadas = cli
    s = sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)
    s.sync(AHORA)
    cli_v4.main(['reset', FISICA, DESDE1])
    out = capsys.readouterr().out
    assert 'AVISO' in out and 'AP74-20261028-001' in out
