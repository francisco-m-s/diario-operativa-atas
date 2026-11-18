"""'Order trade' como fuente de fills del log, reloj ATAS y migración (28/09/2026).

Desde ATAS 8.0.14 (24/09/2026 11:56) el log no escribe DEBUG: no hay 'Processing trade' y las operaciones salían
[SIN TELEMETRÍA]. Casos reales: fixtures/ATAS_28 (28/09/2026, extraído con extraer_fixtures.py --28) y líneas
copiadas tal cual de los logs del 23 y 24/09.
"""

import os
import shutil

import pytest

from atasv4 import almacen, config, fuentes, motor, reloj, sync
from conftest import RAIZ, HojaFalsa

AQUI = os.path.dirname(os.path.abspath(__file__))
FIXT_28 = os.path.join(AQUI, 'fixtures', 'ATAS_28')
PA15, E250 = 'PA-APEX-91120-15', 'fsern2776tcp25d152250'
AHORA = '2026-09-28 15:44:00.000'                   # 17:44 Madrid, reloj del PC
X = 'Cazas de soporte / resistencia en M17'
CUENTAS_28 = {
    'cuentas': {
        PA15: {'firma': 'Apex', 'tipo': 'PA', 'tamano': 25000, 'sin_regla': False, 'alta': '2026-09-28',
               'retiros': 0, 'ancla_inicial': 25100.0, 'ancla_estimada': False},
        E250: {'firma': 'E2T', 'tipo': 'TCP', 'tamano': 25000, 'sin_regla': False, 'alta': '2026-09-28',
               'retiros': 0, 'ancla_inicial': None, 'ancla_estimada': True},
    },
    'sim_activas': [],
}

# Líneas reales (sin tocar)
OT_28 = ('2026-09-28 17:01:17,655 [Process   ] INFO  mic[APEX-91120]: Order trade 2908067498/50343781/1149934 '
         'Sell MNQZ6@CME at 30417 vol 1.')
REG_28 = ('2026-09-28 17:01:17,648 [Process   ] INFO  mic[APEX-91120]: Order registered 2908067498/50343781 Sell '
          'Market MNQZ6@CME on PA-APEX-91120-15 at 0/0 vol 1/1 tif GTC state Active .')
INICIO_28 = '2026-09-28 13:58:45,712 [18        ] INFO  App            : Started v8.0.14.399'
REG_23 = ('2026-09-23 18:48:17,611 [Process   ] INFO  mic[APEX-91120]: Order registered 2903726005/50159635 Sell '
          'Market NQZ6@CME on APEX-91120-75 at 0/0 vol 2,0/2,0 tif GTC state Active .')
OT_23 = ('2026-09-23 18:48:17,611 [Process   ] INFO  mic[APEX-91120]: Order trade 2903726005/50159635/1539522 '
         'Sell NQZ6@CME at 30702,75 vol 1.')
PT_23 = ('2026-09-23 18:48:17,612 [Process   ] DEBUG tchingProcessor: APEX-91120-75 NQZ6@CME. Processing trade '
         '1539522 Sell 2026.09.23 16:48:16.326 1x30702,75')
REG_24 = ('2026-09-24 11:34:27,012 [Process   ] INFO  hmic[fsern2776]: Order registered 2637304164/40356133 Sell '
          'Market NQZ6@CME on fsern2776_sim at 0/0 vol 1/1 tif GTC state Active .')
OT_24 = ('2026-09-24 11:34:27,012 [Process   ] INFO  hmic[fsern2776]: Order trade 2637304164/40356133/1017505 '
         'Sell NQZ6@CME at 30434,25 vol 1.')
OT_24_REPETIDA = ('2026-09-24 11:57:35,717 [Process   ] INFO  hmic[fsern2776]: Order trade '
                  '2637304164/40356133/1017505 Sell NQZ6@CME at 30434,25 vol 1.')
DESDE = '2026-09-22 00:00:00.000'


