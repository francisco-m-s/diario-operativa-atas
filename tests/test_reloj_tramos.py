"""Reloj por tramos, orden de línea y el resto de hallazgos de la verificación del 28/09 (29/09/2026).

A1 (ALTO): tras un salto del reloj del PC con ATAS abierto (w32time, 28/09 17:32:17: −7,131 s) el desfase por sesión se
quedaba viejo; un fill que solo estaba en el log se fechaba 7 s antes, adelantaba a otro real de MyTrade y un largo se
convertía en un corto fantasma con ID permanente, con avisos falsos y "NO OPERAR HASTA" 7 s antes.
M1 (MEDIO): ATAS 8/Apex escribe un 'Bal:' de transición en el mismo ms que la vuelta a plano, ANTES de ella: DESCUADRE
falso. B1, B2, B4, B5 y #12: en cada test.
Casos reales: fixtures/ATAS_28_salto (extraer_fixtures.py --salto), fixtures/ATAS_28 y líneas copiadas tal cual de los
logs del 21, 23, 24 y 28/09.
"""

import os
import shutil
import sqlite3
from datetime import date, timedelta

import pytest

from atasv4 import almacen, config, fuentes, motor, reloj, sync
from conftest import RAIZ, HojaFalsa

AQUI = os.path.dirname(os.path.abspath(__file__))
FIXT_SALTO = os.path.join(AQUI, 'fixtures', 'ATAS_28_salto')
FIXT_28 = os.path.join(AQUI, 'fixtures', 'ATAS_28')
LOG_28 = 'app_2026092820260928.log'
AP75, PA15, PA16, E250 = 'APEX-91120-75', 'PA-APEX-91120-15', 'PA-APEX-91120-16', 'fsern2776tcp25d152250'
R = config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'), {})
CUENTAS = {'cuentas': {
    AP75: {'firma': 'Apex', 'tipo': 'evaluacion', 'tamano': 50000, 'sin_regla': False, 'alta': '2026-09-28',
           'retiros': 0, 'ancla_inicial': None, 'ancla_estimada': False},
    PA15: {'firma': 'Apex', 'tipo': 'PA', 'tamano': 25000, 'sin_regla': False, 'alta': '2026-09-28', 'retiros': 0,
           'ancla_inicial': 25100.0, 'ancla_estimada': False},
    E250: {'firma': 'E2T', 'tipo': 'TCP', 'tamano': 25000, 'sin_regla': False, 'alta': '2026-09-28', 'retiros': 0,
           'ancla_inicial': None, 'ancla_estimada': True}},
    'sim_activas': []}

# Líneas reales (sin tocar)
LAT_RITHMIC_28 = ('2026-09-28 17:32:17,569 [Process   ] INFO  Latency manager: Rithmic[APEX-91120] time: 28.09.2026 '
                  '15:32:17.569555 event: 28.09.2026 15:32:17.497232 diff: -00:00:07.1312993 delay: -00:00:07.0589761')
LAT_BINANCE_28 = ('2026-09-28 17:32:17,557 [Process   ] INFO  Latency manager: Binance time: 28.09.2026 15:32:17.557001 '
                  'event: 28.09.2026 15:32:17.432000 diff: -00:00:07.1339539 delay: -00:00:07.0089522')
LAT_BINANCE_23 = ('2026-09-23 18:10:43,826 [Process   ] INFO  Latency manager: Binance time: 23.09.2026 16:10:43.826874 '
                  'event: 23.09.2026 16:10:43.821390 diff: -00:00:01.2814228 delay: -00:00:01.2759381')
INICIO_28 = '2026-09-28 13:58:45,712 [18        ] INFO  App            : Started v8.0.14.399'


def _entorno(tmp_path, atas_dir, cuentas=CUENTAS, inicio='2026-09-28', respaldo=None, log=None):
    base = str(tmp_path / 'base')
    os.makedirs(base, exist_ok=True)
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), os.path.join(base, 'reglas.yaml'))
    cfg = config.cargar_yaml(os.path.join(RAIZ, 'config.yaml'), {})
    cfg.update({'atas_dir': atas_dir, 'sqlite': os.path.join(base, 't.sqlite'), 'backups_dir': os.path.join(base, 'b'),
                'inicio_sesion': inicio, 'quemadas_iniciales': [], 'logs_respaldo': respaldo})
    config.guardar_yaml(os.path.join(base, 'config.yaml'), cfg)
    config.guardar_yaml(os.path.join(base, 'cuentas.yaml'), cuentas)
    ent = config.cargar_entorno(base)
    con = almacen.conectar(ent.sqlite_path)
    return ent, con, sync.Sincronizador(ent, con, HojaFalsa(), log=log if log is not None else (lambda m: None))


