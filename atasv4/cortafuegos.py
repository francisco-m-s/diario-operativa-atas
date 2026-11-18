"""Cortafuegos (spec §4.5): infracciones de entrada, preventivos al cerrar e infracciones por incumplirlos.

Se cuentan DECISIONES a nivel global (todas las cuentas; la copia en una seguidora no cuenta). Una
entrada escalonada sí es una decisión nueva. Todas las horas son de Madrid, del exchange (las del log pasan
por el reloj ATAS), y las ventanas se miden con la hora de apertura. Textos, tiempos y umbrales en
reglas.yaml (bloque `cortafuegos`).
"""

from dataclasses import dataclass
from datetime import timedelta

from . import fuentes, v5 as mv5
from .fuentes import parse_ts

ESQUEMA = """
CREATE TABLE IF NOT EXISTS cortafuegos_saltados (  -- una fila por INFRACCIÓN (no por preventivo)
  id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT, cuenta TEXT, tipo TEXT,
  hora_evento TEXT, hora_permitida TEXT, detalle TEXT, UNIQUE (decision_id, cuenta, tipo));
CREATE TABLE IF NOT EXISTS avisos_emitidos (       -- avisos en vivo ya lanzados (uno por evento)
  clave TEXT PRIMARY KEY, t TEXT, texto TEXT, notificado INTEGER);
"""


@dataclass
class Decision:
    clave: str
    fila: object
    apertura: object               # datetime Madrid
    cierre: object                 # datetime Madrid o None si sigue abierta
    neto: float
    perdedora: bool = None         # spec §10.4, según la cuenta que mide la decisión; None = por el neto (el criterio de antes)
    ganadora: bool = False


def _madrid(t_utc):
    return fuentes.utc_a_madrid(t_utc)


def _hora(dt):
    return dt.strftime('%H:%M:%S')


def cuenta_que_mide(f):
    """Fila de la cuenta que mide una decisión (spec §10.4): si en la decisión (líder o seguidoras cerradas) hay alguna
    real, la real (la líder si lo es; si no, la primera real por nombre); si no, la líder."""
    if f.real:
        return f
    reales = sorted((x for x in f.copias if x.real and x.t_cierre_utc), key=lambda x: x.cuenta)
    return reales[0] if reales else f


def decisiones(cerradas, abiertas, R=None):
    """Decisiones (filas de líder, la copia no cuenta) en orden de apertura. Con `R`, cada cerrada lleva si es perdedora y
    si es ganadora para la cuenta que la mide (spec §10.4): real -> su neto <= racha.perdedora_max; prueba -> su
    Resultado (ticks) <= cortafuegos.perdida.prueba_ticks; ganadora: su Resultado (ticks) >= cortafuegos.seguidas.ganadora_ticks.
    Una sim activada (ni real ni prueba, §9.1) conserva el criterio de siempre: el neto. Solo para las decisiones abiertas
    desde `plan4.desde`; las anteriores siguen con el neto de la líder (`perdedora` None: `evaluar` usa el de antes)."""
    from .motor import regla_nueva                # import diferido: motor usa este módulo
    out = []
    for f in cerradas:
        if f.es_seguidora:
            continue
        d = Decision(f.clave, f, _madrid(f.t_apertura_utc), _madrid(f.t_cierre_utc), f.neto)
        if R is not None and regla_nueva(f.sesion, R):
            m = cuenta_que_mide(f)
            d.perdedora = (m.neto <= R['racha']['perdedora_max'] if (m.real or m.sim)
                           else round(m.ticks, 2) <= R['cortafuegos']['perdida']['prueba_ticks'])
            d.ganadora = round(m.ticks, 2) >= R['cortafuegos']['seguidas']['ganadora_ticks']
        out.append(d)
    out += [Decision(f.clave, f, _madrid(f.t_apertura_utc), None, None) for f in abiertas if not f.es_seguidora]
    return sorted(out, key=lambda d: (d.apertura, d.clave))