def _entorno(tmp_path, atas_dir, respaldo=None):
    base = str(tmp_path)
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), os.path.join(base, 'reglas.yaml'))
    cfg = config.cargar_yaml(os.path.join(RAIZ, 'config.yaml'), {})
    cfg.update({'atas_dir': atas_dir, 'sqlite': os.path.join(base, 't.sqlite'), 'backups_dir': os.path.join(base, 'b'),
                'inicio_sesion': '2026-09-28', 'quemadas_iniciales': [], 'logs_respaldo': respaldo})
    config.guardar_yaml(os.path.join(base, 'config.yaml'), cfg)
    config.guardar_yaml(os.path.join(base, 'cuentas.yaml'), CUENTAS_28)
    ent = config.cargar_entorno(base)
    con = almacen.conectar(ent.sqlite_path)
    return ent, con, sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)


@pytest.fixture
def entorno_28(tmp_path):
    ent, con, s = _entorno(tmp_path, FIXT_28)
    s.ingerir()
    yield ent, con, s
    con.close()


def _escribir_log(dir_logs, nombre, lineas, modo='w'):
    os.makedirs(dir_logs, exist_ok=True)
    with open(os.path.join(dir_logs, nombre), modo, encoding='utf-8') as f:
        f.write(''.join(l + '\n' for l in lineas))


def _con():
    return almacen.conectar(':memory:')


def _byte_de(dir_logs, nombre, linea):
    """Posición en bytes de una línea dentro del fichero (orden de línea)."""
    with open(os.path.join(str(dir_logs), nombre), 'rb') as f:
        return f.read().index(linea.encode('utf-8'))


# ===========================================================================
# 1. Ingesta de 'Order trade'
# ===========================================================================

def test_parse_order_trade_de_las_dos_versiones_y_arranque():
    assert fuentes.parse_linea(OT_28) == ('fill_ot', {
        'order_id': '2908067498', 'fill_id': '1149934', 'lado': 'Sell', 'simbolo': 'MNQZ6@CME', 'precio': 30417.0,
        'qty': 1.0, 't_log': '2026-09-28 17:01:17.655'})
    tipo, d = fuentes.parse_linea(OT_23)                 # ATAS 7: precio con coma decimal
    assert tipo == 'fill_ot' and (d['fill_id'], d['precio'], d['qty']) == ('1539522', 30702.75, 1.0)
    assert fuentes.parse_linea(INICIO_28) == ('inicio', {'version': '8.0.14.399', 't_log': '2026-09-28 13:58:45.712'})


def test_order_trade_toma_la_cuenta_de_su_orden(tmp_path):
    con = _con()
    _escribir_log(str(tmp_path), 'app_20260928.log', [REG_28, OT_28])
    assert almacen.ingerir_logs(con, str(tmp_path), DESDE) == 2          # la orden y el fill
    r = dict(con.execute('SELECT * FROM fills_log').fetchone())
    assert r == {'cuenta': PA15, 'simbolo': 'MNQZ6@CME', 'fill_id': '1149934', 'lado': 'Sell', 't_exch': None,
                 't_log': '2026-09-28 17:01:17.655', 'qty': 1.0, 'precio': 30417.0, 'order_id': '2908067498',
                 'fichero': 'app_20260928.log', 'pos': _byte_de(tmp_path, 'app_20260928.log', OT_28)}  # orden de línea
    assert con.execute('SELECT COUNT(*) FROM fills_ot_pendientes').fetchone()[0] == 0


def test_order_trade_antes_que_su_orden_no_se_pierde(tmp_path):
    """El fill llega en una lectura y su 'Order registered' en la siguiente: queda pendiente en SQLite (el offset del
    log ya pasó la línea) y se resuelve en la ingesta posterior."""
    con = _con()
    d = str(tmp_path)
    _escribir_log(d, 'app_20260928.log', [OT_28])
    assert almacen.ingerir_logs(con, d, DESDE) == 0
    assert con.execute('SELECT COUNT(*) FROM fills_log').fetchone()[0] == 0
    assert con.execute('SELECT fill_id FROM fills_ot_pendientes').fetchone()[0] == '1149934'
    _escribir_log(d, 'app_20260928.log', [REG_28], modo='a')
    assert almacen.ingerir_logs(con, d, DESDE) == 2                       # la orden y el fill pendiente
    assert con.execute('SELECT cuenta FROM fills_log WHERE fill_id=?', ('1149934',)).fetchone()[0] == PA15
    assert con.execute('SELECT COUNT(*) FROM fills_ot_pendientes').fetchone()[0] == 0