def _atas_hasta(tmp_path, fixt, t_log, quitar_de_cdb=()):
    """Copia del fixture como estaba a esa hora del log: log cortado y, en los cdb, sin las filas que contengan
    alguno de los textos de `quitar_de_cdb` (lo que ATAS aún no había escrito)."""
    d = str(tmp_path / 'ATAS')
    shutil.copytree(fixt, d)
    for nombre in os.listdir(os.path.join(d, 'Logs')):
        p = os.path.join(d, 'Logs', nombre)
        with open(p, encoding='utf-8') as f:
            lineas = [l for l in f.read().splitlines() if l[:23].replace(',', '.') <= t_log]
        with open(p, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lineas) + '\n')
    for cdb in ('MyTrade.cdb', 'HistoryMyTrade.cdb'):
        p = os.path.join(d, 'Database', cdb)
        with open(p, encoding='utf-8') as f:
            texto = [l for l in f.read().splitlines() if not any(x in l for x in quitar_de_cdb)]
        with open(p, 'w', encoding='utf-8') as f:
            f.write('\n'.join(texto) + '\n')
    return d


class _Avisos(list):
    def __call__(self, titulo, texto):
        self.append(texto)


# ===========================================================================
# Fuentes: 'Latency manager' y posición de línea
# ===========================================================================

def test_parse_latency_manager():
    tipo, d = fuentes.parse_linea(LAT_RITHMIC_28)
    assert tipo == 'latencia'
    assert (d['fuente'], d['t_log'], d['t_pc_utc'], d['t_evento_utc']) == (
        'Rithmic[APEX-91120]', '2026-09-28 17:32:17.569', '2026-09-28 15:32:17.569', '2026-09-28 15:32:17.497')
    assert d['diff_s'] == pytest.approx(-7.1312993) and d['delay_s'] == pytest.approx(-7.0589761)
    tipo, d = fuentes.parse_linea(LAT_BINANCE_23)
    assert (tipo, d['fuente'], d['delay_s']) == ('latencia', 'Binance', pytest.approx(-1.2759381))
    assert fuentes._intervalo_s('1.02:03:04.5') == pytest.approx(93784.5)


def test_leer_incremental_da_la_posicion_en_bytes(tmp_path):
    """Posición = byte de inicio de la línea, también con CRLF, acentos y lectura en dos veces."""
    p = str(tmp_path / 'app_x.log')
    with open(p, 'wb') as f:
        f.write('línea uno\r\nlínea dos\nincomple'.encode('utf-8'))
    lineas, off = fuentes.leer_incremental(p, 0)
    assert lineas == [(0, 'línea uno'), (len('línea uno\r\n'.encode('utf-8')), 'línea dos')]
    with open(p, 'ab') as f:
        f.write('ta\nfin\n'.encode('utf-8'))
    lineas2, _ = fuentes.leer_incremental(p, off)
    assert lineas2 == [(off, 'incompleta'), (off + len(b'incompleta\n'), 'fin')]


# ===========================================================================
# Reloj por tramos
# ===========================================================================

def test_salto_con_atas_abierto_parte_la_sesion_en_dos_tramos(tmp_path):
    """28/09 real: una sola sesión de ATAS (13:58:45) y w32time corrige el PC a las 17:32:17. Antes del salto, lo
    que ATAS medía (−diff = 7,131); después, time − event de Rithmic (0,072). Las muestras de MyTrade no cuentan con
    exactas en el tramo. Las líneas de Binance (16:43:40 y 17:32:17,557) no cortan."""
    ent, con, s = _entorno(tmp_path, FIXT_SALTO)
    s.ingerir()
    rel = reloj.cargar(con, ent.reglas)
    assert [(d['inicio'], d['tipo'], d['desfase_s'], d['fuente']) for d in rel.detalle] == [
        ('2026-09-28 13:58:45.712', 'inicio', 7.131, 'exchange (ms)'),
        ('2026-09-28 17:32:17.569', 'salto', 0.072, 'exchange (ms)')]
    assert rel.a_exch('2026-09-28 17:06:50.709') == '2026-09-28 17:06:43.578'      # MyTrade: 15:06:43 UTC
    assert rel.a_exch('2026-09-28 18:27:40.382') == '2026-09-28 18:27:40.310'      # MyTrade: 16:27:40 UTC


def test_binance_no_corta_ni_mide():
    """23/09 18:10:43: Binance da delay −1,28 s sin que el PC salte (Rithmic mide +1,3 s a los dos lados)."""
    con = almacen.conectar(':memory:')
    almacen._guardar_evento(con, 'latencia', {**fuentes.parse_linea(LAT_BINANCE_23)[1], 'fichero': 'f', 'pos': 10}, {})
    rel = reloj.cargar(con, R)
    assert [c.tipo for c in rel.cortes] == ['inicio'] and rel.desfases == [0.0]


def test_linea_escrita_antes_del_salto_es_de_su_tramo_por_su_posicion():
    """Tras el salto el reloj del PC repite 7 s: 17:32:20 puede ser de antes (línea anterior al corte en su fichero)
    o de después. Sin posición, la hora decide (tramo posterior)."""
    rel = reloj.Reloj([reloj.Corte('2026-09-28 13:58:45.712', LOG_28, 100, 'inicio'),
                       reloj.Corte('2026-09-28 17:32:17.569', LOG_28, 5000, 'salto')], [7.131, 0.072],
                      margen_salto_s=1.0)
    assert rel.a_exch('2026-09-28 17:32:20.000', LOG_28, 4000) == '2026-09-28 17:32:12.869'
    assert rel.a_exch('2026-09-28 17:32:20.000', LOG_28, 6000) == '2026-09-28 17:32:19.928'
    assert rel.a_exch('2026-09-28 17:32:20.000') == '2026-09-28 17:32:19.928'
    assert rel.a_exch('2026-09-28 17:32:20.000', 'otro.log', 4000) == '2026-09-28 17:32:19.928'
    assert rel.ambiguedad_s == pytest.approx(7.059 + 1)


