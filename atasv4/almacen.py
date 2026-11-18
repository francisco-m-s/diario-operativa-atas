"""SQLite: fuente de verdad de la V4. Ingesta incremental e idempotente (clave natural por fuente).

Cada evento del log se guarda con su hora cruda (reloj del PC) y su línea: `fichero` (nombre del app_*.log) y `pos`
(byte de inicio de la línea). La línea asigna el tramo del reloj tras un salto y ordena lo que cae en el mismo ms
(29/09/2026, spec §1: orden de línea)."""

import json
import os
import re
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import cortafuegos, fuentes, reloj as mreloj, resets as mresets
from .fuentes import CHICAGO, MADRID

ESQUEMA = """
CREATE TABLE IF NOT EXISTS fills (          -- MyTrade.cdb
  cuenta TEXT, simbolo TEXT, fill_id TEXT, order_id TEXT, lado TEXT,
  precio REAL, volumen REAL, t_utc TEXT,
  PRIMARY KEY (cuenta, fill_id));
CREATE TABLE IF NOT EXISTS fills_log (      -- log: 'Processing trade' (hora exchange con ms + hora del log)
  cuenta TEXT, simbolo TEXT, fill_id TEXT, lado TEXT, t_exch TEXT, t_log TEXT, qty REAL, precio REAL,
  order_id TEXT,                            -- el de 'Order trade'; sin él, 'Order changed' de la misma cuenta/símbolo/lado justo después
  fichero TEXT, pos INTEGER,                -- línea de la hora t_log (orden de línea)
  PRIMARY KEY (cuenta, fill_id));           -- 'Order trade' sin 'Processing trade' (ATAS 8): t_exch NULL
CREATE TABLE IF NOT EXISTS fills_ot_pendientes ( -- log: 'Order trade' cuya orden (cuenta) aún no se conoce
  fill_id TEXT, simbolo TEXT, lado TEXT, order_id TEXT, t_log TEXT, qty REAL, precio REAL,
  fichero TEXT, pos INTEGER,
  PRIMARY KEY (fill_id, simbolo, lado));
CREATE TABLE IF NOT EXISTS sesiones_atas (  -- log: cada 'Started v…' (arranque de ATAS = corte de tramo del reloj)
  t_log TEXT PRIMARY KEY, version TEXT, fichero TEXT, pos INTEGER);
CREATE TABLE IF NOT EXISTS latencias (      -- log: 'Latency manager' (salto del reloj del PC y muestra del desfase)
  t_log TEXT, fuente TEXT, fichero TEXT, pos INTEGER, t_pc_utc TEXT, t_evento_utc TEXT, diff_s REAL, delay_s REAL,
  PRIMARY KEY (t_log, fuente));
CREATE TABLE IF NOT EXISTS patas (          -- HistoryMyTrade.cdb (FIFO de ATAS, PnL bruto)
  cuenta TEXT, simbolo TEXT, pata_id TEXT, open_utc TEXT, open_px REAL, open_vol REAL,
  close_utc TEXT, close_px REAL, close_vol REAL, pnl REAL, ticks_pnl REAL, comision REAL,
  PRIMARY KEY (cuenta, pata_id, open_utc, close_utc));
CREATE TABLE IF NOT EXISTS ordenes (        -- log: alta de órdenes (una fila por orden)
  cuenta TEXT, simbolo TEXT, ext_id TEXT, lado TEXT, tipo TEXT, limite REAL, stop REAL,
  etiqueta TEXT, t_log TEXT, origen TEXT, fichero TEXT, pos INTEGER,
  PRIMARY KEY (cuenta, ext_id));
CREATE TABLE IF NOT EXISTS cancelaciones (
  cuenta TEXT, simbolo TEXT, ext_id TEXT, lado TEXT, etiqueta TEXT, t_log TEXT, fichero TEXT, pos INTEGER,
  PRIMARY KEY (cuenta, ext_id));
CREATE TABLE IF NOT EXISTS equity (         -- log: 'Portfolio changed'
  cuenta TEXT, t_log TEXT, bal REAL, pnl_abierto REAL, pnl_cerrado REAL, fichero TEXT, pos INTEGER,
  PRIMARY KEY (cuenta, t_log, bal, pnl_abierto, pnl_cerrado));
CREATE INDEX IF NOT EXISTS ix_equity ON equity (cuenta, t_log);
CREATE TABLE IF NOT EXISTS posiciones (     -- log: última 'Position changed' por cuenta/símbolo
  cuenta TEXT, simbolo TEXT, volumen REAL, t_log TEXT, PRIMARY KEY (cuenta, simbolo));
CREATE TABLE IF NOT EXISTS posiciones_hist ( -- log: todas las 'Position changed' (cuenta plana según ATAS)
  cuenta TEXT, simbolo TEXT, t_log TEXT, volumen REAL, fichero TEXT, pos INTEGER,
  PRIMARY KEY (cuenta, simbolo, t_log, volumen));
CREATE TABLE IF NOT EXISTS log_offsets (fichero TEXT PRIMARY KEY, offset INTEGER);
CREATE TABLE IF NOT EXISTS cdb_mtime (fichero TEXT PRIMARY KEY, mtime REAL);

CREATE TABLE IF NOT EXISTS cuentas_estado ( -- balance de arranque: último Bal: plano del log
  cuenta TEXT PRIMARY KEY, bal_base REAL, bal_base_t TEXT);
CREATE TABLE IF NOT EXISTS anclas (          -- threshold vigente (nunca baja)
  cuenta TEXT PRIMARY KEY, valor REAL, t TEXT, origen TEXT);
CREATE TABLE IF NOT EXISTS threshold_manual (cuenta TEXT PRIMARY KEY, valor REAL, t TEXT);
CREATE TABLE IF NOT EXISTS movimientos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cuenta TEXT, t TEXT, importe REAL, tipo TEXT, nota TEXT);
CREATE TABLE IF NOT EXISTS cuentas_quemadas (cuenta TEXT PRIMARY KEY, t TEXT, nota TEXT);

CREATE TABLE IF NOT EXISTS ops_ids (         -- ID estable, generado una vez
  clave TEXT PRIMARY KEY, op_id TEXT UNIQUE, cuenta TEXT, sesion TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS manual_ops (      -- columnas manuales preservadas por ID
  op_id TEXT PRIMARY KEY, estrategia TEXT, observaciones TEXT, analisis_llm TEXT);
CREATE TABLE IF NOT EXISTS escrito_ops (     -- último MAE/MFE que el demonio escribió en el sheet (detección de manuales)
  op_id TEXT PRIMARY KEY, mae REAL, mfe REAL);
CREATE TABLE IF NOT EXISTS operaciones (     -- foto derivada de la última sincronización
  op_id TEXT PRIMARY KEY, cuenta TEXT, lider_id TEXT, sesion TEXT, t_apertura_utc TEXT, datos TEXT);
CREATE TABLE IF NOT EXISTS estado (clave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS compuerta_lecturas ( -- preoperativa, paso 2 (spec §10.6): cada cambio de las filas SÍ/NO
  t_pc_utc TEXT PRIMARY KEY,                -- hora de la lectura (reloj del PC; se compara en hora del exchange con el reloj ATAS)
  valores TEXT);                            -- lista JSON con el valor de cada fila, en mayúsculas ('' = vacía)
CREATE TABLE IF NOT EXISTS objetivos_manual ( -- recuento semanal (spec §11): columna 5 escrita a mano, por semana
  semana TEXT PRIMARY KEY,                  -- lunes de la semana (YYYY-MM-DD)
  valor TEXT);                              -- lo escrito en la celda, tal cual (número o texto)
CREATE TABLE IF NOT EXISTS mercado_lecturas ( -- preoperativa, estado del mercado (spec §12): cada cambio del valor
  t_pc_utc TEXT PRIMARY KEY,                -- hora de la lectura (reloj del PC; se compara en hora del exchange con el reloj ATAS)
  valor TEXT);
CREATE TABLE IF NOT EXISTS ciclos (           -- ciclos archivados (spec §15): lo abierto hasta hasta_utc ya no sale en la pestaña V4
  n INTEGER PRIMARY KEY AUTOINCREMENT, desde_utc TEXT, hasta_utc TEXT,   -- apertura (exchange, UTC) de la primera y la última operación
  pestana TEXT, decisiones INTEGER, t_archivado TEXT);
CREATE TABLE IF NOT EXISTS foto_filas (       -- foto del cálculo (spec §16.10): lo que da el log de cada operación ya archivada
  clave TEXT PRIMARY KEY, datos TEXT);      -- `<cuenta>|<Id del fill de entrada>` -> JSON de motor.CAMPOS_FOTO
CREATE TABLE IF NOT EXISTS foto_evaluar (     -- y la evaluación del cortafuegos de cada decisión ya archivada
  clave TEXT PRIMARY KEY, datos TEXT);
CREATE TABLE IF NOT EXISTS turnos (           -- turnos de operativa (spec §16.2): de arrancar el demonio a cerrarlo
  id INTEGER PRIMARY KEY AUTOINCREMENT, inicio_utc TEXT, fin_utc TEXT,     -- reloj del PC, UTC; fin NULL = en marcha
  latido_utc TEXT);                         -- última vez que el demonio anotó que seguía vivo
CREATE TABLE IF NOT EXISTS estrategia_asignada ( -- preoperativa (spec §7.2): estrategia puesta por el demonio al anotar
  op_id TEXT PRIMARY KEY, estrategia TEXT, t_elegida_utc TEXT, t_asignada_utc TEXT, tardia INTEGER,
  t_elegida_exch_utc TEXT);                 -- t_elegida_utc: reloj del PC; la del exchange es la que se compara
"""