def test_order_trade_sin_orden_en_el_log_se_resuelve_por_mytrade(tmp_path):
    """La orden se registró antes del fichero (u otra sesión): la cuenta sale del fill de MyTrade con el mismo Id."""
    con = _con()
    _escribir_log(str(tmp_path), 'app_20260928.log', [OT_28])
    almacen.ingerir_logs(con, str(tmp_path), DESDE)
    assert con.execute('SELECT COUNT(*) FROM fills_log').fetchone()[0] == 0
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)',     # fila real de MyTrade.cdb
                (PA15, 'MNQZ6@CME', '1149934', '2908067498', 'Sell', 30417.0, 1.0, '2026-09-28 15:01:10.000'))
    assert almacen.resolver_fills_ot(con) == 1
    assert con.execute('SELECT cuenta, t_log FROM fills_log').fetchone()[:] == (PA15, '2026-09-28 17:01:17.655')


def test_repeticion_al_reconectar_no_duplica_y_manda_la_primera(tmp_path):
    """24/09: ATAS repite el fill 1017505 al reconectar (11:34:27 y 11:57:35). Una fila, con la primera hora, aunque
    la repetición se lea antes (otro fichero)."""
    for orden in ([REG_24, OT_24, OT_24_REPETIDA], [REG_24, OT_24_REPETIDA, OT_24]):
        con = _con()
        d = str(tmp_path / str(len(os.listdir(str(tmp_path)))))
        _escribir_log(d, 'app_20260924.log', orden[:2])
        almacen.ingerir_logs(con, d, DESDE)
        _escribir_log(d, 'app_20260924.log', orden[2:], modo='a')
        almacen.ingerir_logs(con, d, DESDE)
        assert [tuple(r) for r in con.execute('SELECT cuenta, fill_id, t_log FROM fills_log')] == [
            ('fsern2776_sim', '1017505', '2026-09-24 11:34:27.012')]


@pytest.mark.parametrize('lineas', [[REG_23, OT_23, PT_23], [REG_23, PT_23, OT_23]])
def test_processing_trade_manda_y_order_trade_pone_la_orden(tmp_path, lineas):
    """ATAS 7 escribe los dos: la hora del exchange (ms) y la del log salen de 'Processing trade'; la orden, exacta,
    de 'Order trade'. Mismo resultado en cualquier orden y sin duplicar."""
    con = _con()
    _escribir_log(str(tmp_path), 'app_20260923.log', lineas)
    almacen.ingerir_logs(con, str(tmp_path), DESDE)
    assert [tuple(r) for r in con.execute('SELECT cuenta, fill_id, t_exch, t_log, order_id FROM fills_log')] == [
        ('APEX-91120-75', '1539522', '2026-09-23 16:48:16.326', '2026-09-23 18:48:17.612', '2903726005')]


# ===========================================================================
# 2. Reloj ATAS
# ===========================================================================

def _con_reloj(sesiones, fills):
    """fills: (cuenta, fill_id, t_log, t_exch|None, t_utc de MyTrade|None)."""
    con = _con()
    for t in sesiones:
        con.execute('INSERT INTO sesiones_atas (t_log, version) VALUES (?, ?)', (t, 'x'))
    for cuenta, fid, t_log, t_exch, t_utc in fills:
        con.execute('INSERT INTO fills_log (cuenta, simbolo, fill_id, lado, t_exch, t_log, qty, precio, '
                    'order_id) VALUES (?,?,?,?,?,?,?,?,?)',
                    (cuenta, 'NQZ6@CME', fid, 'Buy', t_exch, t_log, 1.0, 1.0, None))
        if t_utc:
            con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)',
                        (cuenta, 'NQZ6@CME', fid, 'o', 'Buy', 1.0, 1.0, t_utc))
    return con


R = config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'), {})
# 28/09 real: horas del log de 'Order trade' y de MyTrade (al segundo) de los 5 fills de PA-15 y E2T-250
FILLS_28 = [(PA15, '1149934', '2026-09-28 17:01:17.655', None, '2026-09-28 15:01:10.000'),
            (PA15, '1149334', '2026-09-28 17:01:39.226', None, '2026-09-28 15:01:31.000'),
            (PA15, '1153056', '2026-09-28 17:06:50.709', None, '2026-09-28 15:06:43.000'),
            (E250, '1004774', '2026-09-28 17:09:32.957', None, '2026-09-28 15:09:25.000'),
            (E250, '1004867', '2026-09-28 17:16:31.809', None, '2026-09-28 15:16:24.000')]