def _con_cortes(sesiones=(), latencias=(), fills=()):
    """sesiones: t_log; latencias: (t_log, fuente, time−event s, diff s, delay s); fills: (cuenta, id, t_log, t_exch)."""
    con = almacen.conectar(':memory:')
    for i, t in enumerate(sesiones):
        con.execute('INSERT INTO sesiones_atas (t_log, version, fichero, pos) VALUES (?,?,?,?)', (t, 'x', 'f', i))
    for i, (t, fuente, te, diff, delay) in enumerate(latencias):
        t_pc = fuentes.fmt_ts(fuentes.madrid_a_utc(fuentes.parse_ts(t)))
        t_ev = fuentes.fmt_ts(fuentes.parse_ts(t_pc) - timedelta(seconds=te))
        con.execute('INSERT INTO latencias (t_log, fuente, fichero, pos, t_pc_utc, t_evento_utc, diff_s, delay_s) '
                    'VALUES (?,?,?,?,?,?,?,?)', (t, fuente, 'f', 1000 + i, t_pc, t_ev, diff, delay))
    for cuenta, fid, t_log, t_exch in fills:
        con.execute('INSERT INTO fills_log (cuenta, simbolo, fill_id, lado, t_exch, t_log, qty, precio) '
                    'VALUES (?,?,?,?,?,?,?,?)', (cuenta, 'NQZ6@CME', fid, 'Buy', t_exch, t_log, 1.0, 1.0))
    return con


def test_herencia_solo_del_mismo_dia_y_sin_salto_de_por_medio():
    """27/09 con muestras (+3 s). 28/09: arranque a las 09:00 sin muestras; salto a las 10:00 cuyo −diff no vale
    (más de desfase_max_s) y cuyo time − event da 0,05; arranque a las 11:00 sin muestras.
    09:00 no hereda del 27/09 (otro día) ni del salto (lo cruza): por defecto. 11:00 hereda del salto."""
    fills = [(AP75, '1', '2026-09-27 10:30:00.000', '2026-09-27 08:29:57.000')]
    con = _con_cortes(['2026-09-27 10:00:00.000', '2026-09-28 09:00:00.000', '2026-09-28 11:00:00.000'],
                      [('2026-09-28 10:00:00.000', 'Rithmic[APEX-91120]', 0.05, -90.0, -3.0)], fills)
    rel = reloj.cargar(con, R)
    assert [(d['inicio'], d['desfase_s'], d['fuente']) for d in rel.detalle] == [
        ('2026-09-27 10:00:00.000', 3.0, 'exchange (ms)'),
        ('2026-09-28 09:00:00.000', 0.0, 'por defecto'),
        ('2026-09-28 10:00:00.000', 0.05, 'exchange (ms)'),
        ('2026-09-28 11:00:00.000', 0.05, 'heredado del salto 2026-09-28 10:00:00.000')]
    assert not reloj._heredable(rel.cortes, 1, 0) and not reloj._heredable(rel.cortes, 1, 2)
    assert reloj._heredable(rel.cortes, 3, 2)


def test_lineas_del_mismo_salto_son_un_solo_corte():
    """21/09 21:16:02 real: tres conexiones Rithmic escriben el mismo salto en 28 ms."""
    lat = [('2026-09-21 21:16:02.009', 'Rithmic[fsern2776]', 0.064, -3.4650204, -3.4009373),
           ('2026-09-21 21:16:02.015', 'Rithmic[BX20240]', 0.070, -3.4650204, -3.3948430),
           ('2026-09-21 21:16:02.037', 'Rithmic[APEX-91120]', 0.092, -3.4650204, -3.3729598)]
    rel = reloj.cargar(_con_cortes(['2026-09-21 15:16:14.906'], lat), R)
    assert [(c.t_log, c.tipo) for c in rel.cortes] == [('2026-09-21 15:16:14.906', 'inicio'),
                                                       ('2026-09-21 21:16:02.009', 'salto')]
    assert rel.desfases == [3.465, 0.07]


# ===========================================================================
# A1: el caso real del 28/09 18:27 (APEX-75)
# ===========================================================================

POSTERIORES_A_1827 = ['1184897', '1186026', '1186584', '1188128', '1187130', '1186355', '1187198', '1187664',
                      '1188496', '1188499', '1189498', '1188767', '1188875', '1190595', '1190789', '1191329',
                      '09/28/2026 16:27:40', '09/28/2026 16:28', '09/28/2026 16:29', '09/28/2026 16:3',
                      '09/28/2026 16:4']


