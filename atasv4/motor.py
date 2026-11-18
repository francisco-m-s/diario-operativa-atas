"""Motor de cálculo V4: SQLite -> operaciones (una por cuenta real) + resumen BAL por cuenta.

Todo número de negocio sale de reglas.yaml (argumento `R`). Las funciones puras de reglas
(sesión, margen, threshold, ratio, racha, observaciones) están separadas para los tests.
"""

import bisect
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import cmp_to_key

from . import almacen, cortafuegos, cuentas as mcuentas, fuentes, reloj as mreloj
from .fuentes import CHICAGO, parse_ts

EPS = 1e-9


# ===========================================================================
# Tiempo y sesiones CME
# ===========================================================================

from . import v5 as mv5  # noqa: E402  (reglas de la V5, spec §16)


def sesion_cme(t_utc):
    """Día = sesión CME, que cambia a las 17:00 hora de Chicago (spec §2)."""
    dt = parse_ts(t_utc).replace(tzinfo=timezone.utc).astimezone(CHICAGO)
    return (dt + timedelta(hours=7)).date()


def inicio_sesion_utc(d):
    return almacen.inicio_utc(d)


def es_habil(d):
    """L–V; los festivos CME cuentan como hábiles (spec §2)."""
    return d.weekday() < 5


def habil_anterior(d):
    p = d - timedelta(days=1)
    while not es_habil(p):
        p -= timedelta(days=1)
    return p


def serial_sheets(dt):
    """datetime naive -> número de serie de Google Sheets (días desde 1899-12-30)."""
    return (dt - datetime(1899, 12, 30)).total_seconds() / 86400.0


# ===========================================================================
# Reglas puras
# ===========================================================================

def raiz_simbolo(simbolo):
    """'MNQZ6@CME' -> 'MNQ'."""
    s = simbolo.split('@')[0]
    m = re.match(r'^([A-Z0-9]+?)[FGHJKMNQUVXZ]\d{1,2}$', s)
    return m.group(1) if m else s


def info_simbolo(simbolo, R):
    return R['simbolos'].get(raiz_simbolo(simbolo))


def comision_por_lado(firma, simbolo, R):
    raiz = raiz_simbolo(simbolo)
    for c in R['comisiones']:
        if c['firma'] == firma and c['simbolo'] in (raiz, '*'):
            return c['por_lado']
    return None


def _por_tabla(tabla, margen, R):
    """Primer umbral que se cumple: `min` (margen >= min) o `mayor_que` (margen > N; spec §10.1 y §10.2: 141 ya no da contrato)."""
    for t in tabla:
        if ('min' in t and margen >= t['min']) or ('mayor_que' in t and margen > t['mayor_que']):
            return t['contratos']
    return R['margen']['contratos_por_defecto']


def desde_plan4(R):
    """Primera sesión CME con las reglas del Plan 4 (spec §10; `plan4.desde` en reglas.yaml)."""
    return date.fromisoformat(str(R['plan4']['desde']))


def regla_nueva(sesion, R):
    """Las reglas nuevas del Plan 4 rigen para lo que se abre en la sesión CME `sesion` (spec §10, decisión del operador del
    07/10/2026: solo desde `plan4.desde`; lo anterior conserva lo que daban las reglas de antes)."""
    return sesion >= desde_plan4(R)


def sesion_de(f):
    """Sesión CME de la apertura de una fila, cerrada o abierta."""
    return f.sesion or sesion_cme(f.t_apertura_utc)


def contratos_permitidos(margen, R, nuevo=True):
    """Tabla general (sims; y símbolos sin tabla propia en las reales): la de §5/§10.2 (141 o menos -> 0) con `nuevo`, y la de
    antes del Plan 4 (`plan4.antes.margen_contratos`, 141 daba 1) si no."""
    return _por_tabla(R['margen']['contratos'] if nuevo else R['plan4']['antes']['margen_contratos'], margen, R)


# ---------------------------------------------------------------------------
# Reales (Apex PA, spec §8) y pruebas (spec §9), 30/09/2026
# ---------------------------------------------------------------------------

def ticks_nq(importe, R):
    """Importe en $ -> ticks de NQ (unidad de los umbrales de §8 y §9)."""
    return importe / R['simbolos'][R['unidad_ticks']]['valor_tick']


def usd_de_ticks(ticks, R):
    return ticks * R['simbolos'][R['unidad_ticks']]['valor_tick']


def fmt_es(x, dec=2):
    """Número es_ES para textos: 26025 -> '26.025'; 25817.7 -> '25.817,7'; −75 -> '-75' (sin ceros de sobra)."""
    s = f"{round(x, dec):,.{dec}f}".replace(',', '\x00').replace('.', ',').replace('\x00', '.')
    return s.rstrip('0').rstrip(',') if dec else s


def limite_real(simbolo, margen, levantado, R, nuevo=True):
    """Contratos permitidos de una cuenta real en ese símbolo. Con `nuevo` (spec §8.2, §10.1): MNQ por margen (más de 141 para
    1), NQ 0 siempre; sin él, la tabla de antes del Plan 4 (`plan4.antes.apex_pa_contratos`: 141 daba 1, NQ incluido). Con el
    margen levantado, 1 MNQ y 0 NQ. Un símbolo sin tabla en apex_pa sigue la tabla general."""
    A = R['apex_pa']
    raiz = raiz_simbolo(simbolo)
    if levantado and raiz in A['margen_levantado']['contratos']:
        return A['margen_levantado']['contratos'][raiz]
    tabla = (A['contratos'] if nuevo else R['plan4']['antes']['apex_pa_contratos']).get(raiz)
    return contratos_permitidos(margen, R, nuevo) if tabla is None else _por_tabla(tabla, margen, R)


def es_levantado(contador, margen, R, nuevo=True):
    """Margen levantado: contador de inactividad ≥ 20 y margen ≤ 141 con `nuevo` (spec §8.4, §10.1) o < 141 (antes del Plan 4)."""
    L = R['apex_pa']['margen_levantado']
    if nuevo:
        return contador >= L['contador_min'] and margen <= L['margen_max']
    return contador >= L['contador_min'] and margen < R['plan4']['antes']['margen_levantado_menor_que']


def dias_validos(netos_dia, ticks, R):
    """Sesiones con el neto del día ≥ `ticks` de NQ (al céntimo; la igualdad cuenta)."""
    umbral = round(usd_de_ticks(ticks, R), 2)
    return {d for d, v in netos_dia.items() if round(v, 2) >= umbral}


def base_contador(dias50, sesion, alta, incluir=False):
    """Día desde el que cuenta la inactividad (spec §8.3): el más antiguo de los dos últimos días +50 anteriores a
    `sesion` (con `incluir`, también ella si ya es +50). Con menos de dos, el que haya; sin ninguno, el alta."""
    previos = [d for d in sorted(dias50) if d < sesion or (incluir and d == sesion)][-2:]
    return previos[0] if previos else alta


def estado_inactividad(contador, levantado, R):
    I = R['apex_pa']['inactividad']
    if levantado:
        return I['estados']['levantado']
    return I['estados']['dormant'] if contador >= I['dormant_desde'] else I['estados']['ok']


def inactividad_firma(regla, sesiones_operadas, alta, sesion_actual):
    """Texto de la inactividad propia de una firma (spec §10.1, 11/11/2026; E2T: cierre tras 5 días de trading seguidos sin
    operar). `sesiones_operadas`: sesiones CME con alguna operación de la cuenta, gane o pierda (también la abierta ahora).
    Contador = días hábiles (es_habil) posteriores a la última sesión operada, hasta la sesión en curso incluida; con una
    operación hoy vale 0. Sin ninguna, cuenta desde el alta (el día del alta es el 1). Límite = último día hábil en que operar
    evita llegar a `limite_dias` días seguidos de ausencia: el hábil nº `limite_dias` tras la última operación."""
    previas = [d for d in sesiones_operadas if d <= sesion_actual]
    ultima = max(previas) if previas else habil_anterior(alta)
    n, d = 0, ultima
    while d < sesion_actual:
        d += timedelta(days=1)
        n += es_habil(d)
    limite, k = ultima, 0
    while k < regla['limite_dias']:
        limite += timedelta(days=1)
        k += es_habil(limite)
    E = regla['estados']
    estado = (E['vencido'] if n > regla['limite_dias'] else E['ultimo'] if n == regla['limite_dias']
              else E['aviso'] if n >= regla['aviso_desde'] else E['ok'])
    return regla['formato'].format(n=n, max=regla['limite_dias'], limite=f"{limite:%d/%m}", estado=estado)


def nq_equivalentes(posicion, R):
    """Posición abierta {raíz: contratos} -> NQ-equivalentes (spec §9.3: NQ 1, MNQ 0,1; sin peso no suma)."""
    peso = R['pruebas']['contratos']['peso']
    return round(sum(abs(q) * peso[r] for r, q in posicion.items() if r in peso), 6)


def texto_permitidos(limites, R):
    """{raíz: contratos} -> '1 NQ / 3 MNQ' (columna BAL 'Contratos permitidos', spec §7.1)."""
    V = R['vista_bal']
    return V['permitidos_separador'].join(V['permitidos'].format(n=fmt_es(n), simbolo=s) for s, n in limites.items())


def objetivo_prueba(firma, tamano, R):
    """Balance objetivo de una prueba (spec §9.5) o None si su firma/tamaño no tiene."""
    tabla = R['pruebas']['objetivo'].get(firma) or {}
    return tabla.get(int(tamano)) if tamano is not None else None


def objetivo_efectivo(objetivo, balance_inicial, mejor_dia, fraccion):
    """E2T (spec §9.5): máx(objetivo, balance inicial + mejor día ÷ 0,3). Sin regla de consistencia, el objetivo."""
    if objetivo is None or fraccion is None or mejor_dia is None:
        return objetivo
    return max(objetivo, balance_inicial + mejor_dia / fraccion)


def consumo_margen(res_ticks, mae_ticks, R):
    """resultado_ticks × 0,2 − |MAE_ticks| (spec §5). Sin telemetría no hay MAE que restar."""
    return res_ticks * R['margen']['factor_resultado'] - (abs(mae_ticks) if mae_ticks is not None else 0.0)


def ratio_br(res_ticks, sl_max, tp_final, tp_llenado, margen_apertura, R):
    """Ratio B/R estático (spec §4.2)."""
    den = sl_max if (sl_max is not None and sl_max > 0) else margen_apertura
    if den is None or den <= 0:
        return None
    if res_ticks > 0 and (tp_final is None or not tp_llenado):
        num = res_ticks
    elif res_ticks <= 0 and tp_final is None:
        num = R['ratio']['tp_implicito_ticks']
    else:
        num = tp_final
    return num / den


def siguiente_racha_v5(racha, ticks, R):
    """Racha con signo de la V5 (spec §16): por ticks, con su banda (ganadora >= +20 t, perdedora <= −20 t, neutra no mueve)."""
    c = mv5.categoria(ticks, R)
    if c == 'G':
        return racha + 1 if racha > 0 else 1
    if c == 'P':
        return racha - 1 if racha < 0 else -1
    return racha


def siguiente_racha(racha, neto, R):
    """Racha con signo en $ netos: ≥ +50 suma ganadora, ≤ −10 suma perdedora, entre medias neutra."""
    if neto >= R['racha']['ganadora_min']:
        return racha + 1 if racha > 0 else 1
    if neto <= R['racha']['perdedora_max']:
        return racha - 1 if racha < 0 else -1
    return racha


def threshold(manual, ancla, pico, regla):
    """máx(manual, mín(tope, máx(ancla previa, pico − drawdown))) (spec §6). Nunca baja: un ancla
    que venía de un manual por encima del tope no se recorta al tope."""
    base = pico - regla['drawdown']
    if ancla is not None:
        base = max(ancla, base)
    thr = min(regla['tope'], base)
    if manual is not None:
        thr = max(manual, thr)
    if ancla is not None:
        thr = max(ancla, thr)
    return round(thr, 2)


def bono_sesion(sesion, previa, consumo, neto, levantada, R):
    """Bono de margen al empezar la sesión hábil `sesion`, según la sesión hábil anterior `previa` de la cuenta.
    consumo y neto: {sesión: valor} acumulados hasta ese momento (una sesión sin operaciones no figura en `consumo`).
    Antes de `plan4.desde`, la regla de §5: +140 si el consumo de la previa fue ≥ 0 o no hubo operaciones. Desde
    esa sesión, la de §10.2: sin operaciones +140; con operaciones, +70 si el neto del día fue ≥ +20 t de NQ (100 $) y +0 si
    menos. `levantada`: la previa fue un día +50 con el margen levantado ("suma lo máximo"), que conserva su +140."""
    M = R['margen']
    B = M['bono']
    if not regla_nueva(sesion, R):
        return M['bono_sesion'] if consumo.get(previa, 0.0) >= 0 else 0
    if levantada:
        return R['apex_pa']['margen_levantado']['bono']
    if previa not in consumo:
        return B['sin_operaciones']
    return B['con_neto'] if round(neto.get(previa, 0.0), 2) >= round(usd_de_ticks(B['neto_ticks'], R), 2) else B['sin_neto']