# 23/09 real: 'Processing trade' (hora del exchange con ms)
FILLS_23 = [('APEX-91120-75', '1538527', '2026-09-23 18:47:28.296', '2026-09-23 16:47:27.007', None),
            ('APEX-91120-75', '1539522', '2026-09-23 18:48:17.612', '2026-09-23 16:48:16.326', None),
            ('APEX-91120-75', '1537405', '2026-09-23 18:49:12.401', '2026-09-23 16:49:11.115', None)]


def test_reloj_por_sesion_exacto_con_ms_y_centro_del_segundo_con_mytrade():
    rel = reloj.cargar(_con_reloj(['2026-09-23 13:55:42.905', '2026-09-28 13:58:45.712'], FILLS_23 + FILLS_28), R)
    assert rel.desfases == [1.286, 7.309]                  # mediana(1,289 1,286 1,286); mediana(7,655…8,226) − 0,5
    assert [d['fuente'] for d in rel.detalle] == ['exchange (ms)', 'MyTrade (al segundo)']
    # el fill del enunciado: log 17:01:17,655 -> exchange 15:01:10,346 UTC, dentro del segundo de MyTrade (15:01:10)
    assert rel.log_a_utc('2026-09-28 17:01:17.655') == '2026-09-28 15:01:10.346'
    assert rel.log_a_utc('2026-09-23 18:48:17.612') == '2026-09-23 16:48:16.326'


def test_reloj_con_ms_manda_sobre_mytrade_en_la_misma_sesion():
    fills = [('APEX-91120-75', '1539522', '2026-09-23 18:48:17.612', '2026-09-23 16:48:16.326',
              '2026-09-23 16:48:16.000')]
    assert reloj.cargar(_con_reloj([], fills), R).desfases == [1.286]


def test_reloj_descarta_repeticiones_y_cuentas_sim():
    repetida = ('fsern2776tcp25d152250', '1', '2026-09-28 17:40:00.000', None, '2026-09-28 15:16:24.000')  # +23 min
    sim = ('fsern2776_sim', '2', '2026-09-28 17:20:00.000', '2026-09-28 15:20:00.000', None)   # 'exchange' del simulador
    rel = reloj.cargar(_con_reloj(['2026-09-28 13:58:45.712'], FILLS_28 + [repetida, sim]), R)
    assert rel.desfases == [7.309] and rel.detalle[0]['muestras'] == 5


def test_reloj_sesion_sin_fills_hereda_la_mas_cercana_y_sin_nada_por_defecto():
    sesiones = ['2026-09-24 11:56:56.343', '2026-09-28 12:23:39.307', '2026-09-28 13:58:45.712']
    fills_24 = [('APEX-91120-75', '1722524', '2026-09-24 17:50:19.622', None, '2026-09-24 15:50:20.000')]
    rel = reloj.cargar(_con_reloj(sesiones, fills_24 + FILLS_28), R)
    assert rel.desfases == [-0.878, 7.309, 7.309]          # 12:23 sin fills: la de 13:58 (1,6 h), no la del 24/09
    assert rel.detalle[1]['fuente'] == 'heredado de la sesión 2026-09-28 13:58:45.712'
    assert rel.desfase('2026-09-28 12:00:00.000') == -0.878      # antes del arranque de las 12:23: sesión anterior
    vacio = reloj.cargar(_con_reloj(sesiones, []), R)
    assert vacio.desfases == [R['reloj']['desfase_por_defecto_s']] * 3
    assert reloj.cargar(_con(), R).log_a_utc('2026-09-28 17:00:00.000') == '2026-09-28 15:00:00.000'


def test_reloj_ida_y_vuelta_y_reloj_del_pc():
    rel = reloj.cargar(_con_reloj(['2026-09-28 13:58:45.712'], FILLS_28), R)
    assert rel.utc_a_log(rel.log_a_utc('2026-09-28 17:09:32.957')) == '2026-09-28 17:09:32.957'
    assert rel.pc_a_utc('2026-09-28 15:09:31.309') == '2026-09-28 15:09:24.000'
    assert rel.a_exch('2026-09-28 17:09:32.957') == '2026-09-28 17:09:25.648'


# ===========================================================================
# Caso real del 28/09 (falla sin el cambio: todo salía [SIN TELEMETRÍA])
# ===========================================================================