def test_a1_largo_real_no_se_convierte_en_corto_fantasma(tmp_path):
    """18:27:42 del PC (ya en hora): el largo 1187478 (18:27:36, ya en MyTrade) acaba de cerrarse con 1184897
    (18:27:40,382, solo en el log). Con el desfase de sesión viejo (+7,2 s) la salida se fechaba a las 16:27:33 UTC,
    antes que la entrada: corto fantasma con ID permanente, dos preventivos y "NO OPERAR HASTA 18:37:45"."""
    ent, con, s = _entorno(tmp_path, _atas_hasta(tmp_path, FIXT_SALTO, '2026-09-28 18:27:42.000', POSTERIORES_A_1827))
    s.ingerir()
    avisos = _Avisos()
    res = s.vigilar(avisos, '2026-09-28 16:27:42.000')
    ap = sorted((f for f in res.filas if f.cuenta == AP75), key=lambda f: f.t_apertura_utc)
    assert [(f.op_id, f.direccion, f.entradas[0]['fill_id'], f.salidas[0]['fill_id']) for f in ap] == [
        ('AP75-20260928-001', 1, '1187096', '1184927'), ('AP75-20260928-002', 1, '1187478', '1184897')]
    assert ap[1].t_cierre_utc == '2026-09-28 16:27:40.310' and not res.filas_abiertas
    assert [r[0] for r in con.execute("SELECT op_id FROM ops_ids WHERE cuenta=? ORDER BY op_id", (AP75,))] == [
        'AP75-20260928-001', 'AP75-20260928-002']
    # 30/09/2026 (spec §4.5): APEX-75 es una prueba, así que no suena; el preventivo se comprueba en avisos_emitidos,
    # con el mismo contrato de antes: uno solo, con la hora buena y sin el fantasma de las 18:27:46
    preventivos = [r['texto'] for r in con.execute('SELECT texto FROM avisos_emitidos WHERE clave LIKE ? AND t >= ?',
                                                   (f'PREVENTIVO|{AP75}|%', '2026-09-28 18:27:00'))]
    assert preventivos == ['NO OPERAR HASTA 18:37:52 (PÉRDIDA: NO OPERAR HASTA 18:37:52; '
                           '2 DECISIONES EN 10 MIN: NO OPERAR HASTA 18:36:57)']
    assert not [a for a in avisos if a.startswith('NO OPERAR HASTA')]


def test_a1_orden_de_linea_aunque_el_desfase_este_viejo():
    """El mecanismo, con los dos fills reales y el reloj que calculaba la versión rechazada (una sesión, +7,209 s):
    si todos los fills están en el log manda el orden de las líneas, no la hora estimada."""
    viejo = reloj.Reloj(['2026-09-28 13:58:45.712'], [7.209])
    entrada = {'cuenta': AP75, 'simbolo': 'NQZ6@CME', 'fill_id': '1187478', 'order_id': '2908340533', 'lado': 'Buy',
               'precio': 30717.75, 'volumen': 1.0, 't_utc': '2026-09-28 16:27:36.000', 't_exch': None,
               't_log': '2026-09-28 18:27:36.965', 'fichero': LOG_28, 'pos': 1000}
    salida = {'cuenta': AP75, 'simbolo': 'NQZ6@CME', 'fill_id': '1184897', 'order_id': '2908340849', 'lado': 'Sell',
              'precio': 30703.25, 'volumen': 1.0, 't_utc': viejo.log_a_utc('2026-09-28 18:27:40.382'), 't_exch': None,
              't_log': '2026-09-28 18:27:40.382', 'fichero': LOG_28, 'pos': 2000}
    assert salida['t_utc'] == '2026-09-28 16:27:33.173' < entrada['t_utc']       # la trampa: estimada antes que la real
    filas, pos = motor.construir_filas([salida, entrada], reloj=viejo)
    assert pos == 0 and [(f.direccion, f.entradas[0]['fill_id']) for f in filas] == [(1, '1187478')]


def test_a1_si_falta_alguno_en_el_log_ordena_por_la_hora_del_exchange():
    """Fills solo de MyTrade (log rotado con el demonio apagado): orden por el exchange, como antes."""
    def fill(fid, lado, t_utc):
        return {'cuenta': AP75, 'simbolo': 'NQZ6@CME', 'fill_id': fid, 'order_id': fid, 'lado': lado, 'precio': 1.0,
                'volumen': 1.0, 't_utc': t_utc, 't_exch': None, 't_log': None}
    filas, pos = motor.construir_filas([fill('9', 'Sell', '2026-09-28 16:27:40.000'),
                                        fill('1', 'Buy', '2026-09-28 16:27:36.000')])
    assert pos == 0 and [(f.direccion, f.entradas[0]['fill_id']) for f in filas] == [(1, '1')]


# ===========================================================================
# Horas corregidas: un salto dentro de una operación no la deforma
# ===========================================================================