def conectar(path):
    con = sqlite3.connect(path, timeout=30)
    _FOTO.pop(id(con), None)            # V5.1: una conexión cerrada puede haber dejado su foto en caché y este id es reutilizable
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(ESQUEMA)
    con.executescript(cortafuegos.ESQUEMA)
    _migrar(con)
    return con


def _migrar(con):
    """Base creada antes de fills_log.order_id / posiciones_hist: releer los logs (la ingesta es idempotente)."""
    cols = {r['name'] for r in con.execute('PRAGMA table_info(fills_log)')}
    if 'order_id' not in cols:
        con.execute('ALTER TABLE fills_log ADD COLUMN order_id TEXT')
        con.execute('DELETE FROM log_offsets')
        con.commit()
    cols = {r['name'] for r in con.execute('PRAGMA table_info(manual_ops)')}
    for c in ('mae_manual', 'mfe_manual'):          # MAE/MFE corregidos a mano (por ID de decisión)
        if c not in cols:
            con.execute(f'ALTER TABLE manual_ops ADD COLUMN {c} REAL')
    con.commit()


TABLAS_LINEA = ('fills_log', 'fills_ot_pendientes', 'sesiones_atas', 'ordenes', 'cancelaciones', 'equity',
                'posiciones_hist')


def migrar_orden_linea(con):
    """Columnas fichero/pos (orden de línea, 29/09/2026) en una base anterior. Va al arrancar el Sincronizador y no en
    conectar(), por lo mismo que migrar_estrategia_asignada: el demonio anterior inserta en estas tablas por posición.
    Las filas viejas reciben su línea en la relectura (migrar_relectura); hasta entonces se ordenan por la hora."""
    for t in TABLAS_LINEA:
        cols = {r['name'] for r in con.execute(f'PRAGMA table_info({t})')}
        for c, tipo in (('fichero', 'TEXT'), ('pos', 'INTEGER')):
            if c not in cols:
                con.execute(f'ALTER TABLE {t} ADD COLUMN {c} {tipo}')
    con.commit()


def migrar_estrategia_asignada(con):
    """Columna t_elegida_exch_utc (reloj ATAS, 28/09/2026). Va al arrancar el Sincronizador y no en conectar():
    cli_v4.py abre la base con el demonio en marcha, y el demonio anterior inserta en esta tabla 5 valores por
    posición; con la columna añadida por debajo, su sync fallaría hasta reiniciarlo."""
    cols = {r['name'] for r in con.execute('PRAGMA table_info(estrategia_asignada)')}
    if 't_elegida_exch_utc' not in cols:
        con.execute('ALTER TABLE estrategia_asignada ADD COLUMN t_elegida_exch_utc TEXT')
        con.commit()


def lecturas_compuerta(con):
    """[(hora del reloj del PC, [valores])] de los cambios de la compuerta (spec §10.6), en orden."""
    return [(r['t_pc_utc'], json.loads(r['valores']))
            for r in con.execute('SELECT t_pc_utc, valores FROM compuerta_lecturas ORDER BY t_pc_utc')]


def objetivos_manual(con):
    """{lunes ISO: texto} de la columna manual del recuento semanal (spec §11.3)."""
    return {r['semana']: r['valor'] for r in con.execute('SELECT semana, valor FROM objetivos_manual')}