def _filas(res):
    return {f.op_id: f for f in res.filas}


def test_caso_real_28_09_recupera_la_telemetria(entorno_28):
    ent, con, s = entorno_28
    f = _filas(s.calcular(AHORA))
    assert set(f) == {'PA15-20260928-001', 'PA15-20260928-002', 'E2T250-20260928-001'}
    assert all(x.telemetria for x in f.values())
    e = f['E2T250-20260928-001']
    assert (e.mae, e.mfe, e.sl_ini, e.tp_final, e.tipo_apertura, e.tipo_cierre) == (-57, 88, 82, 88, 'Mercado', 'TP')
    assert 'SL 82→-1→-38, TP 81→88' in e.obs and ent.reglas['observaciones']['sin_telemetria'] not in e.obs
    p1, p2 = f['PA15-20260928-001'], f['PA15-20260928-002']
    assert (p1.mae, p1.mfe, p1.sl_ini, p1.tp_final, p1.tipo_apertura, p1.tipo_cierre) == (-207, 0, 136, 120, 'Mercado', 'SL')
    assert (p2.sl_ini, p2.tp_final, p2.tipo_apertura, p2.tipo_cierre) == (119, 153, 'Límite', 'SL')
    # el margen ya resta el MAE: 500 + (−152·0,2 − 207) + (−119·0,2 − 207)
    assert p2.margen_tras == pytest.approx(500 + (-152 * 0.2 - 207) + (-119 * 0.2 - 207))
    # horas y dinero, de MyTrade/HistoryMyTrade como antes
    assert (e.t_apertura_utc, e.t_cierre_utc, e.neto) == ('2026-09-28 15:09:25.000', '2026-09-28 15:16:24.000', 435.92)


def test_bal_plano_con_la_posicion_abierta_antes_que_el_fill(entorno_28):
    """ATAS 8/E2T: 'Position changed … vol -1' (17:09:32,945) y un 'Bal:' ya con la posición (,946) llegan antes que
    el 'Order trade' (,957); el 'Bal:' plano sale en el mismo ms que el fill de salida (17:16:31,809). Con los fills
    solos, el BAL de E2T-250 daba DESCUADRE −22,20 (log 24.844,40)."""
    ent, con, s = entorno_28
    assert almacen.ultimo_bal_plano(con, E250) == ('2026-09-28 17:16:31.809', 25302.36)
    assert almacen.ultimo_bal_plano(con, E250, hasta='2026-09-28 17:15:00.000') == (None, None)   # siempre abierta
    # balances base antes de operar: PA-15, su 'Bal:' plano de las 14:05:56; E2T-250, el de la base real (24.866,60)
    con.execute('INSERT INTO cuentas_estado VALUES (?, ?, ?)', (PA15, 25670.28, '2026-09-28 14:05:56.398'))
    con.execute('INSERT INTO cuentas_estado VALUES (?, ?, ?)', (E250, 24866.60, '2026-09-28 17:00:00.000'))
    con.commit()
    bal = {b['cuenta']: b for b in s.calcular(AHORA).bal}
    assert (bal[PA15]['balance'], bal[E250]['balance']) == (25532.74, 25302.52)
    assert 'DESCUADRE' not in bal[E250]['obs'] and 'DESCUADRE' not in bal[PA15]['obs']


def test_fill_solo_en_el_log_usa_la_hora_del_exchange_estimada(entorno_28):
    """Antes de que ATAS escriba MyTrade, la apertura sale de 'Order trade' corregida con el reloj ATAS."""
    ent, con, s = entorno_28
    con.execute('DELETE FROM fills WHERE cuenta=?', (E250,))
    con.commit()
    assert reloj.cargar(con, ent.reglas).desfases == [7.209]       # solo los 3 fills de PA-15: 7,709 − 0,5
    e = _filas(s.calcular(AHORA))['E2T250-20260928-001']
    assert e.t_apertura_utc == '2026-09-28 15:09:25.748'   # log 17:09:32,957 − 7,209 s; MyTrade: 15:09:25 (truncada)
    assert e.t_cierre_utc == '2026-09-28 15:16:24.600'     # log 17:16:31,809 − 7,209 s; MyTrade: 15:16:24
    assert e.telemetria and e.mae == -57