def detalle_exceso(f, R):
    """Detalle de CONTRATOS > PERMITIDOS o None. Prueba de 25K (spec §10.3): posición de la cuenta frente al máximo según la
    distancia al threshold en la apertura (lo calcula el motor). Otra prueba (§9.3): NQ-equivalentes de la cuenta frente al
    máximo, sin mirar el margen. Real (§8.2, §10.1) y sim (§5): contratos del símbolo frente a los permitidos por el margen."""
    if f.dist_regla:
        return f.exceso_dist
    if f.neq_max is not None:
        if f.neq is not None and f.neq > f.neq_max:
            from .motor import fmt_es              # import diferido: motor usa este módulo
            return R['pruebas']['contratos']['detalle'].format(neq=fmt_es(f.neq, 3), max=fmt_es(f.neq_max, 3))
        return None
    if f.permitidos is not None and f.pos_max > f.permitidos:
        return f"{f.pos_max:g} contratos con {f.permitidos} permitidos"
    return None


def tipos_propios(R):
    """Infracciones propias de cada cuenta, también de una seguidora (cada una tiene su margen, su objetivo y su día)."""
    P = R['pruebas']
    return {R['observaciones']['contratos_excedidos'], P['objetivo_alcanzado']['infraccion'],
            P['perdida_diaria']['infraccion']}


def infracciones_de_entrada(f, R):
    """[(tipo, detalle)] de CONTRATOS > PERMITIDOS, SIN SL y SL CANCELADO de una fila cerrada, más las de su cuenta
    (OPERACIÓN TRAS OBJETIVO / TRAS PÉRDIDA DIARIA, spec §9.5 y §9.7)."""
    O = R['observaciones']
    out = []
    exceso = detalle_exceso(f, R)
    if exceso:
        out.append((O['contratos_excedidos'], exceso))
    if es_v5(f, R):                 # spec §16: en la V5 no existen SIN SL ni SL CANCELADO; sí FUERA DE HORARIO
        if f.t_apertura_utc and mv5.fuera_de_horario(f.t_apertura_utc, R):
            H = R['v5']['horario']
            out.append((H['infraccion'], f"abierta a las {_madrid(f.t_apertura_utc):%H:%M:%S}"))
        return out + list(f.infracciones_cuenta)
    if f.telemetria and f.simbolo_ok and f.sin_sl:
        out.append((O['sin_sl'], f"sin SL a los {R['cortafuegos']['sin_sl_s']} s"))
    if f.sl_cancelado:
        out.append((O['sl_cancelado'], ''))
    return out + list(f.infracciones_cuenta)


def es_v5(f, R):
    """La fila (cerrada o abierta) es de la V5: sesión CME de su apertura >= v5.desde (import diferido: motor usa este módulo)."""
    if mv5.desde(R) is None or not f.t_apertura_utc:
        return False
    from .motor import sesion_de
    return mv5.es_v5(sesion_de(f), R)


def _sesion(dt_madrid):
    """Sesión CME de una hora de Madrid del exchange (import diferido: motor usa este módulo)."""
    from .motor import sesion_cme
    return sesion_cme(fuentes.fmt_ts(fuentes.madrid_a_utc(dt_madrid)))


def _perdidas_seguidas(decs, perdedora, R):
    """Racha global de pérdidas (spec §10.4): todas las cuentas, por decisión (la copia no cuenta), dentro de una sesión CME y
    en orden de cierre. Una perdedora suma 1, una ganadora la pone a 0 y una neutra ni suma ni rompe; el cambio de sesión
    CME la pone a 0. Solo cuentan las decisiones abiertas desde `plan4.desde`. Con la racha >= `minimo` tras una perdedora: preventivo `<N> PÉRDIDAS SEGUIDAS: NO OPERAR HASTA hh:mm:ss`
    = cierre + `espera_s`. Devuelve {'preventivos': {clave: (texto, hora)}, 'bloqueos': [(cierre, hora, sesión)]}."""
    S = R['cortafuegos']['seguidas']
    espera = timedelta(seconds=S['espera_s'])
    from .motor import regla_nueva                # import diferido: motor usa este módulo
    out = {'preventivos': {}, 'bloqueos': []}
    racha, sesion = 0, None
    for d in sorted((x for x in decs if x.cierre is not None and regla_nueva(_sesion(x.apertura), R)),
                    key=lambda x: (x.cierre, x.clave)):
        ses = _sesion(d.cierre)
        if ses != sesion:
            racha, sesion = 0, ses
        if perdedora(d):
            racha += 1
        elif d.ganadora:
            racha = 0
        if perdedora(d) and racha >= S['minimo']:
            fin = d.cierre + espera
            out['preventivos'][d.clave] = (S['preventivo'].format(n=racha, hora='{hora}'), fin)
            out['bloqueos'].append((d.cierre, fin, ses))
    return out


