"""Lectura de las fuentes de ATAS: .cdb (CSV propio), logs app_*.log y CopyTrading.cnf.

Hechos de formato (verificados sobre ficheros reales, INFORME_V3 §1 y 22/09/2026):
  - .cdb: cabecera `Columns:`, filas entre StartData/EndData, horas UTC `MM/DD/YYYY HH:MM:SS`.
    Playbook.cdb escapa comillas con barra invertida (`\\"`).
  - Logs: hora local de Madrid con ms; decimales con coma hasta ATAS 8.0.14 y con punto desde 8.0.15 (01/10/2026), sin
    separador de miles en ninguna (num() admite los dos). Las modificaciones de SL/TP son cancelación + nuevo
    `Order registered`.
  - Id del fill (`Order trade` y MyTrade.cdb): `<número>` hasta ATAS 8.0.14; `<OrderId>_<número>` desde 8.0.15. Se
    guarda normalizado al número (id_fill), el mismo en las dos versiones.
  - Las horas del log son del reloj del PC, no del exchange (reloj.py). Desde ATAS 8.0.14 (24/09/2026 11:56) el log
    no escribe DEBUG: no hay `Processing trade`; los fills salen de `Order trade` (INFO, en las dos versiones).
  - `Latency manager` (INFO): ATAS la escribe cuando el reloj del PC retrocede con ATAS abierto (w32time); marca el
    salto y mide el desfase de antes y de después (reloj.py). Las líneas se leen con su posición en bytes (orden de línea).
    ATAS 8.0.15 escribe además `Latency manager: <conexión> market data latency: median N ms, peak N ms` (WARN) y
    `… back to normal: median N ms, peak was N ms` (INFO): sin `time/event/diff/delay`, no se leen.
"""

import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MADRID = ZoneInfo('Europe/Madrid')
CHICAGO = ZoneInfo('America/Chicago')


def num(s, defecto=None):
    if s is None:
        return defecto
    s = str(s).strip().replace(',', '.')
    if not s:
        return defecto
    try:
        return float(s)
    except ValueError:
        return defecto


def cdb_utc(s):
    """'09/21/2026 16:06:41' (UTC en .cdb) -> 'YYYY-MM-DD HH:MM:SS.000' UTC."""
    dt = datetime.strptime(s.strip(), '%m/%d/%Y %H:%M:%S')
    return dt.strftime('%Y-%m-%d %H:%M:%S.000')


def parse_ts(s):
    """'YYYY-MM-DD HH:MM:SS.mmm' -> datetime. fromisoformat (C) en vez de strptime: se llama por cada fila del log
    que pasa por el reloj ATAS, cada 2 s."""
    return datetime.fromisoformat(s)


def fmt_ts(dt):
    """datetime -> 'YYYY-MM-DD HH:MM:SS.mmm' (ms truncados)."""
    return dt.isoformat(sep=' ', timespec='milliseconds')


def utc_a_madrid(s_utc):
    """Texto UTC -> datetime naive Europe/Madrid (zoneinfo: cambio de hora correcto)."""
    return parse_ts(s_utc).replace(tzinfo=timezone.utc).astimezone(MADRID).replace(tzinfo=None)


def madrid_a_utc(dt_madrid):
    return dt_madrid.replace(tzinfo=MADRID).astimezone(timezone.utc).replace(tzinfo=None)


def read_cdb(path, reintentos=3):
    """Filas del .cdb como dicts. [] si el fichero no existe o ATAS lo está reescribiendo."""
    for _ in range(reintentos):
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        except FileNotFoundError:
            return []
        except OSError:
            time.sleep(0.5)
            continue
        if 'EndData' not in text:
            time.sleep(0.5)
            continue
        cols, rows, in_data = [], [], False
        for line in text.splitlines():
            if line.startswith('Columns:'):
                cols = [c.strip() for c in next(csv.reader([line[8:]], skipinitialspace=True))]
            elif line.startswith('StartData'):
                in_data = True
            elif line.startswith('EndData'):
                break
            elif in_data and line.strip():
                vals = next(csv.reader([line], skipinitialspace=True, escapechar='\\'))
                rows.append(dict(zip(cols, (v.strip() for v in vals))))
        return rows
    raise IOError(f"No se pudo leer {os.path.basename(path)} completo")


def leer_playbook(db_dir):
    return [r['Name'] for r in read_cdb(os.path.join(db_dir, 'Playbook.cdb')) if r.get('Name')]


