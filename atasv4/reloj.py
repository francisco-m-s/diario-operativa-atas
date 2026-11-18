"""Reloj ATAS (28/09/2026; tramos y orden de línea, 29/09/2026). Las horas del log de ATAS son del reloj del PC; la hora
buena es la del exchange (ATAS/Rithmic). Decisión del operador: si hay desfase, se corrige hacia la del exchange.

desfase = hora del log − hora del exchange (incluye la latencia hasta ATAS), uno por TRAMO del reloj del PC.

Tramos. El reloj del PC salta entre arranques de ATAS (+3,9 s el 21/09; −0,4…−0,9 el 22/09; +1,5…+1,2 el 23/09; −0,7 el
24/09; +7,3 el 28/09) y también con ATAS abierto, cuando w32time lo corrige: −3,465 s el 21/09 a las 21:16:02 y −7,131 s
el 28/09 a las 17:32:17. Con un desfase por sesión, tras un salto así un fill que solo estaba en el log se fechaba 7 s
antes, adelantaba a otro real de MyTrade y creaba una operación fantasma (verificación del 28/09, defecto A1). Un tramo
empieza en cada 'Started v…' y en cada salto, que ATAS delata con una línea 'Latency manager' de latencia negativa
(|delay| >= reloj.salto_min_s) en una conexión de reloj.fuente_latencia (Rithmic; la hora de Binance deriva por su
cuenta y no sirve). Las líneas del mismo salto (una por conexión) son un solo corte.

Muestras de un tramo:
  - exactas (ms): 'Processing trade' (ATAS 7) cruzado por Id, y las 'Latency manager' de reloj.fuente_latencia
    (Rithmic): time − event para el tramo que empieza y −diff (lo que ATAS medía justo antes) para el que acaba;
  - si el tramo no tiene exactas: MyTrade.cdb, que trunca la hora al segundo (verificado: 110 de 110 fills del
    22–23/09): muestra = log − MyTrade − resolución/2.
Mediana: robusta a la latencia variable y a alguna línea repetida al reconectar; las repeticiones lejanas
(|muestra| > reloj.desfase_max_s) ni cuentan. Dentro de un tramo el reloj deriva ~0,1–0,2 s/h, por debajo de las
tolerancias en juego (1–2 s). Las cuentas sim no cuentan: su hora de 'exchange' la pone el simulador.

Tramo sin muestras (arranque sin operar, o primer fill antes de que ATAS lo escriba en MyTrade): el del tramo con
muestras más cercano del mismo día (Madrid) y sin un salto de por medio; si no hay, reloj.desfase_por_defecto_s. Nunca
se hereda de otro día ni a través de un salto. En cuanto el tramo tiene muestras usa las suyas: por eso en SQLite se
guardan horas crudas del log/PC y la corrección se aplica al usarlas.

Orden de línea. Tras un salto hacia atrás el reloj del PC repite unos segundos: una línea escrita antes del salto es de
su tramo por su posición en el fichero (fichero, pos), no por su hora. En el mismo ms también manda la posición.
"""

import re
from bisect import bisect_right
from collections import defaultdict, namedtuple
from datetime import timedelta
from statistics import median

from . import fuentes
from .fuentes import fmt_ts, parse_ts

INF = float('inf')

Corte = namedtuple('Corte', 't_log fichero pos tipo')      # tipo: 'inicio' ('Started v…') | 'salto' ('Latency manager')


def _corte(c):
    return c if isinstance(c, Corte) else Corte(c, None, None, 'inicio')