LINEAS_SALTO_DENTRO = [
    INICIO_28,
    # entrada a las 17:32:16,000 reales, escrita ANTES del salto: el PC (+7,131 s) marca 17:32:23,1, más tarde que la
    # línea 'Latency manager' que viene detrás (el reloj del PC repite esos segundos tras el salto)
    '2026-09-28 17:32:23,130 [Process   ] INFO  mic[APEX-91120]: Order registered 900001/1 Buy Market NQZ6@CME on '
    'APEX-91120-75 at 0/0 vol 1/1 tif GTC state Active .',
    '2026-09-28 17:32:23,131 [Process   ] INFO  mic[APEX-91120]: Order trade 900001/1/800001 Buy NQZ6@CME at 30700 vol 1.',
    '2026-09-28 17:32:23,131 [Process   ] INFO  mic[APEX-91120]: Position changed APEX-91120-75/NQZ6@CME vol 1 avg 30700 '
    'pnl 0/0.',
    LAT_BINANCE_28,
    LAT_RITHMIC_28,
    # el SL, 2,4 s después de la entrada en hora real (17:32:18,400), con el PC ya en hora: 17:32:18,472
    '2026-09-28 17:32:18,472 [Process   ] INFO  mic[APEX-91120]: Order registered 900002/2 Sell Stop NQZ6@CME on '
    'APEX-91120-75 at 0/30680,00 vol 1/1 tif GTC state Active SL.',
    '2026-09-28 17:32:40,000 [Process   ] INFO  mic[APEX-91120]: Portfolio changed APEX-91120-75 Bal:50800/0/0 Avl:0 '
    'PnL:-100/0 L:1 C:0.',
    '2026-09-28 17:33:00,000 [Process   ] INFO  mic[APEX-91120]: Order registered 900003/3 Sell Market NQZ6@CME on '
    'APEX-91120-75 at 0/0 vol 1/1 tif GTC state Active .',
    '2026-09-28 17:33:00,001 [Process   ] INFO  mic[APEX-91120]: Order trade 900003/3/800002 Sell NQZ6@CME at 30710 vol 1.',
    '2026-09-28 17:33:00,001 [Process   ] INFO  mic[APEX-91120]: Position changed APEX-91120-75/NQZ6@CME vol 0 avg 0 '
    'pnl 200/0.',
]


def _atas_salto_dentro(tmp_path, abierta=False):
    """abierta: el log termina en el alta del SL (la operación sigue abierta) y MyTrade aún no tiene la salida."""
    d = tmp_path / 'ATAS'
    (d / 'Logs').mkdir(parents=True)
    (d / 'Database').mkdir()
    fin = LINEAS_SALTO_DENTRO.index(LAT_RITHMIC_28) + 2 if abierta else len(LINEAS_SALTO_DENTRO)
    (d / 'Logs' / LOG_28).write_text('\n'.join(LINEAS_SALTO_DENTRO[:fin]) + '\n', encoding='utf-8')
    cab = ('Columns: "Route", "AccountID", "SecurityId", "Id", "OrderId", "OrderDirection", "Price", "Time", "Volume", '
           '"OpenVolume", "Commission", "CommissionCurrency"\nStartData\n')
    filas = [', "APEX-91120-75", "NQZ6@CME", "800001", "900001", "Buy", "30700", "09/28/2026 15:32:16", "1", "0", "0.0", ']
    if not abierta:
        filas.append(', "APEX-91120-75", "NQZ6@CME", "800002", "900003", "Sell", "30710", "09/28/2026 15:32:59", "1", '
                     '"0", "0.0", ')
    (d / 'Database' / 'MyTrade.cdb').write_text(cab + '\n'.join(filas) + '\nEndData\n', encoding='utf-8')
    shutil.copy(os.path.join(FIXT_SALTO, 'Database', 'Playbook.cdb'), str(d / 'Database'))
    return str(d)


def test_salto_dentro_de_la_operacion_no_da_sin_sl_falso(tmp_path):
    """En horas crudas el SL (17:32:18,472) quedaba antes que la entrada (17:32:23,131): SIN SL falso y sin SL
    inicial. En hora corregida la entrada es de antes del salto por su posición (17:32:16,000) y el SL, 2,4 s después."""
    ent, con, s = _entorno(tmp_path, _atas_salto_dentro(tmp_path))
    s.ingerir()
    f = next(x for x in s.calcular('2026-09-28 15:40:00.000').filas if x.cuenta == AP75)
    assert (f.t_ent_c, f.t_sal_c) == ('2026-09-28 17:32:16.000', '2026-09-28 17:32:59.929')
    assert f.telemetria and not f.sin_sl and f.sl_ini == 80 and 'SIN SL' not in f.obs
    assert f.mae == -20                                   # 'Bal:' de las 17:32:40 (PnL −100 $, 5 $/tick)


def test_salto_dentro_de_la_operacion_sin_aviso_falso_en_vivo(tmp_path):
    """Lo mismo en vivo, con la operación abierta: a las 17:32:30 del PC la entrada lleva 13,9 s (17:32:29,928 −
    17:32:16,000) y el SL llegó a los 2,4 s. En horas crudas: 6,9 s sin SL en [23,131; 28,131] -> 'INFRACCIÓN: SIN SL'."""
    ent, con, s = _entorno(tmp_path, _atas_salto_dentro(tmp_path, abierta=True))
    s.ingerir()
    avisos = _Avisos()
    res = s.vigilar(avisos, '2026-09-28 15:32:30.000')
    assert [f.entradas[0]['fill_id'] for f in res.filas_abiertas] == ['800001']
    # APEX-75 es una prueba (30/09/2026): no suena nunca, así que el aviso falso se busca en avisos_emitidos
    assert not any('SIN SL' in r['texto'] for r in con.execute('SELECT texto FROM avisos_emitidos'))
    assert not any('SIN SL' in a for a in avisos)