def margen_timeline(filas, alta, sesion_actual, R, suma_maxima=None):
    """Margen por cuenta como línea de tiempo (spec §5, §10.2).

    filas: objetos con .t_apertura_utc, .t_cierre_utc, .sesion, .consumo, .neto (orden cualquiera).
    Devuelve (función margen_en(t_utc), margen actual, {sesión: consumo}).
    Bono al inicio de cada sesión hábil posterior al alta según la sesión hábil anterior (`bono_sesion`). Sábados y
    domingos no suman.
    suma_maxima(sesión, margen al empezarla) -> bool (cuentas reales, spec §8.4: día +50 con el margen levantado): si
    es cierto, cada fila de esa sesión aporta máx(0, consumo) (se guarda en .consumo), así que la siguiente sesión
    hábil recibe el +140. Se decide una vez por sesión, al empezarla (o en su primera fila si no hay inicio hábil).
    """
    decidida = {}

    def suma_max(d):
        if suma_maxima is None:
            return False
        if d not in decidida:
            decidida[d] = bool(suma_maxima(d, m))
        return decidida[d]

    eventos = []
    d = alta + timedelta(days=1)
    while d <= sesion_actual:
        if es_habil(d):
            eventos.append((inicio_sesion_utc(d), 0, d))
        d += timedelta(days=1)
    for f in filas:
        eventos.append((f.t_cierre_utc, 1, f))
    eventos.sort(key=lambda e: (e[0], e[1]))
    consumo, neto = defaultdict(float), defaultdict(float)
    m = float(R['margen']['inicial'])
    tiempos, valores = [], []
    for t, tipo, x in eventos:
        if tipo == 0:
            previa = habil_anterior(x)
            m += bono_sesion(x, previa, consumo, neto, decidida.get(previa, False), R)
            suma_max(x)
        else:
            if suma_max(x.sesion):
                x.consumo = max(0.0, x.consumo)
            m += x.consumo
            consumo[x.sesion] += x.consumo
            neto[x.sesion] += x.neto
        tiempos.append(t)
        valores.append(m)
    inicial = float(R['margen']['inicial'])

    def margen_en(t_utc, incluir_igual=False):
        i = (bisect.bisect_right if incluir_igual else bisect.bisect_left)(tiempos, t_utc)
        return valores[i - 1] if i else inicial

    return margen_en, m, dict(consumo)


def etiqueta_duracion(dur_s, mae, mfe, sl, tp, R):
    O = R['observaciones']
    for c in O['duracion']:
        if dur_s < c['menor_que']:
            return c['etiqueta']
    L = O['larga']
    if mae is None:
        return L['etiqueta']
    if sl is not None and sl > 0 and abs(mae) > L['aguantando_perdida']['mae_pct_sl'] * sl:
        causa = L['aguantando_perdida']['etiqueta']
    elif tp is not None and tp > 0 and mfe is not None and mfe >= L['dejando_correr']['mfe_pct_tp'] * tp:
        causa = L['dejando_correr']['etiqueta']
    else:
        causa = L['sin_gestion']
    return f"{L['etiqueta']} {causa}"


def texto_cambios(nombre, valores):
    v = []
    for x in valores:
        x = round(x, 2)
        if not v or v[-1] != x:
            v.append(x)
    if len(v) < 2:
        return ''
    return f"{nombre} " + '→'.join(_fmt_num(x) for x in v)


def nota_sl_beneficio(valores, R):
    """Nota de SL de una prueba con tope (spec §9.10): ignora los SL en riesgo (ticks > 0) y empieza en el primer SL que
    asegura al menos `beneficio_min_ticks`; desde ahí sigue sus cambios con el beneficio asegurado y su signo, sin hora
    (prefijo y separador en reglas.yaml). Con un solo nivel, ese nivel. Si ningún SL llega a ese beneficio, sin nota."""
    T = R['pruebas']['tope_sl']
    niveles = []
    for x in valores:
        b = round(-x, 2) + 0.0                       # beneficio asegurado (en sl_hist el riesgo es positivo)
        if b >= 0 and (not niveles or niveles[-1] != b):
            niveles.append(b)
    i = next((k for k, b in enumerate(niveles) if b >= T['beneficio_min_ticks']), None)
    if i is None:
        return ''
    return f"{T['prefijo_nota']} " + T['separador_nota'].join(_con_signo(b) for b in niveles[i:])


def _fmt_num(x):
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def cuenta_corta_por_patron(cuenta, R):
    """ID corto de reglas.yaml -> cuenta_corta; None si ningún patrón casa."""
    for p in R['cuenta_corta']:
        m = re.search(p['regex'], cuenta)
        if m:
            out = p['plantilla']
            for i, g in enumerate(m.groups(), 1):
                out = out.replace('{%d}' % i, g)
            return out
    return None


def cuenta_corta(cuenta, R):
    """Sin patrón, los 6 últimos caracteres alfanuméricos. Para un reset (`…-reset1`) saldría `RESET1` en todas las cuentas y los
    ID de operación chocarían (op_id es único): cuentas.reset exige un patrón de cuenta_corta antes de registrarlo."""
    return cuenta_corta_por_patron(cuenta, R) or re.sub(r'[^A-Za-z0-9]', '', cuenta)[-6:].upper()


# ===========================================================================
# Construcción de operaciones desde fills (FIFO)
# ===========================================================================

@dataclass
class Fila:
    cuenta: str
    simbolo: str
    direccion: int                 # +1 largo, −1 corto
    order_id: str
    entradas: list = field(default_factory=list)
    salidas: list = field(default_factory=list)
    contratos: float = 0.0
    pos_max: float = 0.0
    # derivados
    clave: str = ''
    op_id: str = ''
    sesion: date = None
    t_apertura_utc: str = ''
    t_cierre_utc: str = ''
    t_ent_c: str = None            # horas corregidas (reloj ATAS, Madrid) del primer fill de entrada y del último de salida
    t_sal_c: str = None
    telemetria: bool = False
    bruto: float = 0.0
    ticks: float = 0.0
    comision: float = 0.0
    neto: float = 0.0
    entrada: float = 0.0
    max_amplitud: float = None     # |MAE| + |MFE| en ticks por contrato, con el manual de cada uno (spec §4.2, 01/10/2026)
    tipo_apertura: str = ''
    tipo_cierre: str = ''
    sl_ini: float = None
    sl_max: float = None
    tp_final: float = None
    tp_llenado: bool = False
    sl_hist: list = field(default_factory=list)
    tp_hist: list = field(default_factory=list)
    tope_sl: float = None          # prueba con sesión >= tope_sl.desde (spec §9.10): tope en ticks por contrato que se aplicó
    sl_ini_orden: float = None     # y el SL inicial y el más amplio de la orden, antes del tope
    sl_max_orden: float = None
    sl_cancelado: bool = False
    sin_sl: bool = False           # cortafuegos: sin SL registrado a los N s de la entrada
    cortafuegos: bool = False      # la fila lleva alguna etiqueta de cortafuegos (fondo rojo)
    mae: float = None
    mfe: float = None
    simbolo_ok: bool = True
    comision_ok: bool = True
    faltan: float = 0.0            # contratos sin fill de salida ni pata (operación incompleta)
    incompleta: bool = False
    rescatada: bool = False        # salida tomada de HistoryMyTrade porque falta el fill
    t_cierre_forzado: str = None   # hora corregida del 'vol 0' con el que ATAS la dio por plana
    nombre: str = ''               # nombre visible congelado: retiros registrados antes de la apertura
    mae_log: float = None          # valor del log, nunca se pierde aunque haya manual
    mfe_log: float = None
    mae_manual: bool = False
    mfe_manual: bool = False
    lider: object = None
    copias: list = field(default_factory=list)
    consumo: float = 0.0
    margen_apertura: float = None
    margen_tras: float = None
    permitidos: int = None
    real: bool = False             # cuenta real (spec §8.1): solo una decisión con alguna real suena (spec §4.5)
    sim: bool = False              # cuenta sim (activada para pruebas): ni líder ni copia de nadie (spec §4.1, 01/10/2026)
    levantado: bool = False        # real abierta con el margen levantado (spec §8.4)
    neq: float = None              # prueba: NQ-equivalentes de la cuenta tras esta entrada (spec §9.3)
    neq_max: float = None          # prueba: máximo permitido en NQ-equivalentes (sustituye a `permitidos`)
    dist_ap: float = None          # prueba: distancia al threshold en la apertura, en ticks de NQ (spec §10.3); None = desconocida
    dist_regla: bool = False       # prueba de 25K: los contratos se miden por la distancia (§10.3) y no en NQ-equivalentes
    exceso_dist: str = None        # detalle de CONTRATOS > PERMITIDOS por la distancia o None
    espejo_auto: bool = False      # ESPEJO detectado en el log (spec §10.5), del líder; la seguidora usa el de su líder
    espejo: bool = False           # ESPEJO final (con la corrección manual) y de dónde sale: 'auto' | 'manual'
    mercado: str = ''              # estado del mercado en la apertura (spec §12); la seguidora usa el de su líder
    espejo_origen: str = 'auto'
    etiquetas_cuenta: list = field(default_factory=list)      # preventivos de la cuenta (§8.4, §9.5–§9.7): fondo rojo
    infracciones_cuenta: list = field(default_factory=list)   # [(tipo, detalle)] propias de la cuenta (§9.5, §9.7)
    balance_tras: float = None
    ratio: float = None
    racha: int = 0
    res_anterior: float = None
    obs: str = ''
    duracion: int = 0
    etq_duracion: str = ''         # etiqueta de duración de Observaciones demonio (spec §4.4)
    naranja: bool = False          # V5: la fila lleva algún CONSEJO IGNORADO (fondo naranja; el rojo manda)
    turno: str = None              # V5: turno de operativa ('T1'…, o «turno fantasma»); solo en SQLite
    n_dia: int = None              # nº de la decisión en su día CME (por apertura) y, tras su cierre, acumulado del día
    acum_dia: float = None         # en ticks y ticks perdidos del día (solo cerradas; spec §16.7)
    perdidos_dia: float = None
    proceso_ok: bool = None        # calificación (spec §4.2, 01/10/2026): proceso, categoría del resultado y cuadrante
    categoria_resultado: str = ''
    calificacion: str = ''

    @property
    def completa(self):
        return self.contratos > 0 and abs(sum(s['qty'] for s in self.salidas) + self.faltan - self.contratos) < EPS

    @property
    def es_seguidora(self):
        return self.lider is not None

    @property
    def decision_id(self):
        """ID de la decisión: el de la líder, o el propio. Una líder todavía abierta no tiene ID
        (se asigna al cerrar), y entonces la seguidora responde por sí misma."""
        return (self.lider.op_id or self.op_id) if self.lider else self.op_id


def _t_fill(f):
    """Hora corregida del fill (Madrid, exchange): la de su línea del log pasada por el reloj ATAS o, si no está en el
    log, la del exchange."""
    return f.get('t_c') or fuentes.fmt_ts(fuentes.utc_a_madrid(f['t_utc']))


def construir_filas(fills, planos=(), ahora=None, inc=None, rescate=None, reloj=None):
    """fills de una cuenta/símbolo -> filas. Cada entrada que aumenta la posición es una fila
    (agrupada por orden); las salidas se reparten FIFO; un giro cierra y abre (spec §4.1).

    planos: horas corregidas en que ATAS dijo 'Position changed … vol 0' (_planos). Si los fills dejan posición
    abierta pero ATAS la dio por plana (falta algún fill en MyTrade y en el log), se cierra a la
    fuerza: la salida se toma de HistoryMyTrade (`rescate`) o la fila queda marcada incompleta.
    ahora: hora corregida actual. reloj: reloj ATAS (sin él, horas del log tal cual).
    Orden (29/09/2026, spec §1): si todos los fills están en el log, orden de línea (hora del log corregida y, en el
    mismo ms, posición en el fichero); si falta alguno, hora del exchange. Ordenar por el segundo de MyTrade mezclando
    horas reales con otras estimadas convertía un largo en un corto fantasma cuando el desfase estimado se quedaba
    viejo tras un salto del reloj del PC (A1: APEX-75, 28/09 18:27:36–40).
    """
    rel = reloj or mreloj.Reloj()
    for f in fills:
        if f.get('t_log') and not f.get('t_c'):
            f['t_c'] = rel.a_exch(f['t_log'], f.get('fichero'), f.get('pos'))

    def fino(f):
        """Desempate dentro del segundo de MyTrade: hora del exchange con ms o, sin ella, la del log corregida."""
        if f.get('t_exch'):
            return f['t_exch']
        return fuentes.fmt_ts(fuentes.madrid_a_utc(parse_ts(f['t_c']))) if f.get('t_c') else f['t_utc']

    if fills and all(f.get('t_c') for f in fills):
        fills = sorted(fills, key=lambda f: (f['t_c'], f['pos'] if f.get('pos') is not None else -1, f['fill_id']))
    else:
        fills = sorted(fills, key=lambda f: (f['t_utc'][:19], fino(f), f['fill_id']))
    filas, lots = [], deque()
    pos = 0.0
    abiertas = {}
    planos = sorted(planos)
    tol = timedelta(seconds=inc['tolerancia_s']) if inc else None

    def forzar(t_plano):
        nonlocal pos, abiertas
        for lot in lots:
            fila, rem = lot['fila'], lot['rem']
            for x in (rescate(fila, lot['fill'], rem) if rescate else []):
                fila.salidas.append(x)
                fila.rescatada = True
                rem -= x['qty']
            if rem > EPS:
                fila.faltan += rem
                fila.incompleta = True
            fila.t_cierre_forzado = t_plano
        lots.clear()
        pos, abiertas = 0.0, {}

    def plano_despues(desde, antes_de=None):
        """Primer 'vol 0' del log posterior a `desde` (y anterior a `antes_de`, si se da). Horas corregidas."""
        for p in planos[bisect.bisect_right(planos, desde):]:
            if antes_de is not None and parse_ts(p) >= antes_de:
                return None
            return p
        return None

    prev_t = None
    for f in fills:
        if inc and abs(pos) > EPS and prev_t:
            p = plano_despues(prev_t, parse_ts(_t_fill(f)) - tol)
            if p:
                forzar(p)
        prev_t = _t_fill(f)
        s = 1 if f['lado'] == 'Buy' else -1
        q = float(f['volumen'])
        if abs(pos) > EPS and (pos > 0) != (s > 0):
            cerrar = min(q, abs(pos))
            q -= cerrar
            while cerrar > EPS:
                lot = lots[0]
                take = min(lot['rem'], cerrar)
                lot['fila'].salidas.append({**f, 'qty': take, 'entrada_fill': lot['fill']})
                lot['rem'] -= take
                cerrar -= take
                pos += s * take
                if lot['rem'] <= EPS:
                    lots.popleft()
            if abs(pos) <= EPS:
                pos = 0.0
                abiertas = {}
        if q > EPS:
            fila = abiertas.get(f['order_id'])
            if fila is None:
                fila = Fila(cuenta=f['cuenta'], simbolo=f['simbolo'], direccion=s, order_id=f['order_id'])
                abiertas[f['order_id']] = fila
                filas.append(fila)
            fila.entradas.append({**f, 'qty': q})
            fila.contratos += q
            lots.append({'fila': fila, 'rem': q, 'fill': f})
            pos += s * q
            fila.pos_max = max(fila.pos_max, abs(pos))
    if inc and abs(pos) > EPS and prev_t and ahora:
        p = plano_despues(prev_t)
        if p and parse_ts(ahora) - parse_ts(p) >= timedelta(minutes=inc['espera_min']):
            forzar(p)
    return filas, pos