class Reloj:
    """Horas del reloj del PC (log de ATAS, o 'ahora') <-> hora del exchange. Sin datos: desfase 0 (identidad).
    'Hora corregida' = hora del log pasada a la del exchange (Madrid) con el desfase de su tramo."""

    def __init__(self, cortes=('',), desfases=(0.0,), detalle=None, margen_salto_s=0.0):
        self.cortes = [_corte(c) for c in cortes] or [Corte('', None, None, 'inicio')]
        self._t = [c.t_log for c in self.cortes]
        self.desfases = list(desfases)
        self.detalle = detalle or [{'inicio': c.t_log, 'tipo': c.tipo, 'desfase_s': d, 'muestras': 0, 'fuente': ''}
                                   for c, d in zip(self.cortes, self.desfases)]
        self.dmin, self.dmax = min(self.desfases), max(self.desfases)
        # segundos que el reloj del PC repite tras un salto hacia atrás (más reloj.margen_salto_s, por la línea
        # 'Latency manager', que llega con el primer dato de mercado tras el salto): hasta ahí pueden desordenarse
        # las horas crudas
        saltos = [abs(self.desfases[i] - self.desfases[i - 1]) for i in range(1, len(self.cortes))
                  if self.cortes[i].tipo == 'salto']
        self.ambiguedad_s = max(saltos) + margen_salto_s if saltos else 0.0

    @property
    def inicios(self):
        return self._t

    def tramo(self, t_log, fichero=None, pos=None):
        """Índice del tramo de una hora del log. Con (fichero, pos), una línea anterior a un corte de su mismo fichero
        es del tramo anterior aunque su hora sea posterior (lo escrito antes de un salto hacia atrás)."""
        i = bisect_right(self._t, t_log) - 1
        if fichero is not None and pos is not None:
            while (i > 0 and self.cortes[i].fichero == fichero and self.cortes[i].pos is not None
                   and pos < self.cortes[i].pos):
                i -= 1
        return max(i, 0)

    def desfase(self, t_log, fichero=None, pos=None):
        return self.desfases[self.tramo(t_log, fichero, pos)]

    def a_exch(self, t_log, fichero=None, pos=None):
        """Hora del log (Madrid, reloj del PC) -> hora corregida (Madrid, exchange)."""
        return fmt_ts(parse_ts(t_log) - timedelta(seconds=self.desfase(t_log, fichero, pos)))

    def a_log(self, t_madrid):
        """Hora del exchange (Madrid) -> la que marcaría el log. El tramo se busca con la hora del exchange: solo cambia
        algo en los segundos de un corte."""
        return fmt_ts(parse_ts(t_madrid) + timedelta(seconds=self.desfase(t_madrid)))

    def log_a_utc(self, t_log, fichero=None, pos=None):
        """Hora del log (Madrid, reloj del PC) -> UTC del exchange."""
        return fmt_ts(fuentes.madrid_a_utc(parse_ts(self.a_exch(t_log, fichero, pos))))

    def utc_a_log(self, t_utc):
        """UTC del exchange -> hora del log (Madrid, reloj del PC)."""
        return self.a_log(fmt_ts(fuentes.utc_a_madrid(t_utc)))

    def pc_a_utc(self, t_pc_utc):
        """Reloj del PC en UTC ('ahora', lecturas de la preoperativa) -> UTC del exchange, con el tramo de esa hora."""
        return self.log_a_utc(fmt_ts(fuentes.utc_a_madrid(t_pc_utc)))

    def clave(self, t_log, fichero=None, pos=None, sin_pos=-1):
        """Orden de un evento del log: hora corregida y, en el mismo ms, su línea. sin_pos es la posición supuesta de
        una fila sin ella (guardada antes del 29/09/2026 y no releída): -1 = antes que las líneas de su ms; INF = después."""
        return self.a_exch(t_log, fichero, pos), (pos if pos is not None else sin_pos)

    def _tramo_unico(self, desde, hasta):
        """Índice del tramo si todo lo que puede caer en [desde, hasta] (horas corregidas) es de un solo tramo, lejos
        de sus cortes (ni el siguiente dentro, ni los segundos repetidos tras el suyo). None si no."""
        lo = fmt_ts(parse_ts(desde) + timedelta(seconds=self.dmin))
        hi = fmt_ts(parse_ts(hasta) + timedelta(seconds=self.dmax))
        i = bisect_right(self._t, lo) - 1
        if i != bisect_right(self._t, hi) - 1:
            return None
        if i > 0 and parse_ts(lo) < parse_ts(self._t[i]) + timedelta(seconds=self.ambiguedad_s):
            return None
        return max(i, 0)

    def filas(self, con, sql, args=(), desde=None, hasta=None, hasta_incluido=True, hora=True):
        """Filas de una tabla del log con hora corregida ('t_c') en [desde, hasta] ([desde, hasta) si no
        hasta_incluido), en orden de línea. `sql` selecciona t_log, fichero y pos y termina en su WHERE: aquí se le
        añade el rango crudo, ampliado con los desfases de todos los tramos, y se filtra después por la hora corregida.
        Así un salto del reloj dentro de la ventana no la recorta. Si la ventana cae entera en un tramo, el filtro es
        exacto en SQL (hora cruda = corregida + desfase) y no se convierte fila a fila; con hora=False ni se calcula
        't_c' (MAE/MFE: miles de filas por ciclo)."""
        args = list(args)
        i = self._tramo_unico(desde, hasta) if (desde is not None and hasta is not None) else None
        if i is not None:
            d = timedelta(seconds=self.desfases[i])
            sql += ' AND t_log>=? AND t_log' + ('<=?' if hasta_incluido else '<?') + ' ORDER BY t_log, pos'
            args += [fmt_ts(parse_ts(desde) + d), fmt_ts(parse_ts(hasta) + d)]
            out = [dict(r) for r in con.execute(sql, args)]
            if hora:
                for r in out:
                    r['t_c'] = fmt_ts(parse_ts(r['t_log']) - d)
            return out
        if desde is not None:
            sql += ' AND t_log>=?'
            args.append(fmt_ts(parse_ts(desde) + timedelta(seconds=self.dmin)))
        if hasta is not None:
            sql += ' AND t_log<=?'
            args.append(fmt_ts(parse_ts(hasta) + timedelta(seconds=self.dmax)))
        out = []
        for r in con.execute(sql, args):
            d = dict(r)
            d['t_c'] = self.a_exch(d['t_log'], d.get('fichero'), d.get('pos'))
            if desde is not None and d['t_c'] < desde:
                continue
            if hasta is not None and (d['t_c'] > hasta or (not hasta_incluido and d['t_c'] == hasta)):
                continue
            out.append(d)
        out.sort(key=lambda d: (d['t_c'], d['pos'] if d.get('pos') is not None else -1))
        return out