# ===========================================================================
# M1: 'Bal:' de transición (orden de línea)
# ===========================================================================

def test_m1_bal_de_transicion_de_atas8_apex_no_es_plano(tmp_path):
    """PA-15, 28/09 17:06:50,710: 'Bal:25557,76' con el flotante aún dentro, en el mismo ms que el 'vol 0' y antes de
    él. Hasta el siguiente 'Bal:' (17:30:32) el BAL daba DESCUADRE +25,02."""
    ent, con, s = _entorno(tmp_path, _atas_hasta(tmp_path, FIXT_28, '2026-09-28 17:20:00.000', [E250]),
                           cuentas={'cuentas': {PA15: CUENTAS['cuentas'][PA15]}, 'sim_activas': []})
    s.ingerir()
    rel = reloj.cargar(con, ent.reglas)
    assert almacen.ultimo_bal_plano(con, PA15, reloj=rel) != ('2026-09-28 17:06:50.710', 25557.76)
    con.execute('INSERT INTO cuentas_estado VALUES (?, ?, ?)', (PA15, 25670.28, '2026-09-28 14:05:56.398'))
    con.commit()
    bal = s.calcular('2026-09-28 15:20:00.000').bal[0]
    assert bal['balance'] == 25532.74 and 'DESCUADRE' not in bal['obs']      # Portfolio.cdb: 25532.74


def test_m1_apex75_1841_bal_de_transicion_y_balance_de_portfolio(tmp_path):
    """APEX-75, 28/09 18:41:43,683: 'Bal:50189,88' antes del 'vol 0' del mismo ms (DESCUADRE −6,02 con el código
    anterior). Portfolio.cdb de ATAS: 50195,9, lo que calcula la V4."""
    ent, con, s = _entorno(tmp_path, FIXT_SALTO, cuentas={'cuentas': {AP75: CUENTAS['cuentas'][AP75]},
                                                          'sim_activas': []})
    s.ingerir()
    rel = reloj.cargar(con, ent.reglas)
    assert almacen.ultimo_bal_plano(con, AP75, reloj=rel) == ('2026-09-28 18:38:15.639', 50883.86)
    con.execute('INSERT INTO cuentas_estado VALUES (?, ?, ?)', (AP75, 50883.86, '2026-09-28 18:38:15.639'))
    con.commit()
    bal = s.calcular('2026-09-28 16:47:00.000').bal[0]
    assert bal['balance'] == 50195.9 and 'DESCUADRE' not in bal['obs']


def _con_equity(filas_pos, filas_bal):
    con = almacen.conectar(':memory:')
    for t, v, p in filas_pos:
        con.execute('INSERT INTO posiciones_hist (cuenta, simbolo, t_log, volumen, fichero, pos) VALUES (?,?,?,?,?,?)',
                    ('C', 'NQZ6@CME', t, v, None if p is None else 'f', p))
    for t, b, p in filas_bal:
        con.execute('INSERT INTO equity (cuenta, t_log, bal, pnl_abierto, pnl_cerrado, fichero, pos) '
                    'VALUES (?,?,?,?,?,?,?)', ('C', t, b, 0.0, 0.0, None if p is None else 'f', p))
    return con


def test_m1_bal_detras_de_la_vuelta_a_plano_si_es_plano_y_giro_no():
    t = '2026-09-23 18:48:17.614'
    # ATAS 7: 'Bal:' bueno DETRÁS del 'vol 0' (mismo ms)
    con = _con_equity([('2026-09-23 18:47:28.297', 1, 10), (t, 0, 20)], [(t, 111.0, 15), (t, 222.0, 25)])
    assert almacen.ultimo_bal_plano(con, 'C') == (t, 222.0)
    # ATAS 8/Apex: el único 'Bal:' del ms va DELANTE: no es plano
    con = _con_equity([('2026-09-23 18:47:28.297', 1, 10), (t, 0, 20)],
                      [('2026-09-23 18:40:00.000', 100.0, 5), (t, 111.0, 15)])
    assert almacen.ultimo_bal_plano(con, 'C') == ('2026-09-23 18:40:00.000', 100.0)
    # giro real de APEX-75 del 23/09: 'Bal:' entre el 'vol 0' y el 'vol -1' del mismo ms: sigue con posición
    con = _con_equity([('2026-09-23 18:47:28.297', 1, 10), (t, 0, 20), (t, -1, 30)],
                      [('2026-09-23 18:40:00.000', 100.0, 5), (t, 52395.3, 25)])
    assert almacen.ultimo_bal_plano(con, 'C') == ('2026-09-23 18:40:00.000', 100.0)


def test_m1_filas_sin_posicion_conservan_la_regla_anterior():
    """Filas guardadas antes del orden de línea y no releídas: el 'Bal:' del mismo ms que la vuelta a plano cuenta
    como plano, como hasta el 28/09."""
    t = '2026-09-23 18:48:17.614'
    con = _con_equity([('2026-09-23 18:47:28.297', 1, None), (t, 0, None)], [(t, 111.0, None)])
    assert almacen.ultimo_bal_plano(con, 'C') == (t, 111.0)