def guardar_objetivos_manual(con, hoja):
    """Columna 5 leída de la pestaña -> SQLite, por semana (spec §11.3). El sheet manda para las semanas presentes (incluido
    borrar); las ausentes se conservan."""
    for semana, valor in hoja.items():
        if valor == '':
            con.execute('DELETE FROM objetivos_manual WHERE semana=?', (semana,))
        else:
            con.execute('INSERT OR REPLACE INTO objetivos_manual VALUES (?, ?)', (semana, valor))
    con.commit()


def lecturas_mercado(con):
    """[(hora del reloj del PC, valor)] de los cambios del estado del mercado (spec §12), en orden."""
    return [(r['t_pc_utc'], r['valor']) for r in con.execute('SELECT t_pc_utc, valor FROM mercado_lecturas ORDER BY t_pc_utc')]


_FOTO = {}          # id(con) -> (versión, foto): se relee solo cuando cambia `estado.foto_version`


def foto(con):
    """Foto del cálculo vigente (spec §16.10): {'filas': {clave: datos}, 'evaluar': {clave: datos}}, vacía si no hay o si está
    desactivada (`estado.foto_version` vacío). En caché por conexión mientras no cambie la versión."""
    version = get_estado(con, 'foto_version', '')
    if not version:
        return {'filas': {}, 'evaluar': {}}
    hit = _FOTO.get(id(con))
    if hit is not None and hit[0] == version:
        return hit[1]
    f = {'filas': {r['clave']: json.loads(r['datos']) for r in con.execute('SELECT clave, datos FROM foto_filas')},
         'evaluar': {r['clave']: json.loads(r['datos']) for r in con.execute('SELECT clave, datos FROM foto_evaluar')}}
    _FOTO[id(con)] = (version, f)
    return f