def test_mismo_segundo_de_mytrade_ordena_la_hora_del_log_y_no_el_id():
    """Sin ms del exchange (ATAS 8), dos fills del mismo segundo de MyTrade se ordenan por la hora del log corregida:
    los Id no son cronológicos (el 28/09, 1149334 llegó 21 s después que 1149934). Entrada y salida inventadas en el
    mismo segundo con esos dos Id."""
    rel = reloj.Reloj(['2026-09-28 13:58:45.712'], [7.309])

    def fill(fid, lado, px, t_log):
        return {'cuenta': PA15, 'simbolo': 'MNQZ6@CME', 'fill_id': fid, 'order_id': fid, 'lado': lado, 'precio': px,
                'volumen': 1.0, 't_utc': '2026-09-28 15:01:10.000', 't_exch': None, 't_log': t_log}
    fills = [fill('1149934', 'Sell', 30417.0, '2026-09-28 17:01:17.655'),
             fill('1149334', 'Buy', 30416.0, '2026-09-28 17:01:17.955')]
    filas, pos = motor.construir_filas(fills, reloj=rel)
    assert pos == 0 and [(f.direccion, f.entradas[0]['fill_id']) for f in filas] == [(-1, '1149934')]


def _atas_a_las(tmp_path, t_log):
    """Copia de fixtures/ATAS_28 como estaba a esa hora del log: MyTrade sin los fills de E2T (ATAS los escribió a las
    17:16:32) y el log cortado."""
    d = str(tmp_path / 'ATAS')
    shutil.copytree(FIXT_28, d)
    log = os.path.join(d, 'Logs', 'app_2026092820260928.log')
    with open(log, encoding='utf-8') as f:
        lineas = [l for l in f.read().splitlines() if l[:23].replace(',', '.') <= t_log]
    with open(log, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lineas) + '\n')
    for cdb in ('MyTrade.cdb', 'HistoryMyTrade.cdb'):
        p = os.path.join(d, 'Database', cdb)
        with open(p, encoding='utf-8') as f:
            texto = [l for l in f.read().splitlines() if E250 not in l]
        with open(p, 'w', encoding='utf-8') as f:
            f.write('\n'.join(texto) + '\n')
    return d


class _Avisos(list):
    def __call__(self, titulo, texto):
        self.append(texto)


def test_caso_real_28_09_aviso_en_vivo_de_la_entrada_de_e2t(tmp_path):
    """17:09:32 (reloj del PC): E2T-250 entra con PA-15 recién cerrada en pérdida (17:06:43 del exchange). Con solo
    MyTrade el aviso no sonó (ATAS escribió el fill a las 17:16:32, pasados los 300 s). Con 'Order trade' suena en el
    ciclo siguiente."""
    ent, con, s = _entorno(tmp_path, _atas_a_las(tmp_path, '2026-09-28 17:09:40.000'))
    s.ingerir()
    avisos = _Avisos()
    s.vigilar(avisos, '2026-09-28 15:09:40.000')           # 17:09:40 del reloj del PC
    # 30/09/2026 (spec §4.5 y §9.4): E2T-250 es una prueba, así que el evento se detecta en este mismo ciclo y queda en
    # avisos_emitidos, pero no suena ni notifica (antes se comprobaba en los avisos sonoros)
    emitidos = {r['texto']: r['notificado'] for r in con.execute('SELECT texto, notificado FROM avisos_emitidos')}
    for texto in ('INFRACCIÓN: REENTRADA TRAS PÉRDIDA (fsern2776tcp25d152250 NQZ6). No operar hasta 17:16:55',
                  'INFRACCIÓN: SOBREOPERACIÓN 10 MIN (fsern2776tcp25d152250 NQZ6). No operar hasta 17:11:10'):
        assert emitidos.get(texto) == 0 and texto not in avisos
    con.close()