# ===========================================================================
# B1: copia en estado mixto
# ===========================================================================

def _fila_copia(cuenta, fid, t_utc, t_log, rel):
    e = {'cuenta': cuenta, 'simbolo': 'MNQZ6@CME', 'fill_id': fid, 'order_id': fid, 'lado': 'Sell', 'precio': 30681.0,
         'volumen': 1.0, 't_utc': t_utc, 't_exch': None, 't_log': t_log, 'fichero': 'app_20260924.log', 'pos': None,
         't_c': rel.a_exch(t_log) if t_log else None}
    f = motor.Fila(cuenta=cuenta, simbolo='MNQZ6@CME', direccion=-1, order_id=fid)
    f.entradas = [e]
    return f


def test_b1_copia_en_estado_mixto_se_empareja():
    """24/09 18:16:43 real: PA-16 (líder, log ,215; MyTrade 16:16:43) y PA-15 (copia, log ,374; MyTrade 16:16:44). Con
    la líder ya en MyTrade y la copia aún solo en el log (hora estimada con −0,747 s): 1,121 s > 1 s y la copia quedaba
    huérfana. Con horas del mismo reloj: 0,159 s."""
    rel = reloj.Reloj(['2026-09-24 11:56:56.343'], [-0.747])
    lider = _fila_copia(PA16, '1731408', '2026-09-24 16:16:43.000', '2026-09-24 18:16:43.215', rel)
    copia = _fila_copia(PA15, '1731221', rel.log_a_utc('2026-09-24 18:16:43.374'), '2026-09-24 18:16:43.374', rel)
    assert copia.entradas[0]['t_utc'] == '2026-09-24 16:16:44.121'
    assert motor._dt_aperturas(lider.entradas[0], copia.entradas[0]) == pytest.approx(0.159)
    motor.emparejar_copias([lider, copia], [(PA16, PA15)], R)
    assert copia.lider is lider
    # las dos solo en MyTrade (log rotado): segundos de MyTrade, 1 s, dentro de la ventana
    l2 = _fila_copia(PA16, 'a', '2026-09-24 16:16:43.000', None, rel)
    c2 = _fila_copia(PA15, 'b', '2026-09-24 16:16:44.000', None, rel)
    motor.emparejar_copias([l2, c2], [(PA16, PA15)], R)
    assert c2.lider is l2


# ===========================================================================
# B2: migración sin respaldo
# ===========================================================================

def test_b2_migracion_con_logs_perdidos_avisa_y_se_reintenta(tmp_path):
    ent, con, s = _entorno(tmp_path, FIXT_28)
    s.ingerir()
    # base anterior: ya había leído app_20260928.log (ATAS 7, mañana del 28/09), que ATAS borró y no está en la copia
    con.execute("INSERT INTO log_offsets VALUES ('app_20260928.log', 685884)")
    almacen.set_estado(con, almacen.MIGRACION_RELECTURA, '0')
    con.commit()
    copia = tmp_path / 'copia'
    copia.mkdir()
    ent.cfg['logs_respaldo'] = str(copia)
    log = []
    sync.Sincronizador(ent, con, HojaFalsa(), log=log.append)
    assert any('[MIGRACIÓN AVISO]' in m and 'app_20260928.log' in m for m in log)
    assert almacen.get_estado(con, almacen.MIGRACION_RELECTURA) != '1'
    # siguiente arranque: sigue faltando (ya no está en log_offsets, lo recuerda el estado) y sigue avisando
    s2 = sync.Sincronizador(ent, con, HojaFalsa(), log=log.append)
    s2.ingerir()
    assert sum('[MIGRACIÓN AVISO]' in m for m in log) == 2
    # aparece en la copia: se lee y se da por hecha
    (copia / 'app_20260928.log').write_text(INICIO_28.replace('13:58:45', '12:23:39') + '\n', encoding='utf-8')
    sync.Sincronizador(ent, con, HojaFalsa(), log=log.append)
    assert almacen.get_estado(con, almacen.MIGRACION_RELECTURA) == '1'
    assert sum('[MIGRACIÓN AVISO]' in m for m in log) == 2
    assert con.execute("SELECT COUNT(*) FROM sesiones_atas WHERE t_log LIKE '2026-09-28 12:23%'").fetchone()[0] == 1


# ===========================================================================
# B4: reloj en caché
# ===========================================================================

def test_b4_reloj_en_cache_mientras_no_cambian_sus_datos(tmp_path):
    ent, con, s = _entorno(tmp_path, FIXT_28)
    s.ingerir()
    r1 = reloj.cargar(con, ent.reglas)
    assert reloj.cargar(con, ent.reglas) is r1
    s.calcular('2026-09-28 15:44:00.000')             # escribe en otras tablas (anclas, ops_ids): no invalida
    assert reloj.cargar(con, ent.reglas) is r1
    con.execute("UPDATE fills_log SET t_exch='2026-09-28 15:01:10.100' WHERE fill_id='1149934'")
    r2 = reloj.cargar(con, ent.reglas)
    assert r2 is not r1 and r2.detalle[0]['fuente'] == 'exchange (ms)'
    con.execute("UPDATE fills_log SET t_log='2026-09-28 17:01:18.655' WHERE fill_id='1149934'")
    assert reloj.cargar(con, ent.reglas) is not r2