def leer_copytrading(atas_dir):
    """[(líder, seguidora)] de CopyTrading.cnf."""
    path = os.path.join(atas_dir, 'CopyTrading.cnf')
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    pares = []
    for s in data.get('CopyTradingManagerSettings', []):
        for fol in s.get('FollowerAccounts', []):
            pares.append((s['ProviderAccount'], fol['AccountId']))
    return pares


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

RE_TS = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})')
RE_FILL = re.compile(
    r'tchingProcessor: (\S+) (\S+)\. Processing trade (\d+) (Buy|Sell) '
    r'(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}\.\d+) ([\d.,]+)x([\d.,]+)')
RE_ORDER = re.compile(
    r'(Order registered|Order changed) (\d+)/\S+ (Buy|Sell) (\w+) (\S+) on (\S+) '
    r'at ([-\d.,]+)/([-\d.,]+) vol \S+ tif \w+\s+state (\w+)\s*(SL|TP)?\s*\.?\s*$')
RE_CANCEL = re.compile(
    r'\bCancel order (\d+)/\S+ (Buy|Sell) (\w+) (\S+) on (\S+) '
    r'at ([-\d.,]+)/([-\d.,]+) vol \S+ tif \w+\s+state \w+\s*(SL|TP)?\s*\.?\s*$')
RE_PORTFOLIO = re.compile(
    r'Portfolio changed (\S+) Bal:([-\d.,]+)/\S* Avl:\S* PnL:([-\d.,]+)/([-\d.,]+)')
RE_POSITION = re.compile(r'Position changed (\S+)/(\S+) vol (-?[\d.,]+) ')
# ATAS 7 y 8 (INFO). <orderNum = OrderId de MyTrade>/<tag>/<Id del fill en MyTrade>. Sin cuenta ni hora del exchange:
# la cuenta sale de la orden. ATAS la repite al reconectar (con la hora de la repetición). El Id del fill es <número>
# hasta 8.0.14 ('Order trade 2642003069/41932828/1020511 …') y <OrderId>_<número> desde 8.0.15
# ('Order trade 2642496470/43707684/2642496470_1025225 …'): id_fill.
RE_ORDER_TRADE = re.compile(
    r'Order trade (\d+)/(\S*?)/(\d+(?:_\d+)?) (Buy|Sell) (\S+) at ([-\d.,]+) vol (\d+(?:[.,]\d+)?)')
RE_ID_FILL_8015 = re.compile(r'\d+_(\d+)')
VERSION_ID_FILL_8015 = (8, 0, 15)       # primera versión de ATAS con el Id <OrderId>_<número> (almacen.migrar_atas_8015)
RE_INICIO = re.compile(r'\bApp\s+: Started v(\S+)')     # cada arranque de ATAS: corte de tramo del reloj (reloj.py)
# ATAS la escribe cuando la latencia de una conexión sale negativa: el reloj del PC acaba de retroceder (w32time).
# time = reloj del PC (UTC), event = hora del exchange del dato (UTC), diff = −(desfase que ATAS medía), delay = salto.
RE_LATENCIA = re.compile(
    r'Latency manager: (\S+) time: (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?) '
    r'event: (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?) diff: (\S+) delay: (\S+)')
RE_INTERVALO = re.compile(r'^(-)?(?:(\d+)\.)?(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$')    # TimeSpan de .NET

_CLAVES = ('Processing trade', 'Order registered', 'Order changed', 'Cancel order ',
           'Portfolio changed', 'Position changed', 'Order trade ', 'Started v', 'Latency manager')


def _intervalo_s(s):
    """TimeSpan de .NET ('-00:00:07.1339539', '1.02:03:04') -> segundos. None si no casa."""
    m = RE_INTERVALO.match(s)
    if not m:
        return None
    signo, dias, h, mi, sec = m.groups()
    v = int(dias or 0) * 86400 + int(h) * 3600 + int(mi) * 60 + float(sec)
    return -v if signo else v


def _hora_net(s):
    """'28.09.2026 15:32:17.569555' -> 'YYYY-MM-DD HH:MM:SS.mmm' (misma zona que el texto)."""
    fecha, hora = s.split(' ')
    if '.' in hora:
        hora, frac = hora.split('.')
        hora = f"{hora}.{frac[:6]}"
    else:
        hora += '.0'
    return fmt_ts(datetime.strptime(f"{fecha} {hora}", '%d.%m.%Y %H:%M:%S.%f'))