def _asignar_patas(fila, patas_idx, sim):
    """PnL y ticks reales desde HistoryMyTrade (pata = entrada FIFO -> salida). Lo que no casa
    se calcula con la tabla de símbolos."""
    bruto, ticks_x_qty = 0.0, 0.0
    for s in fila.salidas:
        e = s['entrada_fill']
        k = (e['t_utc'][:19], round(e['precio'], 6), fila.direccion, s['t_utc'][:19], round(s['precio'], 6))
        rem = s['qty']
        for p in patas_idx.get(k, ()):
            if rem <= EPS:
                break
            if p['rem'] <= EPS:
                continue
            take = min(p['rem'], rem)
            bruto += p['pnl'] * take / abs(p['open_vol'])
            ticks_x_qty += p['ticks_pnl'] * take
            p['rem'] -= take
            rem -= take
        if rem > EPS:
            if sim is None:
                fila.simbolo_ok = False
                continue
            mov_ticks = (s['precio'] - e['precio']) * fila.direccion / sim['tick']
            bruto += mov_ticks * sim['valor_tick'] * rem
            ticks_x_qty += mov_ticks * rem
    fila.bruto = round(bruto, 2)
    cerrado = sum(s['qty'] for s in fila.salidas)
    fila.ticks = round(ticks_x_qty / cerrado, 2) if cerrado else 0.0


def _ticks(precio, fila, sim):
    return round((fila.entrada - precio) * fila.direccion / sim['tick'], 2)


def tipo_cierre(orden, R):
    """Tipo cierre del fill de máxima amplitud (spec §4.2): etiqueta SL o cualquier Stop -> SL;
    etiqueta TP -> TP; Limit -> Límite; Market -> Mercado. Orden desconocida -> vacío."""
    if orden is None:
        return ''
    tc = R['tipos_cierre']
    if orden['etiqueta'] == 'SL' or orden['tipo'] in tc['tipos_stop']:
        return tc['sl']
    if orden['etiqueta'] == 'TP':
        return tc['tp']
    return tc['por_tipo'].get(orden['tipo'], '')


def disparo_espejo(ent, cuenta):
    """Disparo del ESPEJO de la cuenta (spec §10.5), en ticks por contrato: real, o prueba según su tamaño. None si no aplica
    (sim, tamaño sin disparo)."""
    D = ent.reglas['espejo']['disparo_ticks']
    if ent.es_real(cuenta):
        return D['real']
    tam = (ent.ficha(cuenta) or {}).get('tamano')
    if ent.es_prueba(cuenta) and tam is not None:
        return D['prueba'].get(int(tam))
    return None


def _enriquecer_log(fila, con, sim, R, reloj=None, disparo=None):
    """SL/TP, tipos de orden, MAE/MFE desde el log. Sin log de todos los fills -> sin telemetría. Las ventanas van en
    hora corregida (reloj ATAS): un salto del reloj del PC dentro de la operación no las recorta. `disparo`: el de
    `disparo_espejo` (ESPEJO automático, spec §10.5)."""
    rel = reloj or mreloj.Reloj()
    tipos = R['tipos_orden']
    ent = [e.get('t_c') for e in fila.entradas]
    sal = [s.get('t_c') or fila.t_cierre_forzado for s in fila.salidas]
    if fila.faltan > EPS:
        sal.append(fila.t_cierre_forzado)
    fila.telemetria = bool(sal) and all(ent) and all(sal)
    if not fila.telemetria:
        return
    fila.t_ent_c, fila.t_sal_c = min(ent), max(sal)

    def orden(ext):
        return con.execute('SELECT * FROM ordenes WHERE cuenta=? AND ext_id=?', (fila.cuenta, ext)).fetchone()

    o = orden(fila.order_id)
    if o:
        fila.tipo_apertura = tipos.get(o['tipo'], o['tipo'])
    # Cierre = tipo del fill de salida más alejado de la entrada
    s_max = _salida_max_amplitud(fila)
    if s_max:
        o = orden(s_max['order_id'])
        fila.tipo_cierre = tipo_cierre(o, R)
    for s in fila.salidas:
        o = orden(s['order_id'])
        if o and o['etiqueta'] == 'TP':
            fila.tp_llenado = True
    if sim is None:            # sin tabla de símbolo no hay conversión a ticks (SL/TP/MAE)
        return

    lado_proteccion = lado_proteccion_de(fila.direccion)
    br = rel.filas(con, "SELECT etiqueta, limite, stop, t_log, fichero, pos FROM ordenes WHERE cuenta=? AND simbolo=? "
                        "AND lado=? AND etiqueta IN ('SL','TP') AND origen='registered'",
                   (fila.cuenta, fila.simbolo, lado_proteccion), desde=fila.t_ent_c, hasta=fila.t_sal_c,
                   hasta_incluido=False)
    sl_regs = [(b['t_c'], b['stop'] or b['limite']) for b in br if b['etiqueta'] == 'SL']
    tp_regs = [(b['t_c'], b['limite'] or b['stop']) for b in br if b['etiqueta'] == 'TP']
    fila.sl_hist = [_ticks(p, fila, sim) for _, p in sl_regs]
    fila.tp_hist = [-_ticks(p, fila, sim) for _, p in tp_regs]
    if fila.sl_hist:
        fila.sl_ini, fila.sl_max = fila.sl_hist[0], max(fila.sl_hist)
    if fila.tp_hist:
        fila.tp_final = fila.tp_hist[-1]

    fila.sl_cancelado = sl_cancelado_sin_reponer(con, fila.cuenta, fila.simbolo, lado_proteccion,
                                                 fila.t_ent_c, fila.t_sal_c, R, rel) is not None
    fila.sin_sl = sin_sl_a_tiempo(fila.t_ent_c, fila.t_sal_c, [t for t, _ in sl_regs], R)

    # MAE / MFE: flotante total de la cuenta durante la operación, en ticks por contrato de esta fila
    conv = sim['valor_tick'] * fila.contratos
    muestras = rel.filas(con, 'SELECT pnl_abierto, t_log, fichero, pos FROM equity WHERE cuenta=?', (fila.cuenta,),
                         desde=fila.t_ent_c, hasta=fila.t_sal_c, hora=False)
    pnls = [r['pnl_abierto'] / conv for r in muestras]
    if disparo is not None:            # ESPEJO (spec §10.5): un TP a >= 0 t dado de alta o modificado tras llegar al disparo
        k = next((i for i, p in enumerate(pnls) if p <= disparo + EPS), None)
        if k is not None:
            t_disparo = rel.a_exch(muestras[k]['t_log'], muestras[k].get('fichero'), muestras[k].get('pos'))
            fila.espejo_auto = any(t > t_disparo and -_ticks(p, fila, sim) >= R['espejo']['tp_min_ticks'] - EPS
                                   for t, p in tp_regs)
    pnls.append(fila.ticks)            # el flotante en el instante del cierre es el resultado
    fila.mae = round(min(0.0, min(pnls)), 1)
    fila.mfe = round(max(0.0, max(pnls)), 1)


def lado_proteccion_de(direccion):
    return 'Sell' if direccion > 0 else 'Buy'


def sl_cancelado_sin_reponer(con, cuenta, simbolo, lado, desde, hasta, R, reloj=None):
    """Hora corregida del primer SL cancelado en [desde, hasta) sin otro SL dado de alta en ≤ N s y con la
    posición todavía abierta más de N s después. Horas corregidas; `hasta` = cierre, o ahora si sigue abierta."""
    rel = reloj or mreloj.Reloj()
    rep = timedelta(seconds=R['observaciones']['sl_reemplazo_s'])
    altas = [parse_ts(r['t_c']) for r in rel.filas(
        con, "SELECT t_log, fichero, pos FROM ordenes WHERE cuenta=? AND simbolo=? AND lado=? AND etiqueta='SL' "
             "AND origen='registered'", (cuenta, simbolo, lado), desde=desde)]
    t_fin = parse_ts(hasta)
    for c in rel.filas(con, "SELECT t_log, fichero, pos FROM cancelaciones WHERE cuenta=? AND simbolo=? AND lado=? "
                            "AND etiqueta='SL'", (cuenta, simbolo, lado), desde=desde, hasta=hasta,
                       hasta_incluido=False):
        tc = parse_ts(c['t_c'])
        if not any(tc < t <= tc + rep for t in altas) and t_fin - tc > rep:
            return c['t_c']
    return None


def sin_sl_a_tiempo(t_entrada, t_fin, altas_sl, R):
    """Cortafuegos SIN SL: a los N s del fill de entrada la posición sigue abierta y no hay SL dado de alta."""
    espera = timedelta(seconds=R['cortafuegos']['sin_sl_s'])
    t0 = parse_ts(t_entrada)
    if parse_ts(t_fin) - t0 <= espera:
        return False
    return not any(t0 <= parse_ts(t) <= t0 + espera for t in altas_sl)


def _salida_max_amplitud(fila):
    """Fill de salida más alejado de la entrada; empate -> gana el negativo (spec §4.2). Interno desde el 01/10/2026: solo
    da el Tipo cierre (la columna 'Máxima amplitud del movimiento' pasó a ser |MAE| + |MFE|)."""
    if not fila.salidas:
        return None
    return max(fila.salidas, key=lambda s: (round(abs(s['precio'] - fila.entrada), 6),
                                            1 if (s['precio'] - fila.entrada) * fila.direccion < 0 else 0))


def _ts_exch(f):
    return f.get('t_exch') or f['t_utc']


def _heredar_de_lider(f, lider):
    """La seguidora no lleva órdenes SL/TP propias (copia por posiciones): hereda las de la líder."""
    for a in ('sl_ini', 'sl_max', 'tp_final', 'tp_llenado', 'sl_hist', 'tp_hist', 'sl_cancelado', 'sin_sl'):
        setattr(f, a, getattr(lider, a))


def topar_sl(f, prueba, R):
    """Tope del SL por el Loss Limit de Rithmic (spec §9.10, 07/10/2026). Solo pruebas (también seguidoras) y operaciones
    con sesión CME >= `pruebas.tope_sl.desde`. Cada SL en riesgo (> 0 t) cuenta como mín(SL, tope), con tope = mín(limite_usd,
    distancia al threshold en $ en la apertura; §10.3, solo desde `plan4.desde`) ÷ (contratos de la fila × valor del tick), o
    limite_usd si la distancia es <= 0 o no se conoce (o si es anterior a esa fecha): `sl_ini` y `sl_max` (columna, ratios, AGUANTANDO PÉRDIDA) salen ya topados.
    `sl_hist` no cambia (la nota de SL lo necesita) y el SL de la orden queda en `sl_ini_orden`/`sl_max_orden`. Se llama
    tras `emparejar_copias`: la seguidora hereda el SL de su líder y recibe el tope con SUS contratos."""
    T = R['pruebas']['tope_sl']
    sim = info_simbolo(f.simbolo, R)
    if not prueba or not f.sl_hist or sim is None or f.sesion < date.fromisoformat(str(T['desde'])):
        return
    limite = T['limite_usd']
    if regla_nueva(f.sesion, R) and f.dist_ap is not None and f.dist_ap > 0:     # §10.3: si la distancia es menor, ese es el riesgo real
        limite = min(limite, usd_de_ticks(f.dist_ap, R))
    f.tope_sl = round(limite / (f.contratos * sim['valor_tick']), 2)
    f.sl_ini_orden, f.sl_max_orden = f.sl_ini, f.sl_max
    topados = [min(x, f.tope_sl) for x in f.sl_hist]       # un SL en beneficio (<= 0 t) queda como está: el tope es > 0
    f.sl_ini, f.sl_max = topados[0], max(topados)