# ---------------------------------------------------------------------------
# Estimación desde SQLite
# ---------------------------------------------------------------------------

_CACHE = {}


def _firma(con):
    """Huella barata (agregados en SQL) de lo que alimenta al reloj. Si no cambia, el reloj ya calculado vale."""
    return tuple(con.execute(
        'SELECT (SELECT COUNT(*) FROM fills_log), (SELECT COUNT(t_exch) FROM fills_log), '
        '(SELECT TOTAL(julianday(t_log)) FROM fills_log), (SELECT TOTAL(pos) FROM fills_log), '
        '(SELECT COUNT(*) FROM fills), (SELECT TOTAL(julianday(t_utc)) FROM fills), '
        '(SELECT COUNT(*) FROM sesiones_atas), (SELECT TOTAL(julianday(t_log)) FROM sesiones_atas), '
        '(SELECT TOTAL(pos) FROM sesiones_atas), '
        '(SELECT COUNT(*) FROM latencias), (SELECT TOTAL(julianday(t_log)) FROM latencias), '
        '(SELECT TOTAL(pos) FROM latencias), (SELECT TOTAL(delay_s) FROM latencias)').fetchone())


def cargar(con, R):
    """Reloj estimado desde SQLite (tablas sesiones_atas, latencias, fills_log y fills). Se reutiliza mientras sus datos
    no cambien: el ciclo de 2 s lo pide varias veces (cálculo, vigilancia, cuentas planas) sin ingesta de por medio."""
    clave = (_firma(con), tuple(sorted((k, str(v)) for k, v in R['reloj'].items())),
             tuple(R.get('patrones_sim') or []))
    hit = _CACHE.get(id(con))
    if hit is not None and hit[0] == clave:
        return hit[1]
    rel = _calcular(con, R)
    _CACHE[id(con)] = (clave, rel)
    return rel


def _heredable(cortes, i, j):
    """El tramo i (sin muestras) puede tomar el desfase del j: mismo día (Madrid) y sin un salto de por medio."""
    if cortes[i].t_log[:10] != cortes[j].t_log[:10]:
        return False
    a, b = sorted((i, j))
    return not any(cortes[k].tipo == 'salto' for k in range(a + 1, b + 1))