# ===========================================================================
# B5: asignaciones de la preoperativa anteriores a t_elegida_exch_utc
# ===========================================================================

def test_b5_asignaciones_antiguas_reciben_la_hora_del_exchange(tmp_path):
    """Las 3 filas reales del 28/09 se guardaron sin t_elegida_exch_utc. Tras la ingesta se rellenan con el reloj
    ATAS (fixture del 28/09: +7,309 s); la marca de tardía no se toca."""
    ent, con, s = _entorno(tmp_path, FIXT_28)
    con.execute('INSERT INTO estrategia_asignada (op_id, estrategia, t_elegida_utc, t_asignada_utc, tardia) '
                'VALUES (?,?,?,?,?)', ('E2T250-20260928-001', 'Cazas de soporte / resistencia en M17',
                                       '2026-09-28 15:08:44.582', '2026-09-28 15:16:35.667', 0))
    con.commit()
    s.ingerir()
    r = con.execute('SELECT t_elegida_exch_utc, tardia FROM estrategia_asignada').fetchone()
    assert (r['t_elegida_exch_utc'], r['tardia']) == ('2026-09-28 15:08:37.273', 0)


# ===========================================================================
# #12: sesión CME y retiros con la hora del exchange
# ===========================================================================

def test_12_sesion_cme_en_hora_del_exchange(tmp_path):
    """PC +7,309 s: a las 17:00:03 CT del PC el exchange aún está en las 16:59:56: sigue la sesión del 28/09."""
    ent, con, s = _entorno(tmp_path, FIXT_28)
    s.ingerir()
    assert motor.sesion_cme('2026-09-28 22:00:03.000') == date(2026, 9, 29)
    assert s.sesion('2026-09-28 22:00:03.000') == date(2026, 9, 28)
    assert s.calcular('2026-09-28 22:00:03.000').bal[0]['sesion'] == date(2026, 9, 28)


def test_12_retiro_anotado_con_el_reloj_del_pc_adelantado(tmp_path):
    """Retiro anotado a las 17:09:28 del PC (+7,309 s: 17:09:20,691 del exchange), antes de la apertura de E2T-250
    (17:09:25 del exchange): la operación ya lleva el nombre con el retiro. Con el reloj del PC salía sin él."""
    ent, con, s = _entorno(tmp_path, FIXT_28)
    s.ingerir()
    con.execute("INSERT INTO movimientos (cuenta, t, importe, tipo, nota) VALUES (?, ?, ?, 'retiro', '')",
                (E250, '2026-09-28 17:09:28.000', -100.0))
    con.commit()
    f = next(x for x in s.calcular('2026-09-28 15:44:00.000').filas if x.cuenta == E250)
    assert f.nombre == f'{E250}_retiros_1'


# ===========================================================================
# Migración: columnas de orden de línea y relectura
# ===========================================================================

def test_orden_de_linea_se_anade_al_arrancar_y_no_al_conectar(tmp_path):
    """Base anterior: conectar() no toca las tablas en las que un demonio viejo aún en marcha inserta por posición."""
    p = str(tmp_path / 'anterior.sqlite')
    c = sqlite3.connect(p)
    c.execute('CREATE TABLE equity (cuenta TEXT, t_log TEXT, bal REAL, pnl_abierto REAL, pnl_cerrado REAL, '
              'PRIMARY KEY (cuenta, t_log, bal, pnl_abierto, pnl_cerrado))')
    c.commit()
    c.close()
    con = almacen.conectar(p)

    def columnas():
        return [r['name'] for r in con.execute('PRAGMA table_info(equity)')]

    assert 'pos' not in columnas()
    con.execute('INSERT INTO equity VALUES (?,?,?,?,?)', ('C', '2026-09-28 17:00:00.000', 1.0, 0.0, 0.0))
    almacen.migrar_orden_linea(con)
    almacen.migrar_orden_linea(con)                     # idempotente
    assert columnas()[-2:] == ['fichero', 'pos']
    con.close()


def test_relectura_pone_la_linea_a_las_filas_viejas_sin_duplicar(tmp_path):
    ent, con, s = _entorno(tmp_path, FIXT_28)
    s.ingerir()
    tablas = almacen.TABLAS_LINEA[:1] + almacen.TABLAS_LINEA[2:]       # fills_ot_pendientes queda vacía
    antes = {t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tablas}
    for t in tablas:                                  # base anterior al 29/09: sin línea
        con.execute(f'UPDATE {t} SET fichero=NULL, pos=NULL')
    almacen.set_estado(con, almacen.MIGRACION_RELECTURA, '0')
    con.commit()
    s2 = sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)
    s2.ingerir()
    assert {t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tablas} == antes
    for t in tablas:
        assert con.execute(f'SELECT COUNT(*) FROM {t} WHERE pos IS NULL').fetchone()[0] == 0, t
    assert almacen.get_estado(con, almacen.MIGRACION_RELECTURA) == '1'