def guardar_foto(con, filas, evaluar, version):
    """Sustituye la foto entera. `version` vacía = sin foto (el motor vuelve a calcularlo todo)."""
    con.execute('DELETE FROM foto_filas')
    con.execute('DELETE FROM foto_evaluar')
    con.executemany('INSERT INTO foto_filas VALUES (?, ?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in filas.items()])
    con.executemany('INSERT INTO foto_evaluar VALUES (?, ?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in evaluar.items()])
    set_estado(con, 'foto_version', version)
    con.commit()
    _FOTO.pop(id(con), None)


def abrir_turno(con, ahora_utc):
    """Empieza un turno (arranque del demonio). Los que quedaron sin cerrar (cuelgue, ventana cerrada con la X) acaban en su
    último latido. Devuelve el id del turno nuevo."""
    con.execute('UPDATE turnos SET fin_utc=COALESCE(latido_utc, inicio_utc) WHERE fin_utc IS NULL')
    cur = con.execute('INSERT INTO turnos (inicio_utc, latido_utc) VALUES (?, ?)', (ahora_utc, ahora_utc))
    con.commit()
    return cur.lastrowid


def latido_turno(con, turno_id, ahora_utc):
    con.execute('UPDATE turnos SET latido_utc=? WHERE id=?', (ahora_utc, turno_id))
    con.commit()


def cerrar_turno(con, turno_id, ahora_utc):
    con.execute('UPDATE turnos SET fin_utc=?, latido_utc=? WHERE id=?', (ahora_utc, ahora_utc, turno_id))
    con.commit()


def turnos(con):
    """[(inicio, fin|None)] en orden; fin None = el turno en marcha."""
    return [(r['inicio_utc'], r['fin_utc']) for r in con.execute('SELECT inicio_utc, fin_utc FROM turnos ORDER BY inicio_utc')]


def ciclos(con):
    return [dict(r) for r in con.execute('SELECT * FROM ciclos ORDER BY n')]


def corte_ciclo(con):
    """Apertura (UTC del exchange) de la última operación archivada (spec §15), o None si no hay ciclos archivados: la pestaña
    V4 muestra solo las decisiones abiertas después."""
    return con.execute('SELECT MAX(hasta_utc) m FROM ciclos').fetchone()['m']


def cerrar_ciclo(con, desde_utc, hasta_utc, pestana, decisiones):
    con.execute('INSERT INTO ciclos (desde_utc, hasta_utc, pestana, decisiones, t_archivado) VALUES (?, ?, ?, ?, ?)',
                (desde_utc, hasta_utc, pestana, decisiones, ahora_madrid()))
    con.commit()


def get_estado(con, clave, defecto=None):
    r = con.execute('SELECT valor FROM estado WHERE clave=?', (clave,)).fetchone()
    return r['valor'] if r else defecto


def set_estado(con, clave, valor):
    con.execute('INSERT OR REPLACE INTO estado (clave, valor) VALUES (?, ?)', (clave, str(valor)))


def sembrar_quemadas(con, cuentas, ahora):
    if get_estado(con, 'quemadas_sembradas') == '1':
        return
    for c in cuentas or []:
        con.execute('INSERT OR IGNORE INTO cuentas_quemadas (cuenta, t, nota) VALUES (?, ?, ?)',
                    (c, ahora, 'quemada antes de la V4'))
    set_estado(con, 'quemadas_sembradas', '1')
    con.commit()


def inicio_utc(inicio_sesion):
    """Arranque de la sesión CME: 17:00 hora de Chicago del día anterior, en UTC."""
    d = inicio_sesion - timedelta(days=1)
    dt = datetime(d.year, d.month, d.day, 17, 0, tzinfo=CHICAGO).astimezone(timezone.utc)
    return fuentes.fmt_ts(dt.replace(tzinfo=None))


def inicio_madrid(inicio_sesion):
    return fuentes.fmt_ts(fuentes.utc_a_madrid(inicio_utc(inicio_sesion)))


# ---------------------------------------------------------------------------
# Ingesta
# ---------------------------------------------------------------------------

def _cdb_cambiado(con, path):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return False, None
    r = con.execute('SELECT mtime FROM cdb_mtime WHERE fichero=?', (os.path.basename(path),)).fetchone()
    return (r is None or r['mtime'] != mt), mt


def ingerir_cdb(con, db_dir, desde_utc, forzar=False, resets=None):
    """MyTrade.cdb y HistoryMyTrade.cdb desde el arranque de la V4. Devuelve nº de filas nuevas. `resets`: el registro de
    resets (spec §14): la cuenta de cada fila es la lógica a su hora UTC (resets.cuenta_logica)."""
    nuevas = 0
    p = os.path.join(db_dir, 'MyTrade.cdb')
    cambiado, mt = _cdb_cambiado(con, p)
    if cambiado or forzar:
        for r in fuentes.read_cdb(p):
            try:
                t = fuentes.cdb_utc(r['Time'])
            except (KeyError, ValueError):
                continue
            if t < desde_utc or not r.get('AccountID') or not r.get('Id'):
                continue
            cur = con.execute(          # Id de 8.0.15 (<OrderId>_<número>) al número, como en el log (fuentes.id_fill)
                'INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?)',
                (mresets.cuenta_logica(resets, r['AccountID'], t, utc=True), r.get('SecurityId', ''),
                 fuentes.id_fill(r['Id']), r.get('OrderId', ''), r.get('OrderDirection', ''), fuentes.num(r.get('Price'), 0.0),
                 fuentes.num(r.get('Volume'), 0.0), t))
            nuevas += cur.rowcount
        if mt is not None:
            con.execute('INSERT OR REPLACE INTO cdb_mtime VALUES (?, ?)', ('MyTrade.cdb', mt))
    p = os.path.join(db_dir, 'HistoryMyTrade.cdb')
    cambiado, mt = _cdb_cambiado(con, p)
    if cambiado or forzar:
        for r in fuentes.read_cdb(p):
            try:
                o, c = fuentes.cdb_utc(r['OpenTime']), fuentes.cdb_utc(r['CloseTime'])
            except (KeyError, ValueError):
                continue
            if o < desde_utc or not r.get('AccountID'):
                continue
            cur = con.execute(
                'INSERT OR IGNORE INTO patas VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (mresets.cuenta_logica(resets, r['AccountID'], o, utc=True), r.get('SecurityId', ''),
                 r.get('Id', ''), o, fuentes.num(r.get('OpenPrice'), 0.0), fuentes.num(r.get('OpenVolume'), 0.0), c,
                 fuentes.num(r.get('ClosePrice'), 0.0), fuentes.num(r.get('CloseVolume'), 0.0),
                 fuentes.num(r.get('PnL'), 0.0), fuentes.num(r.get('TicksPnL'), 0.0),
                 fuentes.num(r.get('Commission'))))
            nuevas += cur.rowcount
        if mt is not None:
            con.execute('INSERT OR REPLACE INTO cdb_mtime VALUES (?, ?)', ('HistoryMyTrade.cdb', mt))
    con.commit()
    return nuevas + resolver_fills_ot(con)     # un 'Order trade' sin orden en el log se resuelve por su fill de MyTrade


VENTANA_ORDEN_FILL_S = 1.0     # 'Order changed' de la orden llenada: mismo ms o pocos ms después del fill


def _completar_linea(con, tabla, donde, args, d):
    """Fila guardada antes del orden de línea (29/09/2026): al releer su misma línea recibe fichero y posición."""
    con.execute(f'UPDATE {tabla} SET fichero=?, pos=? WHERE {donde} AND pos IS NULL AND t_log=?',
                (d.get('fichero'), d.get('pos'), *args, d['t_log']))


def _guardar_evento(con, tipo, d, fills_sin_orden):
    fichero, pos = d.get('fichero'), d.get('pos')
    if tipo == 'fill':
        fills_sin_orden[(d['cuenta'], d['simbolo'], d['lado'])] = (d['fill_id'], d['t_log'])
        # 'Processing trade' manda sobre un 'Order trade' ya guardado (trae la hora del exchange; su línea pasa a ser
        # la del fill); un segundo 'Processing trade' del mismo fill (repetición) no cambia nada.
        n = con.execute('INSERT INTO fills_log (cuenta, simbolo, fill_id, lado, t_exch, t_log, qty, precio, order_id, '
                        'fichero, pos) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cuenta, fill_id) DO UPDATE '
                        'SET t_exch=excluded.t_exch, t_log=excluded.t_log, qty=excluded.qty, precio=excluded.precio, '
                        'fichero=excluded.fichero, pos=excluded.pos WHERE fills_log.t_exch IS NULL',
                        (d['cuenta'], d['simbolo'], d['fill_id'], d['lado'], d['t_exch'], d['t_log'], d['qty'],
                         d['precio'], None, fichero, pos)).rowcount
        _completar_linea(con, 'fills_log', 'cuenta=? AND fill_id=?', (d['cuenta'], d['fill_id']), d)
        return n
    if tipo == 'fill_ot':
        # Sin cuenta: se resuelve por su orden al final de la ingesta (resolver_fills_ot). La primera aparición manda.
        con.execute('INSERT INTO fills_ot_pendientes (fill_id, simbolo, lado, order_id, t_log, qty, precio, fichero, '
                    'pos) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(fill_id, simbolo, lado) DO UPDATE '
                    'SET t_log=excluded.t_log, fichero=excluded.fichero, pos=excluded.pos '
                    'WHERE excluded.t_log < fills_ot_pendientes.t_log',
                    (d['fill_id'], d['simbolo'], d['lado'], d['order_id'], d['t_log'], d['qty'], d['precio'],
                     fichero, pos))
        return 0
    if tipo == 'inicio':
        con.execute('INSERT INTO sesiones_atas (t_log, version, fichero, pos) VALUES (?,?,?,?) ON CONFLICT(t_log) '
                    'DO UPDATE SET fichero=excluded.fichero, pos=excluded.pos WHERE sesiones_atas.pos IS NULL',
                    (d['t_log'], d['version'], fichero, pos))
        return 0
    if tipo == 'latencia':
        con.execute('INSERT INTO latencias (t_log, fuente, fichero, pos, t_pc_utc, t_evento_utc, diff_s, delay_s) '
                    'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(t_log, fuente) DO UPDATE '
                    'SET fichero=excluded.fichero, pos=excluded.pos WHERE latencias.pos IS NULL',
                    (d['t_log'], d['fuente'], fichero, pos, d['t_pc_utc'], d['t_evento_utc'], d['diff_s'],
                     d['delay_s']))
        return 0
    if tipo == 'orden' and d['origen'] == 'changed':
        k = (d['cuenta'], d['simbolo'], d['lado'])
        if k in fills_sin_orden:
            fid, t_fill = fills_sin_orden[k]
            dt = (fuentes.parse_ts(d['t_log']) - fuentes.parse_ts(t_fill)).total_seconds()
            if 0 <= dt <= VENTANA_ORDEN_FILL_S:
                con.execute("UPDATE fills_log SET order_id=? WHERE cuenta=? AND fill_id=? "
                            "AND (order_id IS NULL OR order_id='')", (d['ext_id'], d['cuenta'], fid))
                del fills_sin_orden[k]
    if tipo == 'orden':
        r = con.execute('SELECT origen, t_log, pos FROM ordenes WHERE cuenta=? AND ext_id=?',
                        (d['cuenta'], d['ext_id'])).fetchone()
        # El alta ('Order registered') manda: hora de colocación y tipo original (un Stop
        # disparado aparece luego como 'Market' en 'Order changed').
        if r is None or (r['origen'] == 'changed' and d['origen'] == 'registered'):
            con.execute('INSERT OR REPLACE INTO ordenes (cuenta, simbolo, ext_id, lado, tipo, limite, stop, etiqueta, '
                        't_log, origen, fichero, pos) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (d['cuenta'], d['simbolo'], d['ext_id'], d['lado'], d['tipo'], d['limite'],
                         d['stop'], d['etiqueta'], d['t_log'], d['origen'], fichero, pos))
            return 1
        if r['pos'] is None and r['origen'] == d['origen'] and r['t_log'] == d['t_log']:
            con.execute('UPDATE ordenes SET fichero=?, pos=? WHERE cuenta=? AND ext_id=?',
                        (fichero, pos, d['cuenta'], d['ext_id']))
        return 0
    if tipo == 'cancel':
        n = con.execute('INSERT OR IGNORE INTO cancelaciones (cuenta, simbolo, ext_id, lado, etiqueta, t_log, fichero, '
                        'pos) VALUES (?,?,?,?,?,?,?,?)',
                        (d['cuenta'], d['simbolo'], d['ext_id'], d['lado'], d['etiqueta'], d['t_log'], fichero,
                         pos)).rowcount
        if not n:
            _completar_linea(con, 'cancelaciones', 'cuenta=? AND ext_id=?', (d['cuenta'], d['ext_id']), d)
        return n
    if tipo == 'equity':
        if d['bal'] == 0:        # reconexión: 'Bal:0/0/0 Avl: PnL:0/0' no es un dato
            return 0
        clave = (d['cuenta'], d['bal'], d['pnl_abierto'], d['pnl_cerrado'])
        n = con.execute('INSERT OR IGNORE INTO equity (cuenta, t_log, bal, pnl_abierto, pnl_cerrado, fichero, pos) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (d['cuenta'], d['t_log'], d['bal'], d['pnl_abierto'], d['pnl_cerrado'], fichero,
                         pos)).rowcount
        if not n:
            _completar_linea(con, 'equity', 'cuenta=? AND bal=? AND pnl_abierto=? AND pnl_cerrado=?', clave, d)
        return n
    if tipo == 'posicion':
        n = con.execute('INSERT OR IGNORE INTO posiciones_hist (cuenta, simbolo, t_log, volumen, fichero, pos) '
                        'VALUES (?,?,?,?,?,?)',
                        (d['cuenta'], d['simbolo'], d['t_log'], d['volumen'], fichero, pos)).rowcount
        if not n:
            _completar_linea(con, 'posiciones_hist', 'cuenta=? AND simbolo=? AND volumen=?',
                             (d['cuenta'], d['simbolo'], d['volumen']), d)
        con.execute('INSERT INTO posiciones (cuenta, simbolo, volumen, t_log) VALUES (?,?,?,?) '
                    'ON CONFLICT(cuenta, simbolo) DO UPDATE '
                    'SET volumen=excluded.volumen, t_log=excluded.t_log WHERE excluded.t_log >= posiciones.t_log',
                    (d['cuenta'], d['simbolo'], d['volumen'], d['t_log']))
        return 0
    return 0