def _a_foto(e):
    """Evaluación de una decisión -> JSON (las horas, a texto)."""
    def h(x):
        return None if x is None else fuentes.fmt_ts(x)
    return {'preventivos': [[t, h(x)] for t, x in e['preventivos']],
            'infracciones': [dict(i, hora_evento=h(i['hora_evento']), hora_permitida=h(i['hora_permitida']))
                             for i in e['infracciones']],
            'avisos': [dict(a, hora=h(a['hora'])) for a in e.get('avisos', [])]}


def _de_foto(d):
    def h(x):
        return None if x is None else parse_ts(x)
    return {'preventivos': [(t, h(x)) for t, x in d['preventivos']],
            'infracciones': [dict(i, hora_evento=h(i['hora_evento']), hora_permitida=h(i['hora_permitida']))
                             for i in d['infracciones']],
            'avisos': [dict(a, hora=h(a['hora'])) for a in d.get('avisos', [])]}


def evaluar(decs, R, foto=None):
    """{clave: {'preventivos': [(texto, hora)], 'infracciones': [dict]}} para todas las decisiones."""
    C = R['cortafuegos']
    perd_max = R['racha']['perdedora_max']
    espera = timedelta(seconds=C['perdida']['espera_s'])
    out = {d.clave: {'preventivos': [], 'infracciones': [], 'avisos': []} for d in decs}
    desde5 = mv5.desde(R)
    v5 = {d.clave for d in decs if desde5 is not None and _sesion(d.apertura) >= desde5}

    def perdedora(d):                  # spec §10.4; una Decision hecha a mano (sin cuenta que mida) va por el neto, como antes
        return d.perdedora if d.perdedora is not None else d.neto is not None and d.neto <= perd_max

    seguidas = _perdidas_seguidas(decs, perdedora, R)
    foto = foto or {}
    for i, d in enumerate(decs):
        if d.clave in foto and d.cierre is not None:      # spec §16.10: decisión ya archivada: su evaluación está en la foto
            out[d.clave] = _de_foto(foto[d.clave])
            continue
        if d.clave in v5:              # spec §16: en la V5 no hay cortafuegos por tiempo ni «NO OPERAR HASTA»
            continue
        prev = decs[:i]
        # Reentrada: apertura antes de que venza la espera de una pérdida ya cerrada
        horas = [p.cierre + espera for p in prev
                 if p.cierre is not None and perdedora(p) and p.cierre <= d.apertura < p.cierre + espera]
        if horas:
            out[d.clave]['infracciones'].append({'tipo': C['perdida']['infraccion'], 'hora_evento': d.apertura,
                                                 'hora_permitida': max(horas), 'detalle': ''})
        # Reentrada tras pérdidas seguidas (§10.4): apertura antes de que venza el bloqueo de una racha de su sesión
        horas = [fin for cierre, fin, ses in seguidas['bloqueos']
                 if cierre <= d.apertura < fin and ses == _sesion(d.apertura)]
        if horas:
            out[d.clave]['infracciones'].append({'tipo': C['seguidas']['infraccion'], 'hora_evento': d.apertura,
                                                 'hora_permitida': max(horas), 'detalle': ''})
        # Sobreoperación: ya había N aperturas en la ventana móvil que termina en esta apertura
        for v in C['ventanas']:
            W, n = timedelta(minutes=v['minutos']), v['aperturas']
            en = sorted(p.apertura for p in prev if d.apertura - W < p.apertura <= d.apertura)
            if len(en) >= n:
                out[d.clave]['infracciones'].append({
                    'tipo': v['infraccion'], 'hora_evento': d.apertura, 'hora_permitida': en[-n] + W,
                    'detalle': f"{len(en) + 1}ª apertura en {v['minutos']} min"})
        if d.cierre is None:
            continue
        # Preventivos al cerrar
        prevs = []
        if perdedora(d):
            prevs.append((C['perdida']['preventivo'], d.cierre + espera))
        if d.clave in seguidas['preventivos']:
            prevs.append(seguidas['preventivos'][d.clave])
        for v in C['ventanas']:
            W, n = timedelta(minutes=v['minutos']), v['aperturas']
            ap = sorted(x.apertura for x in decs if d.cierre - W < x.apertura <= d.cierre)
            if len(ap) >= n:      # el filtro ya garantiza ap[-n] > d.cierre - W, o sea ap[-n] + W > d.cierre
                prevs.append((v['preventivo'], ap[-n] + W))
        out[d.clave]['preventivos'] = prevs
    if v5:                             # consejos del día de la V5 (4 ganadoras, perdedoras seguidas, ticks perdidos)
        for clave, e in mv5.reglas_dia([d for d in decs if d.clave in v5], lambda d: _sesion(d.apertura), R).items():
            if clave in foto and clave in out and next(d for d in decs if d.clave == clave).cierre is not None:
                continue               # ya viene entera de la foto
            out[clave]['infracciones'] += e['infracciones']
            out[clave]['avisos'] += e['avisos']
    return out