def _dif_aperturas(a, b):
    """Segundos de a − b entre dos fills de apertura, con horas del mismo reloj (spec §4.1, 29/09/2026): la del exchange
    con ms si las dos la tienen; si no, la del log corregida si las dos están en el log; si no, el segundo de MyTrade de
    las dos. Comparar una truncada con una estimada con ms dejaba huérfana una copia real (24/09 18:16:43: 1,121 s para
    0,159 s reales) y la seguidora contaba como decisión propia en el cortafuegos."""
    if a.get('t_exch') and b.get('t_exch'):
        ta, tb = a['t_exch'], b['t_exch']
    elif a.get('t_c') and b.get('t_c'):
        ta, tb = a['t_c'], b['t_c']
    else:
        ta, tb = a['t_utc'][:19] + '.000', b['t_utc'][:19] + '.000'
    return (parse_ts(ta) - parse_ts(tb)).total_seconds()


def _dt_aperturas(a, b):
    return abs(_dif_aperturas(a, b))


def _linea(f):
    """(fichero, posición) de la línea del log del primer fill de entrada, o None si no está en el log."""
    e = f.entradas[0]
    return (e['fichero'], e['pos']) if e.get('fichero') and e.get('pos') is not None else None


def _primero(a, b, figura):
    """Orden para elegir líder entre dos filas (spec §4.1, 01/10/2026): primero la cuenta que figura como líder en el
    CopyTrading.cnf actual (`figura`: ids de fila); si no, la apertura más temprana (mismo reloj); si empatan, la primera
    por orden de línea; si siguen empatadas, por nombre de cuenta."""
    if (id(a) in figura) != (id(b) in figura):
        return -1 if id(a) in figura else 1
    d = _dif_aperturas(a.entradas[0], b.entradas[0])
    if abs(d) > EPS:
        return -1 if d < 0 else 1
    la, lb = _linea(a), _linea(b)
    if la and lb and la != lb:
        return -1 if la < lb else 1
    return (a.cuenta > b.cuenta) - (a.cuenta < b.cuenta)