def _anterior_al_arranque(name, desde_madrid):
    """app_YYYYMMDD….log de un día anterior al arranque de la V4: no se abre."""
    dia = name[4:12]
    return dia.isdigit() and f"{dia[:4]}-{dia[4:6]}-{dia[6:]}" < desde_madrid[:10]


def _ingerir_lineas(con, lines, desde_madrid, fills_sin_orden, fichero=None, resets=None):
    """lines: [(posición en bytes, texto)] de `fichero` (leer_incremental). `resets`: el registro de resets (spec §14): el
    evento se guarda con su cuenta lógica a la hora de su línea (resets.cuenta_logica)."""
    nuevas = 0
    for pos, line in lines:
        ev = fuentes.parse_linea(line)
        if ev is None:
            continue
        tipo, d = ev
        if d['t_log'] < desde_madrid:
            continue
        d['fichero'], d['pos'] = fichero, pos
        if resets and 'cuenta' in d:
            d['cuenta'] = mresets.cuenta_logica(resets, d['cuenta'], d['t_log'])
        nuevas += _guardar_evento(con, tipo, d, fills_sin_orden)
    return nuevas


def ingerir_logs(con, logs_dir, desde_madrid, resets=None):
    """Lee solo los bytes nuevos de cada app_*.log. Idempotente: re-leer no duplica. `resets`: ver _ingerir_lineas."""
    nuevas = 0
    fills_sin_orden = {}
    for name in fuentes.ficheros_log(logs_dir):
        if _anterior_al_arranque(name, desde_madrid):
            continue
        r = con.execute('SELECT offset FROM log_offsets WHERE fichero=?', (name,)).fetchone()
        lines, off = fuentes.leer_incremental(os.path.join(logs_dir, name), r['offset'] if r else 0)
        if r is not None and off == r['offset']:        # sin bytes nuevos: ni escritura ni commit (11/11/2026; antes, uno
            continue                                    # por fichero y ciclo de 2 s, con su fsync)
        nuevas += _ingerir_lineas(con, lines, desde_madrid, fills_sin_orden, name, resets)
        con.execute('INSERT OR REPLACE INTO log_offsets VALUES (?, ?)', (name, off))
        con.commit()
    nuevas += resolver_fills_ot(con)
    return nuevas