def anotar(cerradas, abiertas, R, foto=None):
    """Añade las etiquetas de cortafuegos a Observaciones demonio de cada decisión cerrada y marca las
    filas que las llevan (fondo rojo en el sheet). Devuelve la evaluación completa."""
    sep = R['observaciones']['separador']
    ev = evaluar(decisiones(cerradas, abiertas, R), R, foto)
    fuera = (R.get('v5') or {}).get('horario', {}).get('infraccion')
    for f in cerradas:
        entrada = infracciones_de_entrada(f, R)
        if fuera and any(t == fuera for t, _ in entrada):      # la única de entrada que no escribe motor._observaciones
            f.obs = sep.join(([f.obs] if f.obs else []) + [fuera])
        if f.es_seguidora:
            f.cortafuegos = bool(entrada or f.etiquetas_cuenta)
            continue
        e = ev.get(f.clave, {'preventivos': [], 'infracciones': []})
        etiquetas = [i['tipo'] for i in e['infracciones']]
        etiquetas += [texto.format(hora=_hora(h)) for texto, h in e['preventivos']]
        avisos = [a['texto'] for a in e.get('avisos', [])]       # V5: el aviso que disparó su cierre, como texto sin color
        if etiquetas or avisos:
            f.obs = sep.join(([f.obs] if f.obs else []) + etiquetas + avisos)
        if es_v5(f, R):
            # spec §16.5: rojo solo por infracción (las de entrada: contratos, tras objetivo, fuera de horario; SIN ESTRATEGIA
            # la pone marcar_analisis); naranja por CONSEJO IGNORADO; los textos de las firmas y los avisos van sin color
            f.cortafuegos = bool(entrada)
            f.naranja = any(i.get('consejo') for i in e['infracciones'])
            continue
        # los preventivos de la cuenta (§8.4, §9.5–§9.7) también ponen la celda en rojo, sin aviso sonoro
        f.cortafuegos = bool(entrada or etiquetas or f.etiquetas_cuenta)
    return ev


def registrar(con, filas, ev, R):
    """Infracciones -> cortafuegos_saltados (idempotente). Las de una seguidora cuelgan de la decisión
    de su líder; solo las de tipos_propios (CONTRATOS > PERMITIDOS y las de su prueba) son de cada cuenta."""
    con.executescript(ESQUEMA)
    for decision, cuenta, tipo, h_ev, h_perm, detalle in _infracciones(filas, ev, R):
        con.execute('INSERT OR IGNORE INTO cortafuegos_saltados '
                    '(decision_id, cuenta, tipo, hora_evento, hora_permitida, detalle) VALUES (?,?,?,?,?,?)',
                    (decision, cuenta, tipo, fuentes.fmt_ts(h_ev), fuentes.fmt_ts(h_perm) if h_perm else None, detalle))
    con.commit()