def id_fill(s):
    """Id del fill normalizado: '2642617452_1025723' (ATAS 8.0.15, log y MyTrade) -> '1025723', el número que ATAS
    8.0.14 daba solo ('1020511' no cambia). El prefijo es el OrderId del fill (6 de 6 fills del 01/10/2026) y el número
    no se repite en MyTrade (2.924 fills desde el 16/03/2026, en ninguna cuenta): un fill tiene el mismo Id en las dos
    versiones y en las dos fuentes (cruce log ↔ MyTrade, un fill por Id, reloj ATAS)."""
    m = RE_ID_FILL_8015.fullmatch(s)
    return m.group(1) if m else s


def parse_linea(line):
    """Una línea de log -> (tipo, dict) o None. Hora t = Madrid 'YYYY-MM-DD HH:MM:SS.mmm'."""
    if not any(k in line for k in _CLAVES):
        return None
    m = RE_TS.match(line)
    if not m:
        return None
    t = f"{m.group(1)}.{m.group(2)}"
    f = RE_FILL.search(line)
    if f:
        acc, sym, fid, side, ts, qty, px = f.groups()
        t_ex = datetime.strptime(ts[:23], '%Y.%m.%d %H:%M:%S.%f')
        return 'fill', {'cuenta': acc, 'simbolo': sym, 'fill_id': fid, 'lado': side,
                        't_exch': fmt_ts(t_ex), 't_log': t, 'qty': num(qty), 'precio': num(px)}
    ot = RE_ORDER_TRADE.search(line)
    if ot:
        ext, _, fid, side, sym, px, qty = ot.groups()
        return 'fill_ot', {'order_id': ext, 'fill_id': id_fill(fid), 'lado': side, 'simbolo': sym,
                           'precio': num(px), 'qty': num(qty), 't_log': t}
    ini = RE_INICIO.search(line)
    if ini:
        return 'inicio', {'version': ini.group(1), 't_log': t}
    lat = RE_LATENCIA.search(line)
    if lat:
        fuente, t_pc, t_ev, diff, delay = lat.groups()
        diff_s, delay_s = _intervalo_s(diff), _intervalo_s(delay)
        if diff_s is None or delay_s is None:
            return None
        return 'latencia', {'fuente': fuente, 't_pc_utc': _hora_net(t_pc), 't_evento_utc': _hora_net(t_ev),
                            'diff_s': diff_s, 'delay_s': delay_s, 't_log': t}
    o = RE_ORDER.search(line)
    if o:
        ev, ext, side, otype, sym, acc, lim, stp, state, tag = o.groups()
        return 'orden', {'cuenta': acc, 'simbolo': sym, 'ext_id': ext, 'lado': side, 'tipo': otype,
                         'limite': num(lim, 0.0), 'stop': num(stp, 0.0), 'etiqueta': tag or '',
                         'estado': state, 't_log': t,
                         'origen': 'registered' if ev == 'Order registered' else 'changed'}
    c = RE_CANCEL.search(line)
    if c:
        ext, side, otype, sym, acc, lim, stp, tag = c.groups()
        return 'cancel', {'cuenta': acc, 'simbolo': sym, 'ext_id': ext, 'lado': side,
                          'etiqueta': tag or '', 't_log': t}
    p = RE_PORTFOLIO.search(line)
    if p:
        acc, bal, opnl, cpnl = p.groups()
        return 'equity', {'cuenta': acc, 't_log': t, 'bal': num(bal, 0.0),
                          'pnl_abierto': num(opnl, 0.0), 'pnl_cerrado': num(cpnl, 0.0)}
    ps = RE_POSITION.search(line)
    if ps:
        acc, sym, vol = ps.groups()
        return 'posicion', {'cuenta': acc, 'simbolo': sym, 'volumen': num(vol, 0.0), 't_log': t}
    return None


def ficheros_log(logs_dir):
    if not os.path.isdir(logs_dir):
        return []
    return sorted(n for n in os.listdir(logs_dir) if n.startswith('app_') and n.endswith('.log'))


def leer_incremental(path, offset):
    """([(posición en bytes de la línea, texto)] de las líneas completas nuevas, nuevo offset). La posición es el orden
    de línea dentro del fichero (en el mismo ms manda; reloj.py y spec §1). Si el fichero encoge, se relee desde 0."""
    size = os.path.getsize(path)
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    with open(path, 'rb') as f:
        f.seek(offset)
        chunk = f.read()
    cut = chunk.rfind(b'\n')
    if cut < 0:
        return [], offset
    lineas, pos = [], offset
    for crudo in chunk[:cut].split(b'\n'):
        lineas.append((pos, crudo.decode('utf-8', errors='ignore').rstrip('\r')))
        pos += len(crudo) + 1
    return lineas, offset + cut + 1