def resolver_fills_ot(con):
    """'Order trade' pendientes -> fills_log. La cuenta sale de su orden en el log ('Order registered/changed' o
    'Cancel order': mismo número, símbolo y lado) o, si el log no la tiene (orden de antes del fichero), del fill de
    MyTrade.cdb con el mismo Id. Lo que no se resuelve sigue en fills_ot_pendientes y se reintenta en cada ingesta
    (el offset del log ya pasó esa línea: si no se guardara, se perdería). Resueltos:
      - fill nuevo: t_exch NULL (la hora del exchange la estima el reloj ATAS al usarlo);
      - ya había 'Processing trade': solo se toma el order_id (el de 'Order trade' es exacto; la hora y la línea
        siguen siendo las de 'Processing trade');
      - ya había un 'Order trade' (repetición al reconectar): se quedan la primera hora y su línea."""
    nuevas = 0
    for p in con.execute('SELECT * FROM fills_ot_pendientes').fetchall():
        args = (p['order_id'], p['simbolo'], p['lado'])
        cuentas = {r['cuenta'] for r in con.execute(
            'SELECT cuenta FROM ordenes WHERE ext_id=? AND simbolo=? AND lado=? '
            'UNION SELECT cuenta FROM cancelaciones WHERE ext_id=? AND simbolo=? AND lado=?', args + args)}
        if not cuentas:
            cuentas = {r['cuenta'] for r in con.execute(
                'SELECT cuenta FROM fills WHERE fill_id=? AND simbolo=? AND lado=?',
                (p['fill_id'], p['simbolo'], p['lado']))}
        if len(cuentas) != 1:
            continue
        cuenta = cuentas.pop()
        r = con.execute('SELECT t_exch, t_log, pos FROM fills_log WHERE cuenta=? AND fill_id=?',
                        (cuenta, p['fill_id'])).fetchone()
        if r is None:
            con.execute('INSERT INTO fills_log (cuenta, simbolo, fill_id, lado, t_exch, t_log, qty, precio, order_id, '
                        'fichero, pos) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (cuenta, p['simbolo'], p['fill_id'], p['lado'], None, p['t_log'], p['qty'], p['precio'],
                         p['order_id'], p['fichero'], p['pos']))
            nuevas += 1
        elif not r['t_exch'] and (p['t_log'] < r['t_log'] or (p['t_log'] == r['t_log'] and r['pos'] is None)):
            con.execute('UPDATE fills_log SET order_id=?, t_log=?, fichero=?, pos=? WHERE cuenta=? AND fill_id=?',
                        (p['order_id'], p['t_log'], p['fichero'], p['pos'], cuenta, p['fill_id']))
        else:
            con.execute('UPDATE fills_log SET order_id=? WHERE cuenta=? AND fill_id=?',
                        (p['order_id'], cuenta, p['fill_id']))
        con.execute('DELETE FROM fills_ot_pendientes WHERE fill_id=? AND simbolo=? AND lado=?',
                    (p['fill_id'], p['simbolo'], p['lado']))
    con.commit()
    return nuevas


MIGRACION_RELECTURA = 'migracion_relectura_20260929'
RELECTURA_FALTAN = 'migracion_relectura_faltan'       # ficheros ya leídos que no están ni en ATAS ni en la copia


def migrar_relectura(con, logs_dir, respaldo_dir, desde_madrid, resets=None):
    """Una vez, al arrancar (29/09/2026; sustituye a la de 'Order trade' del 28/09, que no llegó a correr en la base
    real). Los logs ya leídos entraron sin 'Order trade', arranques ni saltos del reloj, y sin orden de línea;
    log_offsets impide releerlos. Se borran los offsets y la siguiente ingesta relee todos los app_*.log que siguen en
    ATAS: no duplica (clave natural en cada tabla) y a las filas viejas solo les pone fichero y posición. Los que ATAS
    ya borró (rota a ~6 días) se leen enteros de la copia `respaldo_dir`.
    Si falta alguno de los ya leídos (ni en ATAS ni en la copia), NO se da por hecha: el que llama avisa y se repite en
    cada arranque hasta que aparezca en la copia. Devuelve (filas nuevas leídas de la copia, ficheros que faltan)."""
    if get_estado(con, MIGRACION_RELECTURA) == '1':
        return 0, []
    vivos = set(fuentes.ficheros_log(logs_dir))
    copia = set(fuentes.ficheros_log(respaldo_dir)) if respaldo_dir else set()
    leidos = {r['fichero'] for r in con.execute('SELECT fichero FROM log_offsets')}
    leidos |= set(json.loads(get_estado(con, RELECTURA_FALTAN, '[]')))     # los que faltaron en un intento anterior
    faltan = sorted(n for n in leidos - vivos - copia if not _anterior_al_arranque(n, desde_madrid))
    nuevas = 0
    for name in sorted(copia - vivos):
        if _anterior_al_arranque(name, desde_madrid):
            continue
        lines, _ = fuentes.leer_incremental(os.path.join(respaldo_dir, name), 0)
        nuevas += _ingerir_lineas(con, lines, desde_madrid, {}, name, resets)
    nuevas += resolver_fills_ot(con)
    con.execute('DELETE FROM log_offsets')
    set_estado(con, RELECTURA_FALTAN, json.dumps(faltan))
    if not faltan:
        set_estado(con, MIGRACION_RELECTURA, '1')
    con.commit()
    return nuevas, faltan


MIGRACION_ATAS_8015 = 'migracion_relectura_20261001'


def migrar_atas_8015(con):
    """Una vez, al arrancar (01/10/2026). ATAS 8.0.15 (arrancó el 01/10 a las 12:08:07) escribe el Id del fill como
    <OrderId>_<número> en 'Order trade' y en MyTrade.cdb. El código anterior no casaba esas líneas (fills_log sin los
    fills desde entonces: operaciones [SIN TELEMETRÍA]) y guardó los fills de MyTrade con el Id tal cual.
      1. Los Id ya guardados en el formato nuevo pasan al número (fuentes.id_fill): fills y, con ellos, las claves que
         los llevan (ops_ids `<cuenta>|<Id>`, que conserva así el ID de la operación, y avisos_emitidos
         `<tipo>|<cuenta>|<Id>`, para no repetir un aviso ya lanzado). Si ya existía el normalizado, sobra el viejo.
      2. log_offsets ya pasó esas líneas: se borran los de los ficheros desde el día del primer arranque de ATAS 8.0.15
         o posterior (sesiones_atas) y la siguiente ingesta los relee enteros. No duplica (clave natural por fuente,
         un fill por Id con la primera hora). Sin ese arranque no se saltó ninguna línea: no hay nada que releer.
    Devuelve (Id renombrados, ficheros que se releen) o None si ya estaba hecha."""
    if get_estado(con, MIGRACION_ATAS_8015) == '1':
        return None
    renombrados = 0
    for r in con.execute("SELECT rowid, cuenta, fill_id FROM fills WHERE fill_id LIKE '%\\_%' ESCAPE '\\'").fetchall():
        nuevo = fuentes.id_fill(r['fill_id'])
        if nuevo == r['fill_id']:
            continue
        renombrados += 1
        viejo = f"{r['cuenta']}|{r['fill_id']}"
        if con.execute('SELECT 1 FROM fills WHERE cuenta=? AND fill_id=?', (r['cuenta'], nuevo)).fetchone():
            con.execute('DELETE FROM fills WHERE rowid=?', (r['rowid'],))
        else:
            con.execute('UPDATE fills SET fill_id=? WHERE rowid=?', (nuevo, r['rowid']))
        con.execute('UPDATE OR IGNORE ops_ids SET clave=? WHERE clave=?', (f"{r['cuenta']}|{nuevo}", viejo))
        for a in con.execute('SELECT clave FROM avisos_emitidos').fetchall():
            if a['clave'].endswith(f'|{viejo}'):
                con.execute('UPDATE OR IGNORE avisos_emitidos SET clave=? WHERE clave=?',
                            (a['clave'][:-len(viejo)] + f"{r['cuenta']}|{nuevo}", a['clave']))
    inicios = []
    for r in con.execute('SELECT t_log, version FROM sesiones_atas'):
        v = re.match(r'(\d+)\.(\d+)\.(\d+)', r['version'] or '')
        if v and tuple(map(int, v.groups())) >= fuentes.VERSION_ID_FILL_8015:
            inicios.append(r['t_log'])
    releer = []
    if inicios:
        dia = min(inicios)[:10].replace('-', '')
        releer = sorted(r['fichero'] for r in con.execute('SELECT fichero FROM log_offsets') if r['fichero'][4:12] >= dia)
        con.executemany('DELETE FROM log_offsets WHERE fichero=?', [(f,) for f in releer])
    set_estado(con, MIGRACION_ATAS_8015, '1')
    con.commit()
    return renombrados, releer