def _infracciones(filas, ev, R):
    """[(decision_id, cuenta, tipo, hora_evento, hora_permitida, detalle)] de todas las infracciones que da el cálculo."""
    propias = tipos_propios(R)
    out = []
    for f in filas:
        # Una seguidora cuyo líder aún no ha cerrado tendría decision_id provisional (el suyo propio) y
        # al cerrar el líder se reinsertaría con otro: se espera al ciclo siguiente, con el ID definitivo.
        if not f.op_id or (f.es_seguidora and not f.lider.op_id):
            continue
        decision = f.decision_id
        for tipo, detalle in infracciones_de_entrada(f, R):
            if f.es_seguidora and tipo not in propias:
                continue
            out.append((decision, f.cuenta, tipo, _madrid(f.t_apertura_utc), None, detalle))
        if not f.es_seguidora:
            for i in ev.get(f.clave, {}).get('infracciones', []):
                out.append((decision, f.cuenta, i['tipo'], i['hora_evento'], i['hora_permitida'], i['detalle']))
    return out


def purgar_saltados(con, filas, ev, R):
    """Borra de cortafuegos_saltados las filas que el cálculo de ahora ya no da (11/11/2026): la tabla solo se rellenaba
    (INSERT OR IGNORE) y conservaba infracciones de reglas, emparejamientos o IDs anteriores. `filas`: TODAS las del
    cálculo. Devuelve cuántas borró."""
    vigentes = {(d, c, t) for d, c, t, *_ in _infracciones(filas, ev, R)}
    sobran = [r['id'] for r in con.execute('SELECT id, decision_id, cuenta, tipo FROM cortafuegos_saltados')
              if (r['decision_id'], r['cuenta'], r['tipo']) not in vigentes]
    con.executemany('DELETE FROM cortafuegos_saltados WHERE id=?', [(i,) for i in sobran])
    con.commit()
    return len(sobran)


def purgar_avisos(con, res, ahora_utc, R, dias):
    """Borra de avisos_emitidos (la marca de «este evento ya se avisó») los que ya no corresponden a ningún evento del
    cálculo de ahora y tienen más de `dias` días (11/11/2026). Los de eventos que siguen existiendo se quedan: sin su
    marca, el evento volvería a registrarse en cada ciclo. Devuelve cuántos borró."""
    vigentes = {clave for clave, *_ in eventos_vivos(con, res, ahora_utc, R)}
    limite = fuentes.fmt_ts(_madrid(res.reloj.pc_a_utc(ahora_utc)) - timedelta(days=float(dias)))
    sobran = [r['clave'] for r in con.execute('SELECT clave FROM avisos_emitidos WHERE t < ?', (limite,))
              if r['clave'] not in vigentes]
    con.executemany('DELETE FROM avisos_emitidos WHERE clave=?', [(c,) for c in sobran])
    con.commit()
    return len(sobran)


# ---------------------------------------------------------------------------
# Vigilancia en vivo (cada ciclo del demonio, no al sincronizar el sheet)
# ---------------------------------------------------------------------------

def _decision_real(f):
    """Spec §4.5 (30/09/2026): la decisión de `f` (su líder y las seguidoras de esta) tiene alguna cuenta real."""
    lider = f.lider or f
    return any(x.real for x in [lider] + list(lider.copias))


