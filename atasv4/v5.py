"""Reglas de la V5 (grill del 11/11/2026, Q1–Q47; spec §16). Rigen para las decisiones cuya sesión CME de apertura es
>= `v5.desde`; lo anterior conserva las reglas de la V4. Todo número y texto sale de reglas.yaml -> v5.

- Banda de resultado: ganadora >= +banda t, perdedora <= −banda t, neutra entre medias (por contrato, fila de la líder).
- «Consejos que no pedí» (voluntarios, por sesión CME, sobre decisiones CERRADAS y en orden de cierre):
    · N ganadoras sin ninguna perdedora antes en el día (las neutras ni cuentan ni lo anulan) -> aviso «no operar más»;
      abrir después es `CONSEJO IGNORADO: …`.
    · N perdedoras seguidas (solo las rompe una ganadora) -> aviso de parar y analizar; se repite con cada perdedora nueva;
      abrir después NO es infracción.
    · suma de los ticks de todas las decisiones cerradas en negativo >= límite -> aviso de fin de sesión; abrir después es
      `CONSEJO IGNORADO: …`.
- Infracciones propias de la V5: `FUERA DE HORARIO` (apertura fuera de la ventana, en hora de Nueva York) y `SIN ESTRATEGIA`
  (la pone motor.marcar_analisis, cuando ya se conoce la estrategia final).
- Turnos: de arrancar el demonio a cerrarlo; numerados dentro del día CME por su primera operación."""

from collections import defaultdict
from datetime import date, time, timezone
from zoneinfo import ZoneInfo

from . import fuentes


def desde(R):
    """Primera sesión CME con las reglas de la V5, o None si reglas.yaml no trae el bloque (entornos de antes)."""
    v = (R.get('v5') or {}).get('desde')
    if v is None:
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def es_v5(sesion, R):
    d = desde(R)
    return d is not None and sesion is not None and sesion >= d


def categoria(ticks, R):
    """'G' (ganadora), 'P' (perdedora) o 'N' (neutra) con la banda de la V5."""
    b = R['v5']['banda_ticks']
    t = round(ticks, 2)
    return 'G' if t >= b else 'P' if t <= -b else 'N'


def _hm(txt):
    h, m = str(txt).split(':')
    return time(int(h), int(m))


def hora_zona(t_utc, R):
    """Hora UTC del exchange ('YYYY-MM-DD HH:MM:SS.mmm') -> datetime en la zona del horario (Nueva York)."""
    zona = ZoneInfo(R['v5']['horario']['zona'])
    return fuentes.parse_ts(t_utc).replace(tzinfo=timezone.utc).astimezone(zona)


def fuera_de_horario(t_apertura_utc, R):
    """La apertura cae fuera de la ventana [desde, hasta) del horario, medida en su zona (así el desfase del cambio de hora
    entre EE. UU. y España se ajusta solo). Fines de semana: fuera."""
    H = R['v5']['horario']
    t = hora_zona(t_apertura_utc, R)
    return t.weekday() >= 5 or not (_hm(H['desde']) <= t.time() < _hm(H['hasta']))


def reglas_dia(decs, sesion_de, R):
    """Consejos del día sobre las decisiones de la V5. `decs`: cortafuegos.Decision (clave, fila, apertura y cierre en hora de
    Madrid, cierre None si sigue abierta); `sesion_de(d)`: sesión CME de su apertura. Devuelve
    {clave: {'infracciones': [dict], 'avisos': [dict]}}:
      - aviso (en la decisión cuyo cierre lo dispara): {'tipo', 'texto', 'hora' (cierre), 'sonido'};
      - infracción (en cada decisión abierta después del aviso, ese día): {'tipo', 'hora_evento', 'hora_permitida': None,
        'detalle', 'consejo': True}."""
    V = R['v5']
    G, S, T = V['cuatro_ganadoras'], V['perdedoras_seguidas'], V['ticks_perdidos']
    pref = V['consejo_prefijo']
    out = defaultdict(lambda: {'infracciones': [], 'avisos': []})
    por_dia = defaultdict(list)
    for d in decs:
        ses = sesion_de(d)
        if es_v5(ses, R):
            por_dia[ses].append(d)
    for ses, ds in por_dia.items():
        ganadoras = racha = 0
        hubo_perdedora = False
        perdidos = 0.0
        fin_g = fin_p = None           # cierre que disparó «no operar más» / «fin de sesión»
        for d in sorted((x for x in ds if x.cierre is not None), key=lambda x: (x.cierre, x.clave)):
            t = round(d.fila.ticks, 2)
            c = categoria(t, R)
            if c == 'G':
                ganadoras, racha = ganadoras + 1, 0
                if ganadoras == G['n'] and not hubo_perdedora and fin_g is None:
                    fin_g = d.cierre
                    out[d.clave]['avisos'].append({'tipo': 'cuatro_ganadoras', 'texto': G['aviso'].format(n=G['n']),
                                                   'hora': d.cierre, 'sonido': G['sonido']})
            elif c == 'P':
                hubo_perdedora, racha = True, racha + 1
                if racha >= S['n']:
                    out[d.clave]['avisos'].append({'tipo': 'perdedoras_seguidas', 'texto': S['aviso'].format(n=racha),
                                                   'hora': d.cierre, 'sonido': S['sonido']})
            if t < 0:
                perdidos += -t
                if perdidos >= T['limite'] and fin_p is None:
                    fin_p = d.cierre
                    out[d.clave]['avisos'].append({'tipo': 'ticks_perdidos',
                                                   'texto': T['aviso'].format(ticks=f'{perdidos:g}', limite=T['limite']),
                                                   'hora': d.cierre, 'sonido': T['sonido']})
        for d in ds:
            for fin, regla in ((fin_g, G), (fin_p, T)):
                if fin is not None and d.apertura > fin:
                    out[d.clave]['infracciones'].append({
                        'tipo': pref + regla['infraccion'], 'hora_evento': d.apertura, 'hora_permitida': None,
                        'detalle': f"aviso a las {fin:%H:%M:%S}", 'consejo': True})
    return dict(out)