def _minuto(f):
    return int((parse_ts(_ts_exch(f.entradas[0])) - datetime(1970, 1, 1)).total_seconds() // 60)


def emparejar_copias(filas, pares, R):
    """Copia (spec §4.1, redefinida el 01/10/2026) = operación de OTRA cuenta (no sim), de la misma dirección e
    instrumento, con la apertura a ≤ `copia.ventana_s` de la de su líder, figure o no la relación en CopyTrading.cnf
    (`pares`: la configuración actual, que no reescribe el pasado). Líder: la cuenta que figure como líder en `pares`
    (de cualquier otra cuenta, esté o no en la operación); si ninguna, la de apertura más temprana; desempates: línea del
    log y nombre (`_primero`). Todas las seguidoras se miden contra la apertura de la líder, no en cadena. Cada fila de
    entrada (escalonadas, giros) se empareja con su homóloga: la más cercana de cada otra cuenta."""
    ventana = R['copia']['ventana_s'] + EPS
    lideres = {lider for lider, _ in pares}
    grupos = defaultdict(list)
    for f in sorted(filas, key=lambda f: f.cuenta):
        if not f.sim:
            grupos[(f.simbolo, f.direccion)].append(f)
    for grupo in grupos.values():
        cubos = defaultdict(list)                  # por minuto: solo se miden las filas de los minutos que toca la ventana
        for f in grupo:
            cubos[_minuto(f)].append(f)
        alcance = int(ventana // 60) + 1

        def cercanas(l):
            m = _minuto(l)
            cand = [(_dt_aperturas(l.entradas[0], f.entradas[0]), f) for k in range(m - alcance, m + alcance + 1)
                    for f in cubos.get(k, ()) if f.cuenta != l.cuenta]
            return [(dt, f) for dt, f in cand if dt <= ventana]

        figura = {id(l) for l in grupo if l.cuenta in lideres}
        asignadas = set()
        for l in sorted(grupo, key=cmp_to_key(lambda a, b: _primero(a, b, figura))):
            if id(l) in asignadas:
                continue
            asignadas.add(id(l))
            mejor = {}                             # cuenta -> (dt, fila): su homóloga más cercana
            for dt, f in cercanas(l):
                if id(f) not in asignadas and (f.cuenta not in mejor or dt < mejor[f.cuenta][0]):
                    mejor[f.cuenta] = (dt, f)
            for cuenta in sorted(mejor):
                f = mejor[cuenta][1]
                asignadas.add(id(f))
                f.lider = l
                l.copias.append(f)
                _heredar_de_lider(f, l)


# ===========================================================================
# Orquestación
# ===========================================================================

@dataclass
class ResultadoMotor:
    filas: list                    # todas las filas completas (una por cuenta real)
    bal: list                      # resumen por cuenta viva
    abiertas: dict                 # cuenta -> posición neta abierta (≠ 0)
    avisos: list
    filas_abiertas: list = field(default_factory=list)   # entradas aún sin cerrar (vigilancia en vivo)
    cortafuegos: dict = field(default_factory=dict)      # clave de decisión -> evaluación del cortafuegos
    reloj: object = field(default_factory=mreloj.Reloj)  # reloj ATAS con el que se calculó


def _fills_cuenta(con, cuenta, reloj):
    """Fills de MyTrade.cdb completados con los del log ('Processing trade' / 'Order trade') que falten en el cdb.
    Un 'Order trade' no trae la hora del exchange: hasta que MyTrade lo tenga, se estima con el reloj ATAS. Cada fill
    del log lleva su línea (fichero, pos) y su hora corregida 't_c'."""
    q = ('SELECT f.*, l.t_exch, l.t_log, l.fichero, l.pos FROM fills f LEFT JOIN fills_log l '
         'ON l.cuenta=f.cuenta AND l.fill_id=f.fill_id WHERE f.cuenta=?')
    out = [dict(r) for r in con.execute(q, (cuenta,))]
    vistos = {f['fill_id'] for f in out}
    for r in con.execute('SELECT * FROM fills_log WHERE cuenta=?', (cuenta,)):
        if r['fill_id'] in vistos:
            continue
        out.append({'cuenta': cuenta, 'simbolo': r['simbolo'], 'fill_id': r['fill_id'],
                    'order_id': r['order_id'] or f"log:{r['fill_id']}", 'lado': r['lado'],
                    'precio': r['precio'], 'volumen': r['qty'],
                    't_utc': r['t_exch'] or reloj.log_a_utc(r['t_log'], r['fichero'], r['pos']),
                    't_exch': r['t_exch'], 't_log': r['t_log'], 'fichero': r['fichero'], 'pos': r['pos']})
    for f in out:
        f['t_c'] = reloj.a_exch(f['t_log'], f['fichero'], f['pos']) if f.get('t_log') else None
    return out


def _planos(con, cuenta, simbolo, inc=None, reloj=None):
    """Horas corregidas con la cuenta plana ('Position changed … vol 0'). En un giro (una orden que cierra y abre
    en sentido contrario) ATAS escribe 'vol 0' y, en el mismo instante, el volumen nuevo: ese cero es transitorio
    y no cuenta. Se reconoce porque, dentro de `incompleta.giro_transitorio_s`, aparece un volumen de signo
    contrario al que había antes del cero. Sin el filtro, el cero forzaba el cierre de la posición nueva y el
    fill siguiente abría una operación fantasma (APEX-91120-75, 23/09/2026 18:48:17)."""
    rel = reloj or mreloj.Reloj()
    giro = timedelta(seconds=float((inc or {}).get('giro_transitorio_s', 0)))
    filas = []
    for r in con.execute('SELECT t_log, volumen, fichero, pos FROM posiciones_hist WHERE cuenta=? AND simbolo=?',
                         (cuenta, simbolo)):
        t = rel.a_exch(r['t_log'], r['fichero'], r['pos'])
        filas.append((parse_ts(t), t, r['volumen']))
    filas.sort()
    nz = [(t, v > 0) for t, _, v in filas if abs(v) > EPS]
    nz_t = [t for t, _ in nz]
    out = []
    for t0, t_txt, vol in filas:
        if abs(vol) > EPS:
            continue
        i = bisect.bisect_left(nz_t, t0)                 # no nulos a partir de t0 (incluido)
        if giro and i > 0:
            largo_antes = nz[i - 1][1]                     # último volumen no nulo estrictamente anterior
            j = bisect.bisect_right(nz_t, t0 + giro)
            if any(largo != largo_antes for _, largo in nz[i:j]):
                continue
        out.append(t_txt)
    return out


def _rescate_patas(con, cuenta):
    """Salida sintética desde HistoryMyTrade para contratos cuya salida no está en MyTrade ni en el log."""
    idx = defaultdict(list)
    for r in con.execute('SELECT * FROM patas WHERE cuenta=? ORDER BY close_utc', (cuenta,)):
        p = dict(r)
        p['rem'] = abs(p['open_vol'])
        idx[(p['open_utc'][:19], round(p['open_px'], 6), 1 if p['open_vol'] > 0 else -1)].append(p)

    def rescate(fila, entrada, qty):
        out = []
        for p in idx.get((entrada['t_utc'][:19], round(entrada['precio'], 6), fila.direccion), ()):
            if qty <= EPS:
                break
            take = min(p['rem'], qty)
            if take <= EPS:
                continue
            p['rem'] -= take
            qty -= take
            out.append({'cuenta': fila.cuenta, 'simbolo': fila.simbolo, 'fill_id': f"pata:{p['pata_id']}",
                        'order_id': '', 'lado': 'Sell' if fila.direccion > 0 else 'Buy',
                        'precio': p['close_px'], 'volumen': take, 't_utc': p['close_utc'], 't_exch': None,
                        't_log': None, 'qty': take, 'entrada_fill': entrada})
        return out
    return rescate


def _ahora(reloj, ahora_utc):
    """'Ahora' del reloj del PC (UTC) -> hora corregida (Madrid, exchange), con el tramo en curso."""
    return fuentes.fmt_ts(fuentes.utc_a_madrid(reloj.pc_a_utc(ahora_utc)))


def posiciones_cuenta(ent, con, cuenta, ahora_utc, reloj=None):
    """({símbolo: posición neta}, nº de fills), con los cierres forzados por el log."""
    reloj = reloj or mreloj.cargar(con, ent.reglas)
    por_sim = defaultdict(list)
    fills = _fills_cuenta(con, cuenta, reloj)
    for f in fills:
        por_sim[f['simbolo']].append(f)
    ahora = _ahora(reloj, ahora_utc)
    inc = ent.reglas['incompleta']
    return {s: construir_filas(fl, _planos(con, cuenta, s, inc, reloj), ahora, inc, reloj=reloj)[1]
            for s, fl in por_sim.items()}, len(fills)


def _patas_idx(con, cuenta):
    idx = defaultdict(list)
    for r in con.execute('SELECT * FROM patas WHERE cuenta=? ORDER BY open_utc, close_utc', (cuenta,)):
        p = dict(r)
        p['rem'] = abs(p['open_vol'])
        d = 1 if p['open_vol'] > 0 else -1
        idx[(p['open_utc'][:19], round(p['open_px'], 6), d, p['close_utc'][:19], round(p['close_px'], 6))].append(p)
    return idx


def cuentas_en_juego(ent, con):
    fichas = ent.cuentas['cuentas']
    return sorted(c for c in fichas if not ent.es_sim(c) or ent.sim_activa(c))


# Lo que `_enriquecer_log` saca del log para una operación: es lo que guarda la foto del cálculo (spec §16.10)
CAMPOS_FOTO = ('telemetria', 't_ent_c', 't_sal_c', 'tipo_apertura', 'tipo_cierre', 'tp_llenado', 'sl_hist', 'tp_hist',
               'sl_ini', 'sl_max', 'tp_final', 'sl_cancelado', 'sin_sl', 'espejo_auto', 'mae', 'mfe')


def calcular(ent, con, ahora_utc, pares_copia, manual_thr=None, usar_foto=True, captura=None):
    """Construye todas las filas y el BAL desde SQLite. No escribe en el sheet.
    Foto del cálculo (spec §16.10): para una operación ya archivada, lo que da el log (SL/TP, tipos de orden, MAE/MFE…) y
    la evaluación del cortafuegos de su decisión salen de la foto guardada y no se vuelven a consultar; todo lo que
    encadena la historia (margen, rachas, balance, threshold, reglas de la cuenta) se sigue calculando entero.
    `usar_foto=False` lo calcula todo (para hacer la foto y para comprobarla); `captura`: dict que recibe, por clave, los
    CAMPOS_FOTO de cada operación calculada."""
    R = ent.reglas
    avisos = []
    reloj = mreloj.cargar(con, R)
    sesion_actual = sesion_cme(reloj.pc_a_utc(ahora_utc))      # la sesión CME cambia a las 17:00 CT del exchange
    queme = almacen.quemadas(con)
    todas, abiertas, abiertas_filas = [], {}, []
    ahora = _ahora(reloj, ahora_utc)
    inc = R['incompleta']
    foto = almacen.foto(con) if usar_foto else {'filas': {}, 'evaluar': {}}

    for cuenta in cuentas_en_juego(ent, con):
        ficha = ent.ficha(cuenta)
        real = ent.es_real(cuenta)
        es_sim = ent.es_sim(cuenta)
        por_sim = defaultdict(list)
        for f in _fills_cuenta(con, cuenta, reloj):
            por_sim[f['simbolo']].append(f)
        patas = _patas_idx(con, cuenta)
        rescate = _rescate_patas(con, cuenta)
        disparo = disparo_espejo(ent, cuenta)
        for simbolo, fl in por_sim.items():
            filas, pos = construir_filas(fl, _planos(con, cuenta, simbolo, inc, reloj), ahora, inc, rescate, reloj)
            for f in filas:
                f.real = real
                f.sim = es_sim
            if abs(pos) > EPS:
                abiertas[cuenta] = abiertas.get(cuenta, 0) + pos
            sim = info_simbolo(simbolo, R)
            if sim is None:
                avisos.append(f"[AVISO] Símbolo sin regla {simbolo} en {cuenta}.")
            for f in filas:
                if not f.completa:
                    f.entrada = f.entradas[0]['precio']
                    f.t_apertura_utc = _ts_exch(f.entradas[0])
                    f.clave = f"{cuenta}|{f.entradas[0]['fill_id']}"
                    abiertas_filas.append(f)
                    continue
                f.entrada = f.entradas[0]['precio']
                f.t_apertura_utc = _ts_exch(f.entradas[0])
                f.t_cierre_utc = (max(_ts_exch(s) for s in f.salidas) if f.salidas else
                                  fuentes.fmt_ts(fuentes.madrid_a_utc(parse_ts(f.t_cierre_forzado))))
                f.sesion = sesion_cme(f.t_apertura_utc)
                f.clave = f"{cuenta}|{f.entradas[0]['fill_id']}"
                f.duracion = int(round((parse_ts(f.t_cierre_utc) - parse_ts(f.t_apertura_utc)).total_seconds()))
                _asignar_patas(f, patas, sim)
                f.simbolo_ok = f.simbolo_ok and sim is not None
                cpl = comision_por_lado(ficha.get('firma'), simbolo, R)
                f.comision_ok = cpl is not None
                f.comision = round((cpl or 0.0) * 2 * f.contratos, 2)
                f.neto = round(f.bruto - f.comision, 2)
                guardado = foto['filas'].get(f.clave)
                if guardado is not None:
                    for campo in CAMPOS_FOTO:
                        setattr(f, campo, guardado[campo])
                else:
                    _enriquecer_log(f, con, sim, R, reloj, disparo)
                    if captura is not None:
                        captura[f.clave] = {campo: getattr(f, campo) for campo in CAMPOS_FOTO}
                if f.incompleta:
                    avisos.append(f"[AVISO] {cuenta} {simbolo}: operación incompleta, faltan {f.faltan:g} "
                                  f"contratos de salida (ATAS la dio por plana a las {f.t_cierre_forzado}).")
                todas.append(f)

    _asignar_ids(con, todas, R)
    # Cerradas y abiertas se emparejan juntas: el fill de salida de la líder y el de su copia no
    # tienen por qué llegar en el mismo ciclo, y una seguidora huérfana contaría como decisión
    # propia en los cortafuegos (ventanas y avisos duplicados).
    emparejar_copias(todas + abiertas_filas, pares_copia, R)
    por_cuenta, abiertas_cuenta = defaultdict(list), defaultdict(list)
    for f in todas:
        por_cuenta[f.cuenta].append(f)
    for f in abiertas_filas:
        abiertas_cuenta[f.cuenta].append(f)
    for cuenta in cuentas_en_juego(ent, con):       # spec §10.3: distancia al threshold en la apertura de cada fila de una prueba
        if ent.es_prueba(cuenta):
            _distancias_apertura(ent, con, cuenta, por_cuenta.get(cuenta, []), abiertas_cuenta.get(cuenta, []), reloj)
    for f in todas:         # spec §9.10 (07/10/2026): con el SL de la líder ya heredado, cada fila con SU tope
        topar_sl(f, ent.es_prueba(f.cuenta), R)
    aplicar_flotantes_manuales(con, todas)
    for f in todas:     # spec §4.2 (01/10/2026): con el MAE/MFE manual de cada uno si existe; vacía si falta alguno
        f.max_amplitud = round(abs(f.mae) + abs(f.mfe), 2) if f.mae is not None and f.mfe is not None else None

    bal = []
    for cuenta in cuentas_en_juego(ent, con):
        fs = sorted(por_cuenta.get(cuenta, []), key=lambda x: (x.t_cierre_utc, x.t_apertura_utc, x.clave))
        res = _metricas_cuenta(ent, con, cuenta, fs, sesion_actual, ahora_utc, manual_thr, avisos, reloj,
                               abiertas_cuenta.get(cuenta, []))
        if cuenta not in queme and not ent.es_sim(cuenta):
            bal.append(res)
    evaluacion = cortafuegos.anotar(todas, abiertas_filas, R, foto['evaluar'])
    dia = mv5.columnas_dia(todas, abiertas_filas)   # spec §16.7: nº en el día (con las abiertas), acumulado y ticks perdidos
    for f in todas:
        f.n_dia, f.acum_dia, f.perdidos_dia = dia.get((f.lider or f).clave, (None, None, None))
    return ResultadoMotor(filas=todas, bal=bal, abiertas=abiertas, avisos=avisos,
                          filas_abiertas=abiertas_filas, cortafuegos=evaluacion, reloj=reloj)


def _t_cierre_c(f):
    """Cierre en hora corregida: el del log si está (se compara con los 'Bal:' del log, también corregidos, en el
    balance y el DESCUADRE) o el del exchange."""
    return f.t_sal_c or fuentes.fmt_ts(fuentes.utc_a_madrid(f.t_cierre_utc))


def _t_ap_c(f):
    """Apertura en hora corregida (Madrid, exchange): la del primer fill de entrada."""
    return _t_fill(f.entradas[0])


def _t_ci_c(f):
    """Cierre en hora corregida: el último fill de salida (o el 'vol 0' con el que ATAS la dio por plana)."""
    ts = [_t_fill(s) for s in f.salidas]
    if f.faltan > EPS and f.t_cierre_forzado:
        ts.append(f.t_cierre_forzado)
    return max(ts)


def _posicion_cuenta(filas):
    """Posición abierta de la cuenta, {raíz: contratos sin signo}, a lo largo del tiempo, desde sus filas cerradas y
    abiertas (horas corregidas). Devuelve en(t): lo que había tras todo lo ocurrido hasta t, incluido. Da 'queda plana'
    (todos sus símbolos a 0, spec §8.4 y §9.5–§9.7) y los NQ-equivalentes de las pruebas (spec §9.3)."""
    ev = []
    for f in filas:
        r = raiz_simbolo(f.simbolo)
        ev += [(_t_fill(e), r, e['qty']) for e in f.entradas]
        ev += [(_t_fill(s), r, -s['qty']) for s in f.salidas]
        if f.faltan > EPS and f.t_cierre_forzado:
            ev.append((f.t_cierre_forzado, r, -f.faltan))
    ev.sort(key=lambda e: e[0])
    tiempos, estados, pos = [], [], defaultdict(float)
    for t, r, q in ev:
        pos[r] += q
        if tiempos and tiempos[-1] == t:
            estados[-1] = dict(pos)
        else:
            tiempos.append(t)
            estados.append(dict(pos))

    def en(t):
        i = bisect.bisect_right(tiempos, t)
        return estados[i - 1] if i else {}
    return en


def _plana(posicion):
    return all(abs(q) < EPS for q in posicion.values())


def _cierres(fs):
    """[(hora corregida, [filas que cierran a esa hora])] en orden; dentro de cada grupo, el orden de `fs`."""
    g = defaultdict(list)
    for f in fs:
        g[_t_ci_c(f)].append(f)
    return sorted(g.items(), key=lambda x: x[0])


def _reglas_real(todas, fs, dias50, alta, posicion, R):
    """Contratos permitidos y parada del día de una cuenta real (spec §8.2, §8.4). Recorre aperturas y cierres en orden.
    En cada apertura: margen levantado = contador ≥ 20 y margen ≤ 141, salvo que el día ya tenga su parada. En cada
    vuelta a plano con el neto del día ≥ +10 t, si alguna de las que cierran se abrió con el margen levantado, la última
    lleva la parada y el resto de ese día vuelve el bloqueo por margen. Devuelve {sesión: hora de la parada}."""
    umbral = round(usd_de_ticks(R['dia']['dia_valido_ticks'], R), 2)
    ev = [(_t_ap_c(f), 1, f) for f in todas] + [(t, 0, g) for t, g in _cierres(fs)]
    ev.sort(key=lambda e: (e[0], e[1]))
    neto, parada = defaultdict(float), {}
    for t, tipo, x in ev:
        if tipo == 1:
            d = sesion_de(x)
            nuevo = regla_nueva(d, R)               # las reglas de §10.1 solo desde plan4.desde
            contador = (d - base_contador(dias50, d, alta)).days
            x.levantado = d not in parada and es_levantado(contador, x.margen_apertura, R, nuevo)
            x.permitidos = limite_real(x.simbolo, x.margen_apertura, x.levantado, R, nuevo)
            continue
        for f in x:
            neto[f.sesion] += f.neto
        d = x[-1].sesion
        if d not in parada and any(f.levantado for f in x) and round(neto[d], 2) >= umbral and _plana(posicion(t)):
            parada[d] = t
            x[-1].etiquetas_cuenta.append(R['apex_pa']['margen_levantado']['parada'])
    return parada


def _plan_prueba(ficha, semilla, R):
    """Parámetros de una prueba (spec §9.5, §9.6): reglas.yaml por firma y tamaño + semillas.yaml."""
    P = R['pruebas']
    firma, tam = ficha.get('firma'), ficha.get('tamano')
    return {'objetivo': objetivo_prueba(firma, tam, R), 'fraccion': P['consistencia'].get(firma),
            'maximo_t': P['maximo_diario']['ticks'].get(firma),
            'balance_inicial': float(semilla.get('balance_inicial') or tam or 0),
            'desde': date.fromisoformat(str(semilla['inicio_prueba'])) if semilla.get('inicio_prueba') else None,
            'mejor_previo': semilla.get('mejor_dia_previo')}


def _mejor_dia(plan, netos_dia, sesion, neto_hoy=None):
    """Mejor neto diario ($) desde que empezó la prueba (spec §9.5): la semilla, las sesiones anteriores a `sesion` y
    el neto de `sesion` hasta ahora (neto_hoy). None si no hay ninguno."""
    c = [plan['mejor_previo']] if plan['mejor_previo'] is not None else []
    c += [v for d, v in netos_dia.items() if d < sesion and (plan['desde'] is None or d >= plan['desde'])]
    if neto_hoy is not None:
        c.append(neto_hoy)
    return round(max(c), 2) if c else None


def _reglas_prueba(fs, plan, netos_dia, posicion, balance_inicio, R):
    """Objetivo, máximo diario y pérdida diaria de una prueba (spec §9.5–§9.7), en cada vuelta a plano de la cuenta; la
    etiqueta la lleva la última operación que cierra. Objetivo y pérdida marcan su primera vez (después, operar es
    infracción); el máximo diario, cada vez que queda plana por encima. Devuelve la hora del objetivo alcanzado o None."""
    P = R['pruebas']
    Ob, Pd = P['objetivo_alcanzado'], P['perdida_diaria']
    perdida_usd = round(usd_de_ticks(Pd['ticks'], R), 2)
    alcanzado, perdida, neto = None, {}, defaultdict(float)
    for t, grupo in _cierres(fs):
        for f in grupo:
            neto[f.sesion] += f.neto
        d, ult = grupo[-1].sesion, grupo[-1]
        if not _plana(posicion(t)):
            continue
        n, b = round(neto[d], 2), ult.balance_tras
        if b is not None and alcanzado is None and plan['objetivo'] is not None:
            obj = objetivo_efectivo(plan['objetivo'], plan['balance_inicial'], _mejor_dia(plan, netos_dia, d, n),
                                    plan['fraccion'])
            if round(b, 2) >= round(obj, 2):
                alcanzado = t
                ult.etiquetas_cuenta.append(Ob['preventivo'])
        if b is not None and plan['maximo_t'] is not None:
            nivel = round(balance_inicio(d) + usd_de_ticks(plan['maximo_t'], R), 2)
            if round(b, 2) > nivel:
                ult.etiquetas_cuenta.append(P['maximo_diario']['etiqueta'].format(nivel=fmt_es(nivel)))
        if d not in perdida and n <= perdida_usd:
            perdida[d] = t
            ult.etiquetas_cuenta.append(Pd['preventivo'].format(ticks=_con_signo(Pd['ticks'])))
    for f in fs:
        t = _t_ap_c(f)
        if alcanzado is not None and t > alcanzado:
            f.infracciones_cuenta.append((Ob['infraccion'], Ob['detalle'].format(hora=alcanzado[11:19])))
        if f.sesion in perdida and t > perdida[f.sesion] and not mv5.es_v5(f.sesion, R):    # V5: ya no es infracción
            f.infracciones_cuenta.append((Pd['infraccion'], Pd['detalle'].format(hora=perdida[f.sesion][11:19])))
    return alcanzado


def _dias_100(ficha, semilla, netos_dia, retiros_c, R):
    """Días +100 (spec §8.3): sesiones con el neto ≥ el umbral de su tamaño posteriores a la sesión del último retiro
    o, sin retiros, todas las de la V4 más las de la semilla. None si el tamaño no tiene umbral."""
    tam = ficha.get('tamano')
    t = R['apex_pa']['dias_100_ticks'].get(int(tam)) if tam is not None else None
    if t is None:
        return None
    validos = dias_validos(netos_dia, t, R)
    if retiros_c:
        desde = sesion_cme(fuentes.fmt_ts(fuentes.madrid_a_utc(parse_ts(max(retiros_c)))))
        return sum(1 for d in validos if d > desde)
    return int(semilla.get('dias_100_previos') or 0) + len(validos)


def _texto_prueba(plan, alcanzado, balance, netos_dia, sesion_actual, R):
    """Columna 'Prueba' del BAL (spec §9.8)."""
    V = R['pruebas']['vista']
    if plan['objetivo'] is None:
        return V['sin_regla']
    if alcanzado is not None:
        return V['alcanzado']
    if balance is None:
        return ''
    hoy = round(netos_dia[sesion_actual], 2) if sesion_actual in netos_dia else None
    mejor = _mejor_dia(plan, netos_dia, sesion_actual, hoy)
    obj = objetivo_efectivo(plan['objetivo'], plan['balance_inicial'], mejor, plan['fraccion'])
    dec = V['decimales_ticks']
    faltan = fmt_es(max(0.0, ticks_nq(obj - balance, R)), dec)
    if plan['fraccion'] is None:
        return V['objetivo'].format(objetivo=fmt_es(obj), faltan=faltan)
    return V['consistencia'].format(objetivo=fmt_es(obj), faltan=faltan, mejor=fmt_es(ticks_nq(mejor or 0.0, R), dec),
                                    maximo=fmt_es(plan['maximo_t'] or 0))


class LineaBalance:
    """Balance de una cuenta hacia delante desde el `Bal:` plano del arranque (spec §7.1): el último plano en cada instante
    (las filas cerradas y los movimientos, a hora corregida). `base` es None si aún no hay `Bal:` plano. Lo usan el BAL y,
    en las pruebas, la distancia al threshold en la apertura (spec §10.3)."""

    def __init__(self, ent, con, cuenta, fs, reloj):
        mcuentas.asegurar_base(ent, con, cuenta)
        self.base = con.execute('SELECT bal_base, bal_base_t FROM cuentas_estado WHERE cuenta=?', (cuenta,)).fetchone()
        # los retiros se anotan con el reloj del PC (cli_v4.py): a hora corregida, como aperturas, cierres y 'Bal:'
        movs = [dict(r, t_c=reloj.a_exch(r['t'])) for r in con.execute(
            'SELECT t, importe, tipo FROM movimientos WHERE cuenta=?', (cuenta,))]
        self.retiros = sorted(m['t_c'] for m in movs if m['tipo'] == 'retiro')
        eventos = sorted([(_t_cierre_c(f), f.neto) for f in fs] + [(m['t_c'], m['importe']) for m in movs])
        self.tiempos = [e[0] for e in eventos]
        self.acum, self.total = [], 0.0
        for _, v in eventos:
            self.total += v
            self.acum.append(self.total)
        if self.base:
            self.base_c = reloj.a_exch(self.base['bal_base_t'])         # guardada cruda (hora del log)
            self.c_base = self.c_hasta(self.base_c)

    def c_hasta(self, t):
        i = bisect.bisect_right(self.tiempos, t)
        return self.acum[i - 1] if i else 0.0

    def balance_en(self, t):
        return round(self.base['bal_base'] + self.c_hasta(t) - self.c_base, 2)


def _regla_threshold(ent, cuenta, ficha, R):
    """{'drawdown', 'tope'} del threshold de la cuenta (spec §6). En las evaluaciones Apex el tope sube hasta el objetivo."""
    regla = mcuentas.regla_firma(ficha.get('firma'), ficha.get('tamano'), R)
    objetivo = objetivo_prueba(ficha.get('firma'), ficha.get('tamano'), R)
    if (regla is not None and ent.es_prueba(cuenta) and objetivo is not None
            and ficha.get('firma') in R['pruebas']['threshold_tope_objetivo']):
        regla = dict(regla, tope=objetivo)                 # §6: el de la evaluación sube hasta el objetivo
    return regla


def _distancias_apertura(ent, con, cuenta, fs, abiertas, reloj):
    """Distancia al threshold (ticks de NQ) en la apertura de cada fila de una prueba, en `dist_ap` (spec §10.3). Balance = el
    último plano en ese instante (los cierres hasta entonces). Threshold = la fórmula de §6 con el pico de equity hasta ese
    instante (balance de los cierres y `Bal:` del log), sin el manual de la columna del BAL (no se sabe desde cuándo manda). Sin
    `Bal:` base o sin regla de threshold, queda None (distancia desconocida)."""
    R = ent.reglas
    ficha = ent.ficha(cuenta)
    lb = LineaBalance(ent, con, cuenta, fs, reloj)
    regla = _regla_threshold(ent, cuenta, ficha, R)
    if not lb.base or regla is None:
        return
    inicial = lb.base['bal_base']
    ancla = float(ficha['ancla_inicial']) if ficha.get('ancla_inicial') is not None else round(inicial - regla['drawdown'], 2)
    puntos = [(_t_cierre_c(f), lb.balance_en(_t_cierre_c(f))) for f in fs]
    puntos += [(r['t_c'], r['bal']) for r in reloj.filas(
        con, 'SELECT bal, t_log, fichero, pos FROM equity WHERE cuenta=? AND bal>?', (cuenta, inicial))]
    puntos.sort()
    tiempos, picos, pico = [], [], inicial
    for t, v in puntos:
        pico = max(pico, v)
        tiempos.append(t)
        picos.append(pico)
    for f in list(fs) + list(abiertas):
        t = _t_ap_c(f)
        i = bisect.bisect_right(tiempos, t)
        bal = lb.balance_en(t)
        f.dist_ap = round(ticks_nq(bal - threshold(None, ancla, max(picos[i - 1] if i else inicial, bal), regla), R), 2)


def exceso_distancia(posicion, distancia, R):
    """CONTRATOS > PERMITIDOS de una prueba de 25K por la distancia al threshold (spec §10.3): detalle o None. `posicion`:
    {raíz: contratos} de la cuenta justo tras la entrada. Con distancia >= `distancia_min_ticks` (o desconocida), hasta
    `max_total` contratos entre los símbolos de la tabla (`lejos`); con menos, solo `cerca` (0 NQ, 1 MNQ)."""
    D = R['pruebas']['contratos']['por_distancia']
    lim = D['lejos' if distancia is None or distancia >= D['distancia_min_ticks'] else 'cerca']
    abiertos = {r: abs(q) for r, q in posicion.items() if r in lim and abs(q) > EPS}
    if sum(abiertos.values()) <= D['max_total'] and all(q <= lim[r] + EPS for r, q in abiertos.items()):
        return None
    V = R['vista_bal']
    texto = V['permitidos_separador'].join(V['permitidos'].format(n=fmt_es(q), simbolo=r) for r, q in abiertos.items())
    return D['detalle'].format(posicion=texto, distancia=D['sin_dato'] if distancia is None else fmt_es(distancia, 1))


def _metricas_cuenta(ent, con, cuenta, fs, sesion_actual, ahora_utc, manual_thr, avisos, reloj, abiertas=()):
    R = ent.reglas
    O = R['observaciones']
    ficha = ent.ficha(cuenta)
    alta = date.fromisoformat(str(ficha.get('alta'))) if ficha.get('alta') else ent.inicio_sesion
    alta = max(alta, ent.inicio_sesion)
    real, prueba = ent.es_real(cuenta), ent.es_prueba(cuenta)
    semilla = ent.semilla(cuenta)
    todas = list(fs) + list(abiertas)

    # --- neto de cada sesión y días +50 (spec §7.1, §8.3): los de la V4 más las semillas anteriores a su inicio ---
    netos_dia = defaultdict(float)
    for f in fs:
        netos_dia[f.sesion] += f.neto
    dias_v4 = dias_validos(netos_dia, R['dia']['dia_valido_ticks'], R)
    semillas50 = {date.fromisoformat(str(d)) for d in semilla.get('ultimos_dias_50') or []}
    dias50 = sorted(dias_v4 | {d for d in semillas50 if d < ent.inicio_sesion})

    # --- margen, contratos permitidos, racha, resultado anterior, observaciones por fila ---
    for f in fs:
        f.consumo = consumo_margen(f.ticks, f.mae if (f.telemetria or f.mae_manual) else None, R)
    suma_maxima = None
    if real:
        def suma_maxima(d, m):          # §8.4: día +50 con el margen levantado al empezar la sesión
            return d in dias_v4 and es_levantado((d - base_contador(dias50, d, alta)).days, m, R, regla_nueva(d, R))
    margen_en, margen_actual, consumo_sesion = margen_timeline(fs, alta, sesion_actual, R, suma_maxima)
    posicion = _posicion_cuenta(todas)
    for f in todas:                     # también las abiertas: el aviso en vivo de CONTRATOS > PERMITIDOS
        f.margen_apertura = round(margen_en(f.t_apertura_utc, incluir_igual=True), 2)
    parada = {}
    if real:                            # §8.2 y §8.4
        parada = _reglas_real(todas, fs, dias50, alta, posicion, R)
    elif prueba:                        # §9.3 y §10.3: el margen no limita
        por_distancia = ficha.get('tamano') in R['pruebas']['contratos']['por_distancia']['tamanos']
        for f in todas:
            pos = posicion(max(_t_fill(e) for e in f.entradas))      # la de la cuenta justo tras la entrada
            if por_distancia and regla_nueva(sesion_de(f), R):     # 25K desde plan4.desde: según la distancia al threshold
                f.dist_regla = True
                f.exceso_dist = exceso_distancia(pos, f.dist_ap, R)
            else:                       # otras (y las 25K anteriores a plan4.desde): máximo en NQ-equivalentes de la cuenta
                f.neq = nq_equivalentes(pos, R)
                f.neq_max = R['pruebas']['contratos']['max_nq_eq']
    else:                               # sim: §5
        for f in todas:
            f.permitidos = contratos_permitidos(f.margen_apertura, R, regla_nueva(sesion_de(f), R))
    racha, prev = 0, None
    for f in fs:
        f.margen_tras = round(margen_en(f.t_cierre_utc, incluir_igual=True), 2)
        racha = (siguiente_racha_v5(racha, f.ticks, R) if mv5.es_v5(f.sesion, R) else siguiente_racha(racha, f.neto, R))
        f.racha, f.res_anterior, prev = racha, prev, f.ticks
        if (f.telemetria or f.es_seguidora) and f.simbolo_ok:
            f.ratio = ratio_br(f.ticks, f.sl_max, f.tp_final, f.tp_llenado, f.margen_apertura, R)
        f.obs = _observaciones(f, R)

    # --- balance hacia delante desde el Bal: real del arranque ---
    lb = LineaBalance(ent, con, cuenta, fs, reloj)
    base, retiros, s, balance_en = lb.base, lb.retiros, lb.total, lb.balance_en
    for f in fs:
        t_ap = fuentes.fmt_ts(fuentes.utc_a_madrid(f.t_apertura_utc))
        f.nombre = ent.nombre_con_retiros(cuenta, bisect.bisect_left(retiros, t_ap))

    obs_bal = [f"{O['bal']['sync']} {fuentes.utc_a_madrid(ahora_utc):%H:%M:%S}"]
    balance = umbral = balance_inicio = None
    if base:
        base_c, c_base = lb.base_c, lb.c_base

        def balance_inicio(d):                               # balance al empezar la sesión CME d (spec §9.6)
            return balance_en(fuentes.fmt_ts(fuentes.utc_a_madrid(inicio_sesion_utc(d))))

        for f in fs:
            f.balance_tras = balance_en(_t_cierre_c(f))
        balance = round(base['bal_base'] + s - c_base, 2)
        bp = almacen.bal_plano(con, cuenta, reloj=reloj)
        if bp is not None and bp['t_c'] >= base_c:
            # La hora del log corregida y la de cierre del exchange (HistoryMyTrade, al segundo) difieren unas
            # décimas: un Bal: plano puede incluir ya una operación que "cierra" después. Se compara también con el
            # balance a t_plano + tolerancia y vale el menor descuadre (fsern…250, 24/09 18:00:32).
            t_tol = fuentes.fmt_ts(parse_ts(bp['t_c']) + timedelta(seconds=R['incompleta']['tolerancia_s']))
            dif = min((bp['bal'] - balance_en(t) for t in (bp['t_c'], t_tol)), key=abs)
            if abs(dif) > R['balance']['descuadre_max']:
                obs_bal.append(f"{O['bal']['descuadre']} {dif:+.2f} (log {bp['bal']:.2f})")
        # Pico de equity: el 'Bal:' del log ya es equity (balance + flotante). Sumarle el PnL abierto
        # lo duplica, y tras un cierre ATAS emite líneas con el PnL abierto anterior.
        r = con.execute('SELECT MAX(bal) m FROM equity WHERE cuenta=?', (cuenta,)).fetchone()
        pico = max(balance, r['m']) if r['m'] is not None else balance
        regla = _regla_threshold(ent, cuenta, ficha, R)
        if regla is None:
            obs_bal.append(O['bal']['sin_regla'])
        else:
            umbral = _actualizar_threshold(ent, con, cuenta, ficha, regla, pico, base['bal_base'],
                                           (manual_thr or {}).get(cuenta))
            if ficha.get('ancla_estimada'):
                obs_bal.append(O['bal']['ancla_estimada'])
    else:
        obs_bal.append(O['bal']['sin_balance'])
        avisos.append(f"[AVISO] {cuenta}: sin Bal: plano en el log; no hay balance base.")
    if ficha.get('sin_regla') and O['bal']['sin_regla'] not in obs_bal:
        obs_bal.append(O['bal']['sin_regla'])

    # --- reglas de la cuenta al quedar plana (spec §8.4, §9.5–§9.7) -> Observaciones demonio ---
    alcanzado, plan = None, None
    if prueba:
        plan = _plan_prueba(ficha, semilla, R)
        alcanzado = _reglas_prueba(fs, plan, netos_dia, posicion, balance_inicio, R)
        if alcanzado is not None:       # spec §16.5: también la posición ABIERTA tras el objetivo, para avisar al abrir
            Ob = R['pruebas']['objetivo_alcanzado']
            for f in abiertas:
                if _t_ap_c(f) > alcanzado:
                    f.infracciones_cuenta.append((Ob['infraccion'], Ob['detalle'].format(hora=alcanzado[11:19])))
    for f in fs:
        extra = [tipo for tipo, _ in f.infracciones_cuenta] + f.etiquetas_cuenta
        if extra:
            f.obs = O['separador'].join(([f.obs] if f.obs else []) + extra)

    # --- columnas del plan (spec §7.1, §8.5, §9.8) ---
    plan_bal = {'dias_50_30d': None, 'inactividad': None, 'dias_100': None, 'prueba': None}
    if real:
        I = R['apex_pa']['inactividad']
        base_d = base_contador(dias50, sesion_actual, alta, incluir=True)
        n = (sesion_actual - base_d).days
        lev = sesion_actual not in parada and es_levantado(n, margen_actual, R)
        desde30 = sesion_actual - timedelta(days=I['ventana_dias'] - 1)
        plan_bal.update(
            dias_50_30d=sum(1 for d in dias50 if desde30 <= d <= sesion_actual),
            inactividad=I['formato'].format(n=n, limite=f"{base_d + timedelta(days=I['limite_dias']):%d/%m}",
                                            estado=estado_inactividad(n, lev, R)),
            dias_100=_dias_100(ficha, semilla, netos_dia, retiros, R))
        propia = (R.get('inactividad_firma') or {}).get(ficha.get('firma'))
        if propia:                      # §10.1 (11/11/2026): la firma tiene su norma (E2T: 5 días hábiles sin operar)
            plan_bal['inactividad'] = inactividad_firma(propia, {sesion_de(f) for f in todas}, alta, sesion_actual)
        permitidos = texto_permitidos({r: limite_real(r, margen_actual, lev, R) for r in R['apex_pa']['contratos']}, R)
    elif prueba:
        pc = R['pruebas']['contratos']
        if por_distancia:               # §10.3: según la distancia actual (la de la columna 'Distancia al threshold')
            dist = (round(ticks_nq(balance - umbral, R), 2) if (balance is not None and umbral is not None) else None)
            pd = pc['por_distancia']
            permitidos = texto_permitidos(pd['lejos' if dist is None or dist >= pd['distancia_min_ticks'] else 'cerca'], R)
        else:
            permitidos = texto_permitidos({r: round(pc['max_nq_eq'] / p, 6) for r, p in pc['peso'].items()}, R)
        plan_bal['prueba'] = _texto_prueba(plan, alcanzado, balance, netos_dia, sesion_actual, R)
    else:
        permitidos = contratos_permitidos(margen_actual, R)

    # --- datos del día (sesión actual) ---
    hoy = [f for f in fs if f.sesion == sesion_actual]
    validos = dias50[-2:]
    ratio_dia = None
    if hoy:
        sls = [(f.sl_ini if (f.sl_ini is not None and f.sl_ini > 0) else f.margen_apertura) for f in hoy]
        media_sl = sum(sls) / len(sls)
        if media_sl > 0:
            ratio_dia = (sum(f.ticks for f in hoy) - sum(abs(f.mae or 0.0) for f in hoy)) / media_sl
    thr_manual = (manual_thr or {}).get(cuenta)
    return {
        'cuenta': cuenta,
        'nombre': ent.nombre_visible(cuenta),
        'balance': balance,
        'threshold': umbral,
        'threshold_manual': thr_manual,
        # en ticks de NQ (30/09/2026, spec §7.1)
        'distancia': round(ticks_nq(balance - umbral, R), 2) if (balance is not None and umbral is not None) else None,
        'margen': round(margen_actual, 2),
        'permitidos': permitidos,
        **plan_bal,
        'n_ops': len(hoy),
        'n_ganadas': (sum(1 for f in hoy if mv5.categoria(f.ticks, R) == 'G') if mv5.es_v5(sesion_actual, R)
                      else sum(1 for f in hoy if f.neto >= R['dia']['ganada_min'])),
        'resultado_dia': round(sum(f.neto for f in hoy), 2),
        'ratio_dia': ratio_dia,
        'sesion': sesion_actual,
        'ultimos_validos': validos,
        'obs': R['observaciones']['separador'].join(obs_bal),
        'consumo_sesion': consumo_sesion,
    }


def _actualizar_threshold(ent, con, cuenta, ficha, regla, pico, bal_base, manual):
    r = con.execute('SELECT valor, origen FROM anclas WHERE cuenta=?', (cuenta,)).fetchone()
    if r:
        ancla = r['valor']
    elif ficha.get('ancla_inicial') is not None:
        ancla = float(ficha['ancla_inicial'])
    else:
        ancla = round(bal_base - regla['drawdown'], 2)      # ANCLA ESTIMADA: balance − drawdown
    calculado = threshold(None, ancla, pico, regla)
    thr = threshold(manual, ancla, pico, regla)
    manda_manual = manual is not None and manual >= calculado     # el manual solo manda si es mayor
    origen = 'manual' if manda_manual else 'calculado'
    if r is None or r['valor'] != thr or r['origen'] != origen:     # solo si cambia: el cálculo corre cada 2 s (11/11/2026)
        con.execute('INSERT OR REPLACE INTO anclas VALUES (?, ?, ?, ?)',
                    (cuenta, thr, almacen.ahora_madrid(), origen))
        con.commit()
    if manda_manual and ficha.get('ancla_estimada'):
        ficha['ancla_estimada'] = False
        ent.guardar_cuentas()
    return thr


def _observaciones(f, R):
    O = R['observaciones']
    es5 = mv5.es_v5(f.sesion, R)       # V5 (spec §16): SIN SL y SL CANCELADO dejan de existir
    partes = []
    if not f.telemetria:
        partes.append(O['sin_telemetria'])
    elif f.sin_sl and f.simbolo_ok and not es5:
        partes.append(O['sin_sl'])
    if f.incompleta:
        partes.append(f"{O['incompleta']} ({f.faltan:g})")
    if f.rescatada:
        partes.append(O['salida_rescatada'])
    if f.sl_cancelado and not es5:
        partes.append(O['sl_cancelado'])
    if cortafuegos.detalle_exceso(f, R):
        partes.append(O['contratos_excedidos'])
    if not f.simbolo_ok:
        partes.append(O['simbolo_desconocido'])
    if not f.comision_ok:
        partes.append(O['comision_sin_regla'])
    sl = nota_sl_beneficio(f.sl_hist, R) if f.tope_sl is not None else texto_cambios('SL', f.sl_hist)   # §9.10
    cambios = [x for x in (sl, texto_cambios('TP', f.tp_hist)) if x]
    if cambios:
        partes.append(', '.join(cambios))
    for campo, texto in (('mae', O['mae_manual']), ('mfe', O['mfe_manual'])):
        if getattr(f, f'{campo}_manual'):
            log = getattr(f, f'{campo}_log')
            partes.append(f"{texto} (log {_con_signo(log) if log is not None else O['log_sin_dato']})")
    f.etq_duracion = etiqueta_duracion(f.duracion, f.mae if (f.telemetria or f.mae_manual) else None, f.mfe,
                                       f.sl_ini, f.tp_final, R)       # también la usa la calificación
    partes.append(f.etq_duracion)
    return O['separador'].join(partes)


def _con_signo(x):
    return (f"{x:+g}").replace('-', '−')


def categoria_resultado(ticks, R):
    """Resultado (ticks) por contrato -> 'ganador' (≥ +10 t), 'perdedor' (≤ −10 t) o 'neutro' (entre medias, en sentido
    estricto). Spec §4.2 (01/10/2026); umbrales en reglas.yaml -> calificacion.resultado."""
    U = R['calificacion']['resultado']
    t = round(ticks, 2)
    if t >= U['ganador_min']:
        return 'ganador'
    if t <= U['perdedor_max']:
        return 'perdedor'
    return 'neutro'


def calificar(filas, ev, manual, tardias, R):
    """Calificación de cada operación de SQLite, líder y seguidoras (spec §4.2, 01/10/2026): proceso × resultado.
    Proceso incorrecto si la operación lleva una infracción de calificacion.proceso_incorrecto, su estrategia final es
    `Sin estrategia`, se marcó `ESTRATEGIA ELEGIDA CON LA OPERACIÓN ABIERTA` o su duración es `LARGA AGUANTANDO PÉRDIDA`.
    ev: evaluación del cortafuegos (res.cortafuegos); manual: {op_id: columnas manuales} ya leídas del sheet y con la
    asignación de la preoperativa hecha; tardias: IDs de decisión con la estrategia elegida con la operación abierta.
    Cada operación, con sus etiquetas: las de entrada, su cuenta y su duración son suyas, y REENTRADA/SOBREOPERACIÓN solo
    las lleva la fila de la decisión (§4.5). Estrategia y tardía son de la decisión: la seguidora usa las de su líder."""
    C = R['calificacion']
    P = C['proceso_incorrecto']
    cuentan = set(P['infracciones']) | set(P['etiquetas'])
    sin_estrategia = {str(e).strip().lower() for e in P['estrategias']}
    larga = {f"{R['observaciones']['larga']['etiqueta']} {c}" for c in P['larga']}      # como etiqueta_duracion
    for f in filas:
        decision = f.decision_id
        if mv5.es_v5(f.sesion, R):
            _calificar_v5(f, ev, manual, tardias, R)
            continue
        etiquetas = [tipo for tipo, _ in cortafuegos.infracciones_de_entrada(f, R)]
        if not f.es_seguidora:
            etiquetas += [i['tipo'] for i in ev.get(f.clave, {}).get('infracciones', [])]
        if decision in tardias:
            etiquetas.append(R['observaciones']['estrategia_tardia'])
        estrategia = (manual.get(decision) or {}).get('estrategia') or R['estrategia_por_defecto']
        f.proceso_ok = not (any(e in cuentan for e in etiquetas) or str(estrategia).strip().lower() in sin_estrategia
                            or f.etq_duracion in larga)
        f.categoria_resultado = categoria_resultado(f.ticks, R)
        cu = C['cuadrantes']
        f.calificacion = (cu['neutro'] if f.categoria_resultado == 'neutro'
                          else cu['correcto' if f.proceso_ok else 'incorrecto'][f.categoria_resultado])


def _calificar_v5(f, ev, manual, tardias, R):
    """Calificación de una operación de la V5 (spec §16.6): proceso correcto = estrategia elegida antes de entrar (ni la de
    defecto ni elegida con la operación abierta) y ninguna infracción, roja o naranja (abrir tras un aviso de parar es un
    CONSEJO IGNORADO). La duración ya no cuenta. Resultado con la banda de la V5 (±20 t)."""
    C = R['calificacion']
    decision = f.decision_id
    infracciones = [tipo for tipo, _ in cortafuegos.infracciones_de_entrada(f, R)]
    lider = f.lider or f
    infracciones += [i['tipo'] for i in ev.get(lider.clave, {}).get('infracciones', [])]
    estrategia = (manual.get(decision) or {}).get('estrategia') or R['estrategia_por_defecto']
    sin_estrategia = str(estrategia).strip().lower() == str(R['estrategia_por_defecto']).strip().lower()
    f.proceso_ok = not (infracciones or sin_estrategia or decision in tardias)
    f.categoria_resultado = {'G': 'ganador', 'P': 'perdedor', 'N': 'neutro'}[mv5.categoria(f.ticks, R)]
    cu = C['cuadrantes']
    f.calificacion = (cu['neutro'] if f.categoria_resultado == 'neutro'
                      else cu['correcto' if f.proceso_ok else 'incorrecto'][f.categoria_resultado])


def espejo_manual(texto, R):
    """Corrección manual del ESPEJO en la columna Observaciones del operador (spec §10.5): False si contiene `no espejo`,
    True si contiene `espejo` (como palabra, sin distinguir mayúsculas), None si ninguna. Manda lo manual, y `no espejo`
    antes que `espejo` (la primera contiene a la segunda)."""
    E = R['espejo']
    t = str(texto or '')

    def hay(frase):
        return re.search(r'(?<!\w)' + r'\s+'.join(map(re.escape, frase.split())) + r'(?!\w)', t, re.IGNORECASE)

    if hay(E['negacion']):
        return False
    return True if hay(E['palabra']) else None


def compuerta_sin_si(t_apertura_utc, lecturas, R):
    """COMPUERTA SIN SÍ (spec §10.6): en la apertura de una decisión alguna fila de la compuerta no está en SÍ. Vale la última
    lectura anterior a la apertura; si es de otra sesión CME, la compuerta se vació al empezar la de la apertura y no hay
    ningún SÍ. `lecturas`: [(hora UTC del exchange, valores)] en orden. Sin ninguna lectura anterior (la compuerta aún no
    existía), no se juzga: False."""
    previas = [(t, v) for t, v in lecturas if t <= t_apertura_utc]
    if not previas:
        return False
    t, valores = previas[-1]
    if sesion_cme(t) != sesion_cme(t_apertura_utc):
        return True
    n = len(R['preoperativa']['compuerta']['filas'])
    si = R['preoperativa']['compuerta']['afirmativo']
    return any(v != si for v in (list(valores) + [''] * n)[:n])


def mercado_en(t_apertura_utc, lecturas, R):
    """Estado del mercado vigente en una apertura (spec §12): el valor de la última lectura anterior o igual a ella (hora del
    exchange). Si es de otra sesión CME, la fila se reseteó al empezar la de la apertura; sin lecturas anteriores, el defecto."""
    defecto = R['preoperativa']['mercado']['defecto']
    previas = [(t, v) for t, v in lecturas if t <= t_apertura_utc]
    if not previas or sesion_cme(previas[-1][0]) != sesion_cme(t_apertura_utc):
        return defecto
    return previas[-1][1]


def marcar_analisis(filas, lecturas, reloj, manual, R, mercado=()):
    """Etiquetas de análisis de cada decisión, tras leer del sheet las columnas manuales (spec §10.5 y §10.6): `ESPEJO` y
    `COMPUERTA SIN SÍ` van a Observaciones demonio de la fila de la decisión (no ponen rojo, no suenan, no cambian la
    Calificación). `espejo` y `espejo_origen` se fijan en la líder y en sus seguidoras (la seguidora usa lo de su líder).
    lecturas: filas de `compuerta_lecturas` (hora del reloj del PC, valores); manual: {op_id: columnas manuales}."""
    E = R['espejo']
    lecturas = sorted((reloj.pc_a_utc(t), v) for t, v in lecturas)
    mercado = sorted((reloj.pc_a_utc(t), v) for t, v in mercado)
    for f in filas:
        if f.es_seguidora:
            continue
        f.mercado = mercado_en(f.t_apertura_utc, mercado, R)           # spec §12: solo se guarda, sin etiqueta
        for c in f.copias:
            c.mercado = f.mercado
        if mv5.es_v5(f.sesion, R):          # spec §16: ni ESPEJO ni COMPUERTA SIN SÍ; SIN ESTRATEGIA (rojo) y ratio planeado
            estrategia = (manual.get(f.op_id) or {}).get('estrategia') or R['estrategia_por_defecto']
            f.espejo, f.espejo_origen = False, 'v5'
            f.ratio = mv5.ratio_planeado(estrategia, R)
            for c in f.copias:
                c.espejo, c.espejo_origen, c.ratio = False, 'v5', f.ratio
            if str(estrategia).strip().lower() == str(R['estrategia_por_defecto']).strip().lower():
                f.obs = R['observaciones']['separador'].join(([f.obs] if f.obs else []) + [R['v5']['sin_estrategia']])
                f.cortafuegos = True
            continue
        man = espejo_manual((manual.get(f.op_id) or {}).get('observaciones'), R)
        f.espejo, f.espejo_origen = (f.espejo_auto, 'auto') if man is None else (man, 'manual')
        for c in f.copias:
            c.espejo, c.espejo_origen = f.espejo, f.espejo_origen
        etiquetas = [E['etiqueta']] if f.espejo else []
        if regla_nueva(f.sesion, R) and compuerta_sin_si(f.t_apertura_utc, lecturas, R):
            etiquetas.append(R['preoperativa']['compuerta']['etiqueta'])
        if etiquetas:
            f.obs = R['observaciones']['separador'].join(([f.obs] if f.obs else []) + etiquetas)


def inicio_semana(sesion):
    """Lunes de la semana de una sesión CME (spec §11.1). Las sesiones de sábado y domingo (hasta el domingo a las 17:00 CT
    la sesión en curso sigue siendo la del fin de semana) caen en la semana que acaba de terminar."""
    return sesion - timedelta(days=sesion.weekday())


def fin_semana(lunes, R):
    """Último día de la semana de `lunes` (viernes): `objetivos.semana.dias` días de sesión."""
    return lunes + timedelta(days=R['objetivos']['semana']['dias'] - 1)


def recuento_semanal(filas, manual, tardias, R):
    """Recuento de los objetivos de proceso 1–4 por semana (spec §11.1): {lunes: {n, sin_preop, reentrada, sin_sl, correctas}}.
    Unidad: la decisión (fila de la líder; ni copias ni sim) con sesión CME de la apertura >= `plan4.desde`. Las etiquetas son
    las de Observaciones demonio de la decisión (infracciones del cortafuegos, SIN SL, COMPUERTA SIN SÍ) más
    `ESTRATEGIA ELEGIDA CON LA OPERACIÓN ABIERTA` si está en `tardias`; la estrategia final es la del sheet (`manual`) o la del
    defecto, como en la Calificación. `filas` ya con `calificar` y `marcar_analisis` aplicados."""
    # V5 (spec §16.8, 11/11/2026): tres objetivos y solo las decisiones de la V5 (sesión >= v5.desde). Las semanas de la V4
    # se quedan en la pestaña de la hoja antigua, con sus cinco objetivos.
    sep = R['observaciones']['separador']
    defecto = str(R['estrategia_por_defecto']).strip().lower()
    rojas = set(R['v5']['calificacion']['infracciones']) - {R['v5']['sin_estrategia']}
    desde = mv5.desde(R)
    out = {}
    for f in filas:
        if f.es_seguidora or f.sim or sesion_de(f) < desde:
            continue
        etiquetas = f.obs.split(sep) if f.obs else []
        estrategia = (manual.get(f.op_id) or {}).get('estrategia') or R['estrategia_por_defecto']
        r = out.setdefault(inicio_semana(sesion_de(f)), {'n': 0, 'sin_estrategia': 0, 'infracciones': 0, 'correctas': 0})
        r['n'] += 1
        r['sin_estrategia'] += int(str(estrategia).strip().lower() == defecto)
        r['infracciones'] += sum(1 for e in etiquetas if e in rojas)
        r['correctas'] += int(bool(f.proceso_ok))
    return out


def aplicar_flotantes_manuales(con, filas):
    """MAE/MFE corregidos por el operador en la fila de la decisión: sustituyen al log en la líder y en
    todas sus seguidoras. El valor del log se conserva en mae_log/mfe_log."""
    man = {r['op_id']: (r['mae_manual'], r['mfe_manual']) for r in con.execute(
        'SELECT op_id, mae_manual, mfe_manual FROM manual_ops '
        'WHERE mae_manual IS NOT NULL OR mfe_manual IS NOT NULL')}
    for f in filas:
        f.mae_log, f.mfe_log = f.mae, f.mfe
        mae, mfe = man.get(f.decision_id, (None, None))
        if mae is not None:
            f.mae, f.mae_manual = -abs(mae), True
        if mfe is not None:
            f.mfe, f.mfe_manual = abs(mfe), True


def _asignar_ids(con, filas, R):
    """ID <cuenta_corta>-<YYYYMMDD>-<nnn>, generado una vez y guardado en SQLite."""
    for f in sorted(filas, key=lambda x: (x.t_apertura_utc, x.clave)):
        r = con.execute('SELECT op_id FROM ops_ids WHERE clave=?', (f.clave,)).fetchone()
        if r:
            f.op_id = r['op_id']
            continue
        ses = f.sesion.isoformat()
        n = con.execute('SELECT COALESCE(MAX(n), 0) + 1 AS n FROM ops_ids WHERE cuenta=? AND sesion=?',
                        (f.cuenta, ses)).fetchone()['n']
        f.op_id = f"{cuenta_corta(f.cuenta, R)}-{f.sesion:%Y%m%d}-{n:03d}"
        con.execute('INSERT INTO ops_ids VALUES (?, ?, ?, ?, ?)', (f.clave, f.op_id, f.cuenta, ses, n))
    con.commit()


def guardar_operaciones(con, filas, ent):
    """Foto derivada en SQLite (fuente de verdad del sheet)."""
    con.execute('DELETE FROM operaciones')
    for f in filas:
        datos = {
            'cuenta_visible': f.nombre, 'simbolo': f.simbolo, 'direccion': f.direccion,
            'order_id': f.order_id, 'contratos': f.contratos, 'entrada': f.entrada,
            'max_amplitud': f.max_amplitud, 'tipo_apertura': f.tipo_apertura, 'tipo_cierre': f.tipo_cierre,
            'ticks': f.ticks, 'bruto': f.bruto, 'comision': f.comision, 'neto': f.neto,
            'mae': f.mae, 'mfe': f.mfe, 'mae_log': f.mae_log, 'mfe_log': f.mfe_log,
            'mae_manual': f.mae_manual, 'mfe_manual': f.mfe_manual, 'sl_ini': f.sl_ini, 'sl_max': f.sl_max, 'tp_final': f.tp_final,
            'tp_llenado': f.tp_llenado, 'ratio': f.ratio, 'racha': f.racha, 'res_anterior': f.res_anterior,
            'margen_apertura': f.margen_apertura, 'margen_tras': f.margen_tras,
            'balance_tras': f.balance_tras, 'duracion': f.duracion, 'obs': f.obs,
            't_cierre_utc': f.t_cierre_utc, 'telemetria': f.telemetria,
            'copias': [c.op_id for c in f.copias],
            # spec §4.2 (01/10/2026): también proceso y categoría, para separar los neutros por proceso
            'calificacion': f.calificacion, 'proceso_ok': f.proceso_ok, 'categoria_resultado': f.categoria_resultado,
            # spec §10.5: ESPEJO final y de dónde sale (la seguidora usa lo de su líder)
            'espejo': f.espejo, 'espejo_origen': f.espejo_origen,
            # spec §12: estado del mercado en la apertura (la seguidora, el de su líder)
            'mercado': f.mercado,
            # spec §16: turno de operativa (no se muestra en la hoja) y datos del día de la decisión
            'turno': f.turno, 'n_dia': f.n_dia, 'acum_dia': f.acum_dia, 'perdidos_dia': f.perdidos_dia,
        }
        if f.tope_sl is not None:      # spec §9.10: el SL de la orden, antes del tope (sl_ini y sl_max ya van topados)
            datos.update(sl_ini_orden=f.sl_ini_orden, sl_max_orden=f.sl_max_orden, sl_tope=f.tope_sl)
        con.execute('INSERT OR REPLACE INTO operaciones VALUES (?, ?, ?, ?, ?, ?)',
                    (f.op_id, f.cuenta, (f.lider.op_id or None) if f.lider else None, f.sesion.isoformat(),
                     f.t_apertura_utc, json.dumps(datos, ensure_ascii=False)))
    con.commit()


def canon(res):
    """Resultado del motor en forma comparable (texto): todas las filas con todos sus campos (la líder y las copias, por su
    clave; los fills, por su Id), el BAL y la evaluación del cortafuegos. Para comprobar la foto contra el cálculo completo."""
    from dataclasses import fields

    def fila(f):
        d = {}
        for c in fields(f):
            v = getattr(f, c.name)
            if c.name == 'lider':
                v = v.clave if v is not None else None
            elif c.name == 'copias':
                v = sorted(x.clave for x in v)
            elif c.name in ('entradas', 'salidas'):
                v = [(x.get('fill_id'), x.get('t_utc'), x.get('precio'), x.get('volumen')) for x in v]
            d[c.name] = repr(v)
        return d

    return {'filas': {f.clave: fila(f) for f in res.filas},
            'abiertas': {f.clave: fila(f) for f in res.filas_abiertas},
            'bal': {b['cuenta']: repr(sorted(b.items(), key=lambda kv: kv[0])) for b in res.bal},
            'cortafuegos': {k: repr(v) for k, v in res.cortafuegos.items()},
            'avisos': sorted(res.avisos)}


def diferencias(a, b, maximo=12):
    """Textos con las diferencias entre dos `canon` (vacío si son iguales)."""
    out = []
    for bloque in ('filas', 'abiertas', 'bal', 'cortafuegos'):
        for k in sorted(set(a[bloque]) | set(b[bloque])):
            x, y = a[bloque].get(k), b[bloque].get(k)
            if x == y:
                continue
            if isinstance(x, dict) and isinstance(y, dict):
                campos = [c for c in x if x[c] != y.get(c)]
                out.append(f"{bloque} {k}: {', '.join(f'{c} {x[c]} != {y.get(c)}' for c in campos[:3])}")
            else:
                out.append(f"{bloque} {k}: {str(x)[:120]} != {str(y)[:120]}")
    if a['avisos'] != b['avisos']:
        out.append('avisos distintos')
    return out[:maximo]