def eventos_vivos(con, res, ahora_utc, R):
    """[(clave, hora_evento, texto, real, sonido)] de todo lo que merece aviso ahora mismo, sin filtrar duplicados. `real`: su
    decisión tiene alguna cuenta real (de los eventos de la V4, solo esos notifican, spec §4.5). `sonido`: None en un evento
    de la V4; en uno de la V5 (spec §16.4), la clave del sonido de `v5.avisos.sonidos` ('' = notificación sin sonido): los de
    la V5 notifican siempre, sea real o prueba."""
    from . import almacen, motor            # import diferido: motor usa este módulo
    C, O = R['cortafuegos'], R['observaciones']
    pref = C['aviso']['infraccion_prefijo']
    V = R.get('v5') or {}
    A5 = V.get('avisos') or {}
    pref5, son_inf = A5.get('infraccion_prefijo', pref), A5.get('infraccion', '')
    rel = res.reloj
    ahora = fuentes.fmt_ts(_madrid(rel.pc_a_utc(ahora_utc)))      # hora corregida, como la de los eventos
    en_bal = {b['cuenta'] for b in res.bal}
    propias = tipos_propios(R)
    eventos = []

    def donde(f):
        return f"{f.cuenta} {f.simbolo.split('@')[0]}"

    def evento(clave, t, texto, f, sonido=None):
        eventos.append((clave, t, texto, _decision_real(f), sonido))

    # Decisiones: reentrada / sobreoperación (al abrir) y preventivos (al cerrar); en la V5, consejos ignorados y avisos
    filas = {f.clave: f for f in list(res.filas) + list(res.filas_abiertas)}
    for clave, e in res.cortafuegos.items():
        f = filas.get(clave)
        for i in e['infracciones']:
            if i.get('consejo'):
                evento(f"{i['tipo']}|{clave}", i['hora_evento'], f"{pref5}{i['tipo']} ({donde(f)}) {i['detalle']}".strip(),
                       f, son_inf)
                continue
            evento(f"{i['tipo']}|{clave}", i['hora_evento'],
                   f"{pref}{i['tipo']} ({donde(f)}). No operar hasta {_hora(i['hora_permitida'])}", f)
        if e['preventivos']:
            ultima = max(h for _, h in e['preventivos'])
            textos = [t.format(hora=_hora(h)) for t, h in e['preventivos']]
            evento(f"PREVENTIVO|{clave}", _madrid(f.t_cierre_utc),
                   f"NO OPERAR HASTA {_hora(ultima)} ({'; '.join(textos)})", f)
        for a in e.get('avisos', ()):
            evento(f"AVISO {a['tipo']}|{clave}", a['hora'], a['texto'], f, a['sonido'])
    # Entrada: filas cerradas. La seguidora solo avisa de lo suyo (contratos, su prueba) y de los contratos solo si su
    # líder no lo hizo ya.
    for f in res.filas:
        v5 = es_v5(f, R)
        lider_excede = f.lider is not None and any(
            t == O['contratos_excedidos'] for t, _ in infracciones_de_entrada(f.lider, R))
        for tipo, detalle in infracciones_de_entrada(f, R):
            if f.es_seguidora and (tipo not in propias or (tipo == O['contratos_excedidos'] and lider_excede)):
                continue
            evento(f"{tipo}|{f.clave}", _madrid(f.t_apertura_utc),
                   f"{pref5 if v5 else pref}{tipo} ({donde(f)}) {detalle}".strip(), f, son_inf if v5 else None)
    # Entrada: posiciones abiertas (límites de su cuenta: §5, §8.2 o §9.3, calculados por el motor)
    def excede(f):
        return detalle_exceso(f, R) if f.cuenta in en_bal else None

    defecto = R['estrategia_por_defecto']
    for f in res.filas_abiertas:
        v5 = es_v5(f, R)
        detalle = excede(f)
        if detalle is not None and not (f.lider is not None and excede(f.lider) is not None):
            evento(f"{O['contratos_excedidos']}|{f.clave}", _madrid(f.t_apertura_utc),
                   f"{pref5 if v5 else pref}{O['contratos_excedidos']} ({donde(f)}) {detalle}", f, son_inf if v5 else None)
        if v5:              # infracciones de su cuenta ya al abrir (OPERACIÓN TRAS OBJETIVO; también una seguidora). Misma
            for tipo, detalle in f.infracciones_cuenta:      # clave que al cerrar: no se repite
                evento(f"{tipo}|{f.clave}", _madrid(f.t_apertura_utc), f"{pref5}{tipo} ({donde(f)}) {detalle}".strip(), f,
                       son_inf)
        t_ent = f.entradas[0].get('t_c')             # hora corregida de la línea del fill de entrada
        if f.es_seguidora:
            continue
        if v5:
            # spec §16.5: al abrir, FUERA DE HORARIO y SIN ESTRATEGIA (la casilla de la preoperativa sigue en el valor por
            # defecto). SIN SL y SL CANCELADO ya no existen.
            ap = _madrid(f.t_apertura_utc)
            if mv5.fuera_de_horario(f.t_apertura_utc, R):
                H = V['horario']
                evento(f"{H['infraccion']}|{f.clave}", ap, f"{pref5}{H['infraccion']} ({donde(f)})", f, son_inf)
            # SIN ESTRATEGIA solo con una lectura de la casilla POSTERIOR a la apertura (la elección pudo hacerse segundos
            # antes de entrar y aún no estar leída: la lectura va cada preoperativa.lectura_s y no corre durante un sync)
            lectura = almacen.get_estado(con, 'preop_t_lectura', '')
            if lectura and almacen.get_estado(con, 'preop_estrategia', defecto) == defecto:
                t_lect = rel.pc_a_utc(lectura)
                if t_lect >= f.t_apertura_utc:
                    evento(f"{V['sin_estrategia']}|{f.clave}", max(ap, _madrid(t_lect)),
                           f"{pref5}{V['sin_estrategia']} ({donde(f)})", f, son_inf)
            continue
        if not t_ent:
            continue
        lado = motor.lado_proteccion_de(f.direccion)
        altas = [r['t_c'] for r in rel.filas(
            con, "SELECT t_log, fichero, pos FROM ordenes WHERE cuenta=? AND simbolo=? AND lado=? AND etiqueta='SL' "
                 "AND origen='registered'", (f.cuenta, f.simbolo, lado), desde=t_ent)]
        # plazos y horas de los eventos, todo en hora corregida (entrada, SL, cancelación y ahora)
        if motor.sin_sl_a_tiempo(t_ent, ahora, altas, R):
            evento(f"{O['sin_sl']}|{f.clave}", parse_ts(t_ent),
                   f"{pref}{O['sin_sl']} ({donde(f)}) sin SL a los {C['sin_sl_s']} s", f)
        tc = motor.sl_cancelado_sin_reponer(con, f.cuenta, f.simbolo, lado, t_ent, ahora, R, rel)
        if tc:
            evento(f"{O['sl_cancelado']}|{f.clave}", parse_ts(tc), f"{pref}{O['sl_cancelado']} ({donde(f)})", f)
    return eventos