def cuentas_vistas(con):
    """Cuentas con actividad o balance en las fuentes ingeridas."""
    q = ('SELECT cuenta FROM fills UNION SELECT cuenta FROM equity '
         'UNION SELECT cuenta FROM patas UNION SELECT cuenta FROM fills_log')
    return sorted(r['cuenta'] for r in con.execute(q))


def _tramos(pasos):
    """[(símbolo, volumen tras el paso, orden)] -> [(desde, hasta|None)] con la cuenta en posición."""
    pos, tramos, abierto = {}, [], None
    for simbolo, volumen, orden in pasos:
        pos[simbolo] = volumen
        plano = all(abs(v) < 1e-9 for v in pos.values())
        if abierto is None and not plano:
            abierto = orden
        elif abierto is not None and plano:
            tramos.append((abierto, orden))
            abierto = None
    if abierto is not None:
        tramos.append((abierto, None))
    return tramos


def tramos_abiertos(con, cuenta, reloj=None):
    """[(desde, hasta|None)] en que la cuenta tiene posición, como claves de orden de línea del reloj ATAS (hora
    corregida, posición en el fichero): unión de lo que dicen sus fills del log y ATAS ('Position changed'). Los fills
    solos no bastan desde ATAS 8 (28/09/2026, E2T): 'Position changed' y el primer 'Bal:' con la posición ya abierta
    llegan ANTES que la línea del fill ('Bal:' 24.844,40 a las 17:09:32,946 con el fill a las ,957); en ATAS 7 era al
    revés (fill, 'Position changed', 'Bal:'). 'PnL abierto = 0' no sirve: la primera 'Portfolio changed' tras una
    entrada trae PnL:0. Sin reloj, horas crudas."""
    rel = reloj or mreloj.Reloj()

    def ordenadas(sql):
        filas = [dict(r) for r in con.execute(sql + ' ORDER BY t_log, rowid', (cuenta,))]
        for r in filas:
            r['k'] = rel.clave(r['t_log'], r['fichero'], r['pos'])
        return sorted(filas, key=lambda r: r['k'])          # estable: sin posición, el orden de inserción

    por_pos = _tramos((r['simbolo'], r['volumen'], r['k']) for r in ordenadas(
        'SELECT simbolo, volumen, t_log, fichero, pos FROM posiciones_hist WHERE cuenta=?'))
    acum, pasos = defaultdict(float), []
    for r in ordenadas('SELECT simbolo, lado, qty, t_log, fichero, pos FROM fills_log WHERE cuenta=?'):
        acum[r['simbolo']] += r['qty'] if r['lado'] == 'Buy' else -r['qty']
        pasos.append((r['simbolo'], acum[r['simbolo']], r['k']))
    unidos = []
    for a, b in sorted(por_pos + _tramos(pasos), key=lambda x: x[0]):
        # un tramo que vuelve a abrir en el mismo ms en que cerró el anterior es un giro: sigue abierta. En el giro
        # de APEX-75 del 23/09 18:48:17,614 hay un 'Bal:' entre el 'vol 0' y el 'vol -1' que no es plano.
        if unidos and (unidos[-1][1] is None or a <= unidos[-1][1] or a[0] == unidos[-1][1][0]):
            fin = unidos[-1][1]
            unidos[-1] = (unidos[-1][0], None if (fin is None or b is None) else max(fin, b))
        else:
            unidos.append((a, b))
    return unidos


def bal_plano(con, cuenta, hasta=None, reloj=None):
    """Último 'Bal:' del log con la cuenta sin posición: {'t_log', 'bal', 't_c' (hora corregida)} o None.
    Orden de línea (29/09/2026, spec §7.1): un 'Bal:' del mismo ms que la vuelta a plano solo es plano si su línea va
    DETRÁS. En ATAS 7 el bueno sale tras el 'Position changed … vol 0' (21/09 18:10:08,014) y en ATAS 8/E2T tras el
    fill de salida (28/09 17:16:31,809); ATAS 8/Apex escribe además uno de transición justo ANTES del 'vol 0' (PA-15,
    28/09 17:06:50,710: DESCUADRE falso +25,02). Una fila sin posición (anterior al 29/09 y no releída) cuenta como
    detrás, como antes. El PnL abierto no sirve para descartar: tras un cierre ATAS repite el anterior.
    `hasta`: hora cruda del log."""
    rel = reloj or mreloj.Reloj()
    tramos = tramos_abiertos(con, cuenta, rel)
    inicios = [a for a, _ in tramos]

    def en_posicion(k):
        i = bisect_right(inicios, k) - 1
        return i >= 0 and (tramos[i][1] is None or k < tramos[i][1])

    q = 'SELECT t_log, bal, fichero, pos FROM equity WHERE cuenta=?'
    args = [cuenta]
    if hasta:
        q += ' AND t_log<=?'
        args.append(hasta)
    # Se recorre hacia atrás por hora cruda; tras un salto hacia atrás del reloj, las horas crudas pueden desordenarse
    # hasta rel.ambiguedad_s: se sigue mirando ese margen antes de dar por bueno el último plano.
    margen = timedelta(seconds=rel.ambiguedad_s)
    mejor = None
    for r in con.execute(q + ' ORDER BY t_log DESC, pos DESC, rowid DESC', args):
        if mejor is not None and fuentes.parse_ts(r['t_log']) < fuentes.parse_ts(mejor['t_log']) - margen:
            break
        k = rel.clave(r['t_log'], r['fichero'], r['pos'], sin_pos=mreloj.INF)
        if en_posicion(k):
            continue
        if mejor is None or k > mejor['k']:
            mejor = {'t_log': r['t_log'], 'bal': r['bal'], 't_c': k[0], 'k': k}
    return mejor