def _calcular(con, R):
    C = R['reloj']
    sim = R.get('patrones_sim') or []
    maximo = float(C['desfase_max_s'])
    salto_min = float(C['salto_min_s'])
    agrupar = float(C['salto_agrupar_s'])
    fuente_muestra = C['fuente_latencia']

    cortes = [Corte(r['t_log'], r['fichero'], r['pos'], 'inicio')
              for r in con.execute('SELECT t_log, fichero, pos FROM sesiones_atas')]
    # Solo las conexiones de reloj.fuente_latencia (Rithmic) cortan y miden. La hora de Binance tiene su propia
    # deriva: el 23/09 a las 18:10:43 dio delay −1,28 s sin salto del PC (Rithmic, +1,3 s a los dos lados) y el
    # 28/09 su diff pasó de 8,09 a 7,50 y 7,13 s mientras Rithmic medía 7,13.
    lat = [dict(r) for r in con.execute('SELECT * FROM latencias ORDER BY t_log, pos')
           if re.search(fuente_muestra, r['fuente'])]
    ultimo = None
    for l in lat:
        l['salto'] = abs(l['delay_s']) >= salto_min
        if not l['salto']:
            continue
        if ultimo is not None and (parse_ts(l['t_log']) - parse_ts(ultimo)).total_seconds() <= agrupar:
            continue                                   # el mismo salto, visto por otra conexión
        cortes.append(Corte(l['t_log'], l['fichero'], l['pos'], 'salto'))
        ultimo = l['t_log']
    cortes.sort(key=lambda c: (c.t_log, c.pos if c.pos is not None else -1))
    if not cortes:
        cortes = [Corte('', None, None, 'inicio')]
    base = Reloj(cortes, [0.0] * len(cortes))          # solo para asignar cada línea a su tramo

    exactas, truncadas = defaultdict(list), defaultdict(list)
    q = ('SELECT l.cuenta, l.t_log, l.t_exch, l.fichero, l.pos, f.t_utc FROM fills_log l '
         'LEFT JOIN fills f ON f.cuenta=l.cuenta AND f.fill_id=l.fill_id')
    for r in con.execute(q):
        if not r['t_log'] or any(re.search(p, r['cuenta']) for p in sim):
            continue
        t_log_utc = fuentes.madrid_a_utc(parse_ts(r['t_log']))
        if r['t_exch']:
            d, destino = (t_log_utc - parse_ts(r['t_exch'])).total_seconds(), exactas
        elif r['t_utc']:
            d = (t_log_utc - parse_ts(r['t_utc'])).total_seconds() - float(C['resolucion_mytrade_s']) / 2
            destino = truncadas
        else:
            continue
        if abs(d) <= maximo:
            destino[base.tramo(r['t_log'], r['fichero'], r['pos'])].append(d)
    for l in lat:
        i = base.tramo(l['t_log'], l['fichero'], l['pos'])
        d = (parse_ts(l['t_pc_utc']) - parse_ts(l['t_evento_utc'])).total_seconds()
        if abs(d) <= maximo:
            exactas[i].append(d)                         # el tramo en el que se escribió la línea
        if l['salto'] and i > 0 and cortes[i].tipo == 'salto' and abs(l['diff_s']) <= maximo:
            exactas[i - 1].append(-l['diff_s'])          # lo que ATAS medía antes del salto: el tramo que acaba

    propios = {}
    for i in range(len(cortes)):
        m = exactas[i] or truncadas[i]
        if m:
            propios[i] = (round(median(m), 3), len(m), 'exchange (ms)' if exactas[i] else 'MyTrade (al segundo)')
    desfases, detalle = [], []
    for i, c in enumerate(cortes):
        if i in propios:
            d, n, fuente = propios[i]
        else:
            cand = [j for j in propios if _heredable(cortes, i, j)]
            if cand:
                j = min(cand, key=lambda k: abs((parse_ts(cortes[k].t_log) - parse_ts(c.t_log)).total_seconds()))
                d, n = propios[j][0], 0
                fuente = f"heredado {'de la sesión' if cortes[j].tipo == 'inicio' else 'del salto'} {cortes[j].t_log}"
            else:
                d, n, fuente = float(C['desfase_por_defecto_s']), 0, 'por defecto'
        desfases.append(d)
        detalle.append({'inicio': c.t_log, 'tipo': c.tipo, 'desfase_s': d, 'muestras': n, 'fuente': fuente})
    return Reloj(cortes, desfases, detalle, float(C['margen_salto_s']))