def vigilar(con, res, ahora_utc, R, avisar, log=print):
    """Lanza cada aviso una sola vez. Los eventos viejos (arranque del demonio, reingesta) se registran
    sin avisar. Devuelve los textos notificados. `ahora_utc` es el reloj del PC: se pasa a hora del exchange,
    la de los eventos. De los eventos de la V4 solo notifica una decisión con alguna cuenta real (spec §4.5, 30/09/2026); la de
    solo pruebas se registra igual (notificado = 0). Los de la V5 (spec §16.4) notifican siempre: `avisar(titulo, texto,
    sonido=<clave>)`, con el título y la antigüedad máxima de `v5.avisos`."""
    con.executescript(ESQUEMA)
    A = R['cortafuegos']['aviso']
    A5 = (R.get('v5') or {}).get('avisos') or {}
    ahora = _madrid(res.reloj.pc_a_utc(ahora_utc))
    lanzados = []
    for clave, t_ev, texto, real, sonido in eventos_vivos(con, res, ahora_utc, R):
        if t_ev > ahora or con.execute('SELECT 1 FROM avisos_emitidos WHERE clave=?', (clave,)).fetchone():
            continue
        es5 = sonido is not None
        maximo = A5.get('max_antiguedad_s', A['max_antiguedad_s']) if es5 else A['max_antiguedad_s']
        reciente = (real or es5) and (ahora - t_ev).total_seconds() <= maximo
        con.execute('INSERT INTO avisos_emitidos VALUES (?, ?, ?, ?)',
                    (clave, fuentes.fmt_ts(t_ev), texto, 1 if reciente else 0))
        if reciente:
            log(f"[{'AVISO V5' if es5 else 'CORTAFUEGOS'}] {texto}")
            if es5:
                avisar(A5.get('titulo', A['titulo']), texto, sonido=sonido)
            else:
                avisar(A['titulo'], texto)
            lanzados.append(texto)
    con.commit()
    return lanzados