def columnas_dia(filas, abiertas=()):
    """Datos del día de cada decisión cerrada (fila de líder), para todas las sesiones: {clave: (nº en el día por orden de
    apertura, acumulado del día en ticks tras su cierre, ticks perdidos del día tras su cierre)}. El nº cuenta también las
    decisiones `abiertas` (11/11/2026, revisión V5, hallazgo 2): si A abre antes que B pero B cierra antes, B ya es la 2ª y
    no cambia cuando A cierra. Los dos acumulados van en orden de cierre y solo cuentan cerradas (nunca el flotante)."""
    from .motor import sesion_cme              # import diferido: motor importa este módulo
    por_dia, abiertas_dia = defaultdict(list), defaultdict(list)
    for f in filas:
        if not f.es_seguidora and f.sesion is not None:
            por_dia[f.sesion].append(f)
    for f in abiertas:
        if not f.es_seguidora and f.t_apertura_utc:
            abiertas_dia[sesion_cme(f.t_apertura_utc)].append(f)
    out = {}
    for ses, fs in por_dia.items():
        todas = fs + abiertas_dia.get(ses, [])
        orden = {f.clave: i + 1 for i, f in enumerate(sorted(todas, key=lambda f: (f.t_apertura_utc, f.clave)))}
        acum = perdidos = 0.0
        for f in sorted(fs, key=lambda f: (f.t_cierre_utc, f.clave)):
            t = round(f.ticks, 2)
            acum += t
            if t < 0:
                perdidos += -t
            out[f.clave] = (orden[f.clave], round(acum, 2), round(perdidos, 2))
    return out


def asignar_turnos(filas, turnos, R):
    """Turno de cada decisión de la V5 -> f.turno: 'T1', 'T2'… o el texto de «turno fantasma» (operada con el demonio
    apagado). `turnos`: [(inicio, fin|None)] en UTC del reloj del PC, en orden; None = sigue en marcha. Una decisión es del
    turno en el que se abrió. Dentro de cada día CME los turnos se numeran por su primera decisión: uno sin decisiones no
    consume número. Las seguidoras llevan el de su líder; lo anterior a la V5 no tiene turno (None)."""
    T = R['v5']['turno']
    por_dia = defaultdict(list)
    for f in filas:
        f.turno = None
        if not f.es_seguidora and es_v5(f.sesion, R):
            por_dia[f.sesion].append(f)
    for fs in por_dia.values():
        numeros = {}
        for f in sorted(fs, key=lambda f: (f.t_apertura_utc, f.clave)):
            i = next((k for k, (a, b) in enumerate(turnos) if a <= f.t_apertura_utc and (b is None or f.t_apertura_utc <= b)),
                     None)
            if i is None:
                f.turno = T['fantasma']
            else:
                numeros.setdefault(i, len(numeros) + 1)
                f.turno = T['formato'].format(n=numeros[i])
    for f in filas:
        if f.es_seguidora and f.lider is not None:
            f.turno = f.lider.turno


def ratio_planeado(estrategia, R):
    """Ratio B/R planeado de la estrategia (Q40): el suyo si reglas.yaml lo trae; si no, el defecto (+204 / −136)."""
    P = R['v5']['ratio_planeado']
    return (P.get('estrategias') or {}).get(str(estrategia), P['defecto'])


def aviso_fin_turno(ahora_utc, R):
    """(fecha, texto) si `ahora_utc` (UTC) ya ha pasado la hora del aviso de fin de turno de ese día y sigue dentro de la
    ventana; None si no. El que llama lo lanza una vez por fecha."""
    H = R['v5']['horario']
    t = hora_zona(ahora_utc, R)
    if t.weekday() >= 5 or not (_hm(H['fin_turno']['hora']) <= t.time() < _hm(H['hasta'])):
        return None
    return t.date().isoformat(), H['fin_turno']['texto']


def recordatorio(R):
    return R['v5']['recordatorio']


__all__ = ['desde', 'es_v5', 'categoria', 'fuera_de_horario', 'reglas_dia', 'columnas_dia', 'asignar_turnos',
           'ratio_planeado', 'aviso_fin_turno', 'recordatorio', 'hora_zona']