def ultimo_bal_plano(con, cuenta, hasta=None, reloj=None):
    """(hora cruda del log, balance) del último 'Bal:' plano (bal_plano), o (None, None)."""
    b = bal_plano(con, cuenta, hasta, reloj)
    return (b['t_log'], b['bal']) if b else (None, None)


# ---------------------------------------------------------------------------
# Reset de una cuenta (spec §14)
# ---------------------------------------------------------------------------

TABLAS_RESET = (                    # (tabla, columna de la hora, ¿UTC?): las que llevan `cuenta` y la parte un reset
    ('fills', 't_utc', True),       # MyTrade.cdb
    ('patas', 'open_utc', True),    # HistoryMyTrade.cdb: una pata es del reset en que abrió
    ('fills_log', 't_log', False),
    ('ordenes', 't_log', False),
    ('cancelaciones', 't_log', False),
    ('equity', 't_log', False),
    ('posiciones_hist', 't_log', False),
)


def reasignar_resets(con, registro):
    """Reasignación retroactiva de lo ya ingerido (spec §14.1): en cada tabla de TABLAS_RESET, lo de la cuenta física (o del
    reset anterior) con hora >= `desde` pasa a la cuenta lógica del reset. Idempotente: lo que ya está en su cuenta no se mueve,
    y una fila que ya existía en la cuenta lógica (repetición de una relectura) deja de sobrar. Solo toca la columna `cuenta` de
    lo ingerido; no quema ni desquema nada. Deja en `estado` la huella del registro aplicado. Devuelve {tabla: filas movidas}."""
    movidas = {}
    for fisica in sorted(registro or {}):
        cs = mresets.cortes(registro, fisica)
        for tabla, col, utc in TABLAS_RESET:
            origen = [fisica]
            for desde, nombre in cs:
                donde = f"cuenta IN ({','.join('?' * len(origen))}) AND {col} >= ?"
                corte = mresets.desde_utc(desde) if utc else desde
                movidas[tabla] = movidas.get(tabla, 0) + con.execute(
                    f'UPDATE OR IGNORE {tabla} SET cuenta=? WHERE {donde}', (nombre, *origen, corte)).rowcount
                con.execute(f'DELETE FROM {tabla} WHERE {donde}', (*origen, corte))     # los duplicados que no se pudieron mover
                origen.append(nombre)
        _reconstruir_posiciones(con, mresets.logicas(registro, fisica))
    set_estado(con, mresets.CLAVE_ESTADO, mresets.huella(registro))
    con.commit()
    return movidas


def _reconstruir_posiciones(con, cuentas):
    """`posiciones` (última 'Position changed' por cuenta y símbolo) de unas cuentas, desde posiciones_hist: el UPDATE de
    reasignar_resets no vale (la última de la cuenta original ya no es la última de la tabla)."""
    marcas = ','.join('?' * len(cuentas))
    con.execute(f'DELETE FROM posiciones WHERE cuenta IN ({marcas})', cuentas)
    ultimas = {}
    for r in con.execute(f'SELECT cuenta, simbolo, volumen, t_log FROM posiciones_hist WHERE cuenta IN ({marcas}) '
                         'ORDER BY t_log, rowid', cuentas):
        ultimas[(r['cuenta'], r['simbolo'])] = (r['volumen'], r['t_log'])
    con.executemany('INSERT INTO posiciones (cuenta, simbolo, volumen, t_log) VALUES (?,?,?,?)',
                    [(c, s, v, t) for (c, s), (v, t) in ultimas.items()])


def aplicar_resets(con, registro):
    """Reasigna SQLite con el registro vigente solo si cambió desde la última vez (huella en `estado`): el que escribe el
    CLI, o uno editado a mano en cuentas.yaml. None si no hizo falta; sin resets ni huella, no toca la base."""
    if get_estado(con, mresets.CLAVE_ESTADO, '') == mresets.huella(registro):
        return None
    return reasignar_resets(con, registro)


def operaciones_con_id_antiguo(con, registro, fisica):
    """op_id de `ops_ids` (ya generados) cuyo primer fill de entrada pertenece ahora a otra cuenta lógica de la misma cuenta
    física. Tras un reset retroactivo con operaciones ya anotadas después del corte, esas operaciones salen con ID nuevo
    (`AP74R1-…`) y sus columnas manuales del sheet (Estrategia, Observaciones, Análisis LLM) no se trasladan."""
    familia = mresets.logicas(registro, fisica)
    marcas = ','.join('?' * len(familia))
    out = []
    for r in con.execute(f'SELECT op_id, cuenta, clave FROM ops_ids WHERE cuenta IN ({marcas})', familia).fetchall():
        fill = r['clave'].split('|', 1)[1]
        movido = con.execute(
            f'SELECT 1 FROM fills_log WHERE fill_id=? AND cuenta<>? AND cuenta IN ({marcas}) UNION '
            f'SELECT 1 FROM fills WHERE fill_id=? AND cuenta<>? AND cuenta IN ({marcas})',
            (fill, r['cuenta'], *familia, fill, r['cuenta'], *familia)).fetchone()
        if movido:
            out.append(r['op_id'])
    return sorted(out)


def primer_bal(con, cuenta):
    """(hora cruda del log, balance) del primer 'Bal:' de la cuenta, o (None, None). Es el de partida de un reset (spec §14.1:
    la cuenta nueva arranca de su primer balance tras el corte; la anterior no cuenta)."""
    r = con.execute('SELECT t_log, bal FROM equity WHERE cuenta=? ORDER BY t_log, pos, rowid LIMIT 1', (cuenta,)).fetchone()
    return (r['t_log'], r['bal']) if r else (None, None)


def quemadas(con):
    return {r['cuenta'] for r in con.execute('SELECT cuenta FROM cuentas_quemadas')}


def ahora_madrid():
    return fuentes.fmt_ts(datetime.now(MADRID).replace(tzinfo=None))