def test_caso_real_28_09_preoperativa_con_el_reloj_del_pc_adelantado(entorno_28):
    """El PC va +7,3 s: la estrategia elegida 1 s antes de la entrada de E2T-250 (15:09:24 del exchange) se lee a las
    15:09:31,309 del PC. Con el reloj del PC salía tardía (6,3 s > 2 s de tolerancia)."""
    ent, con, s = entorno_28
    res = s.calcular(AHORA)
    for clave, valor in (('preop_estrategia', X), ('preop_t_elegida', '2026-09-28 15:09:31.309'),
                         ('preop_eleccion_desconocida', '')):
        almacen.set_estado(con, clave, valor)
    con.commit()
    assert s._asignar_estrategia(res, AHORA) == ent.reglas['estrategia_por_defecto']
    asig = {r['op_id']: dict(r) for r in con.execute('SELECT * FROM estrategia_asignada')}
    assert set(asig) == {'E2T250-20260928-001'}            # las de PA-15 cerraron antes de elegir
    a = asig['E2T250-20260928-001']
    assert (a['tardia'], a['t_elegida_utc'], a['t_elegida_exch_utc']) == (
        0, '2026-09-28 15:09:31.309', '2026-09-28 15:09:24.000')


# ===========================================================================
# 3. Migración retroactiva
# ===========================================================================

def _cuentas_tablas(con):
    return {t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            for t in ('fills_log', 'ordenes', 'cancelaciones', 'equity', 'posiciones_hist', 'sesiones_atas')}


def test_migracion_relee_los_logs_ya_leidos_una_vez_y_sin_duplicar(tmp_path):
    ent, con, s = _entorno(tmp_path, FIXT_28)
    s.ingerir()
    completo = _cuentas_tablas(con)
    assert completo['fills_log'] == 5 and completo['sesiones_atas'] == 1
    # estado de una base del código anterior: logs leídos hasta el final, sin 'Order trade' ni arranques
    con.execute('DELETE FROM fills_log')
    con.execute('DELETE FROM sesiones_atas')
    almacen.set_estado(con, almacen.MIGRACION_RELECTURA, '0')
    con.commit()
    s.ingerir()
    assert _cuentas_tablas(con)['fills_log'] == 0            # sin migración, los offsets impiden releer
    s2 = sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)   # arranque del demonio
    s2.ingerir()
    assert _cuentas_tablas(con) == completo
    assert almacen.migrar_relectura(con, ent.logs_dir, None, s2.desde_madrid) == (0, [])   # ya hecha: no hace nada
    assert con.execute('SELECT COUNT(*) FROM log_offsets').fetchone()[0] == 1
    s2.ingerir()
    assert _cuentas_tablas(con) == completo
    assert all(f.telemetria for f in s2.calcular(AHORA).filas)


def test_la_columna_nueva_se_anade_al_arrancar_y_no_al_conectar(tmp_path):
    """cli_v4.py abre la base con el demonio en marcha: conectar() no puede cambiar la tabla en la que el demonio
    anterior inserta 5 valores por posición."""
    import sqlite3
    p = str(tmp_path / 'anterior.sqlite')
    c = sqlite3.connect(p)
    c.execute('CREATE TABLE estrategia_asignada (op_id TEXT PRIMARY KEY, estrategia TEXT, t_elegida_utc TEXT, '
              't_asignada_utc TEXT, tardia INTEGER)')
    c.commit()
    c.close()
    con = almacen.conectar(p)

    def columnas():
        return [r['name'] for r in con.execute('PRAGMA table_info(estrategia_asignada)')]

    assert 't_elegida_exch_utc' not in columnas()
    con.execute('INSERT OR REPLACE INTO estrategia_asignada VALUES (?, ?, ?, ?, ?)', ('A', X, 't', 't', 0))
    almacen.migrar_estrategia_asignada(con)
    almacen.migrar_estrategia_asignada(con)                 # idempotente
    assert columnas()[-1] == 't_elegida_exch_utc'
    con.close()


def test_migracion_lee_de_la_copia_los_logs_que_atas_ya_borro(tmp_path):
    """El log del 28/09 ya no está en ATAS\\Logs (rotado): se lee de la copia de logs_respaldo."""
    atas = str(tmp_path / 'ATAS')
    shutil.copytree(FIXT_28, atas)
    shutil.rmtree(os.path.join(atas, 'Logs'))
    os.makedirs(os.path.join(atas, 'Logs'))
    ent, con, s = _entorno(tmp_path, atas, respaldo=os.path.join(FIXT_28, 'Logs'))
    s.ingerir()
    assert _cuentas_tablas(con)['fills_log'] == 5
    assert con.execute('SELECT COUNT(*) FROM log_offsets').fetchone()[0] == 0   # la copia no deja offsets
    assert all(f.telemetria for f in s.calcular(AHORA).filas)
    con.close()
