"""Vista: pestaña V4 regenerada entera desde SQLite (spec §7) y lectura previa de columnas manuales."""

from datetime import datetime, timedelta

from . import fuentes, motor

DIAS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']

BAL_COLS = ['Cuenta', 'Balance final', 'Threshold', 'Threshold Rithmic (manual)', 'Distancia al threshold',
            'Margen de operativa', 'Contratos permitidos', 'Nº ops (día)', 'Nº ganadas (día)',
            'Resultado (día, neto)', 'Ratio B/R (día)', 'Día semana', 'Fecha', 'Últimos 2 días +50 $',
            'Días +50 (30 d)', 'Inactividad', 'Días +100', 'Prueba',            # 30/09/2026 (spec §8.5, §9.8); V5: 'Inactividad'
            'Observaciones demonio']

# V5 (spec §16.7, 11/11/2026): salen 'Tipo apertura', 'Resultado anterior (ticks)' y 'Análisis LLM'; entran 'Nº en el día',
# 'Acumulado del día (ticks)' y 'Ticks perdidos del día'. 'Ratio B/R' pasa a ser el planeado por la estrategia.
OPS_COLS = ['ID', 'Cuenta', 'Copias', 'Balance final', 'Margen de operativa', 'Largo/Corto',
            'Tipo cierre', 'Estrategia', 'Resultado (ticks)', 'Resultado ($)',
            'Flotante aguantado (ticks)(MAE)', 'Flotante máx (ticks)(MFE)', 'SL (ticks)', 'TP (ticks)',
            'Entrada', 'Máxima amplitud del movimiento', 'Observaciones', 'Observaciones demonio',
            'Calificación',                                                      # 01/10/2026 (spec §4.2, §4.3)
            'Ratio B/R', 'Símbolo', 'Contratos', 'Fecha y hora apertura', 'Duración (s)', 'Comisión',
            'Nº en el día', 'Acumulado del día (ticks)', 'Ticks perdidos del día', 'Racha', 'OrderId']
OC = {c: i for i, c in enumerate(OPS_COLS)}
BC = {c: i for i, c in enumerate(BAL_COLS)}
MANUAL_OPS = {'estrategia': 'Estrategia', 'observaciones': 'Observaciones'}
UNIFICADO_COLS = OPS_COLS[:-1] + ['Origen', 'OrderId']        # histórico unificado (spec §16.9): las de la V5 más el origen
FLOTANTES = {'mae': 'Flotante aguantado (ticks)(MAE)', 'mfe': 'Flotante máx (ticks)(MFE)'}
TOL_FLOTANTE = 0.05            # el demonio escribe MAE/MFE con 1 decimal
N_COLS = len(OPS_COLS)


def _r(x, n=2):
    return '' if x is None else round(x, n)


def fila_bal(b):
    ses = b['sesion']
    return [
        b['nombre'], _r(b['balance']), _r(b['threshold']),
        '' if b['threshold_manual'] is None else b['threshold_manual'],
        _r(b['distancia']), _r(b['margen'], 1), b['permitidos'], b['n_ops'], b['n_ganadas'],
        _r(b['resultado_dia']), _r(b['ratio_dia']), DIAS[ses.weekday()],
        motor.serial_sheets(datetime(ses.year, ses.month, ses.day)),
        ' · '.join(f"{d:%d/%m/%Y}" for d in b['ultimos_validos']),
        # solo reales (§8.5) / solo pruebas (§9.8); vacías en las cuentas a las que no se aplican
        _v(b.get('dias_50_30d')), _v(b.get('inactividad')), _v(b.get('dias_100')), _v(b.get('prueba')),
        b['obs'],
    ]


def _v(x):
    return '' if x is None else x


def fila_op(f, ent, manual, defecto_estrategia, tardias=()):
    m = manual.get(f.op_id, {})
    O = ent.reglas['observaciones']
    obs = f.obs
    if f.op_id in tardias:                       # preoperativa: estrategia elegida con la operación abierta
        obs = O['separador'].join(x for x in (obs, O['estrategia_tardia']) if x)
    t_madrid = fuentes.utc_a_madrid(f.t_apertura_utc)
    row = [''] * N_COLS
    row[OC['ID']] = f.op_id
    row[OC['Cuenta']] = f.nombre
    row[OC['Copias']] = ', '.join(c.nombre for c in f.copias if c.nombre)  # una copia aún abierta no lo tiene
    row[OC['Balance final']] = _r(f.balance_tras)
    row[OC['Margen de operativa']] = _r(f.margen_tras, 1)
    row[OC['Largo/Corto']] = 'Largo' if f.direccion > 0 else 'Corto'
    row[OC['Tipo cierre']] = f.tipo_cierre
    row[OC['Estrategia']] = m.get('estrategia') or defecto_estrategia
    row[OC['Resultado (ticks)']] = _r(f.ticks)
    row[OC['Resultado ($)']] = _r(f.neto)
    row[OC['Flotante aguantado (ticks)(MAE)']] = _r(f.mae, 1)
    row[OC['Flotante máx (ticks)(MFE)']] = _r(f.mfe, 1)
    row[OC['SL (ticks)']] = _r(f.sl_ini)
    row[OC['TP (ticks)']] = _r(f.tp_final)
    row[OC['Entrada']] = f.entrada
    row[OC['Máxima amplitud del movimiento']] = _r(f.max_amplitud)         # ticks (01/10/2026)
    row[OC['Observaciones']] = m.get('observaciones') or ''
    row[OC['Observaciones demonio']] = obs
    row[OC['Calificación']] = f.calificacion                                # la de la fila de la líder
    row[OC['Ratio B/R']] = _r(f.ratio)
    row[OC['Símbolo']] = f.simbolo
    row[OC['Contratos']] = int(f.contratos) if float(f.contratos).is_integer() else f.contratos
    row[OC['Fecha y hora apertura']] = motor.serial_sheets(t_madrid)
    row[OC['Duración (s)']] = f.duracion
    row[OC['Comisión']] = _r(f.comision)
    row[OC['Nº en el día']] = _v(f.n_dia)
    row[OC['Acumulado del día (ticks)']] = _r(f.acum_dia)
    row[OC['Ticks perdidos del día']] = _r(f.perdidos_dia)
    row[OC['Racha']] = f.racha
    row[OC['OrderId']] = f.order_id
    return row


def construir(res, ent, manual, playbook, preop_estrategia=None, tardias=(), compuerta=None, semana=None, mercado=None,
              desde_utc=None, hasta_utc=None, recordatorio=None):
    """Layout completo de la pestaña: valores + posiciones para formatos.
    preop_estrategia: valor que queda en la celda de la preoperativa (spec §7.2); tardias: IDs marcados; compuerta: valores
    que quedan en las filas SÍ/NO del paso 2 (spec §10.6; vacías si no se da); semana: fila SEMANA del BAL (spec §11.2, de
    `fila_semana`; sin ella, no se escribe); mercado: valor de la fila `Estado del mercado` (spec §12; el defecto si no se da);
    desde_utc: corte del último ciclo archivado (spec §15): solo salen las decisiones abiertas después (el BAL no cambia);
    hasta_utc: solo al archivar hasta un día dado: salen las abiertas antes de esa hora; recordatorio: texto del recordatorio
    de inicio de turno (spec §16.3), en la fila vacía de encima de la preoperativa; None = no se muestra."""
    R = ent.reglas
    P = R['preoperativa']['estrategia']
    bal = sorted(res.bal, key=lambda b: b['nombre'])
    decisiones = sorted((f for f in res.filas if not f.es_seguidora and (not desde_utc or f.t_apertura_utc > desde_utc)
                         and (not hasta_utc or f.t_apertura_utc < hasta_utc)),
                        key=lambda f: (f.t_apertura_utc, f.op_id), reverse=True)
    celdas_manuales, celdas_cortafuegos, celdas_naranja = [], [], []
    vals = [BAL_COLS + [''] * (N_COLS - len(BAL_COLS))]
    vals += [fila_bal(b) + [''] * (N_COLS - len(BAL_COLS)) for b in bal]
    celdas_semana = []
    if semana is not None:       # spec §11.2: una fila más del bloque BAL, tras las cuentas y antes de las 2 vacías
        celdas_semana = [(len(vals), 0), (len(vals), 1)] if semana['rojo'] else []
        vals.append([semana['nombre'], semana['texto']] + [''] * (N_COLS - 2))
    vals += [[''] * N_COLS, [''] * N_COLS]
    recordatorio_fila = None
    if recordatorio:             # la segunda fila vacía: la primera separa el BAL (parsear_hoja para en ella)
        recordatorio_fila = len(vals) - 1
        vals[recordatorio_fila][1] = recordatorio        # columna B: la A vacía sigue marcando el fin del BAL
    defecto = R['estrategia_por_defecto']
    preop_col = OC[P['columna']]
    M = R['preoperativa']['mercado']              # estado del mercado (§12): encima de la estrategia, en su misma columna
    mercado_fila = len(vals)
    fila_mercado = [''] * N_COLS
    fila_mercado[0] = M['etiqueta']
    fila_mercado[preop_col] = mercado or M['defecto']
    vals.append(fila_mercado)
    preop_fila = len(vals)
    fila_preop = [''] * N_COLS
    fila_preop[0] = P['etiqueta']
    fila_preop[preop_col] = preop_estrategia or defecto
    vals.append(fila_preop)
    Q = R['preoperativa']['compuerta']           # paso 2 (§10.6): una fila SÍ/NO por texto, debajo de la estrategia
    compuerta_fila = len(vals)
    filas_compuerta = Q['filas'] if Q.get('mostrar', True) else []      # V5: sin filas SÍ/NO
    for i, texto in enumerate(filas_compuerta):
        fila_c = [''] * N_COLS
        fila_c[0] = texto
        fila_c[preop_col] = compuerta[i] if compuerta and i < len(compuerta) else ''
        vals.append(fila_c)
    ops_header = len(vals)                       # índice 0-based de la fila de encabezado de operaciones
    vals.append(list(OPS_COLS))
    for f in decisiones:
        fila = len(vals)
        vals.append(fila_op(f, ent, manual, defecto, tardias))
        if f.cortafuegos:
            celdas_cortafuegos.append((fila, OC['Observaciones demonio']))
        elif f.naranja:                  # CONSEJO IGNORADO (V5): naranja; si además hay infracción, manda el rojo
            celdas_naranja.append((fila, OC['Observaciones demonio']))
        if f.mae_manual:
            celdas_manuales.append((fila, OC[FLOTANTES['mae']]))
        if f.mfe_manual:
            celdas_manuales.append((fila, OC[FLOTANTES['mfe']]))
    return {
        'valores': vals,
        'n_bal': len(bal),
        'ops_header': ops_header,
        'ops_ini': ops_header + 1,
        'ops_fin': ops_header + 1 + len(decisiones),     # exclusivo
        'estrategias': [defecto] + [p for p in playbook if p != defecto],
        'mercado_fila': mercado_fila,
        'preop_fila': preop_fila,
        'preop_col': preop_col,
        'compuerta_fila': compuerta_fila,
        'compuerta_n': len(filas_compuerta),
        'celdas_manuales': celdas_manuales,
        'celdas_cortafuegos': celdas_cortafuegos,
        'celdas_naranja': celdas_naranja,
        'recordatorio_fila': recordatorio_fila,
        'celdas_semana': celdas_semana,
    }


def poner_r17(lay, texto):
    """Texto de R17 (spec §13) en la celda de la derecha de la de la estrategia, en el layout ya construido. Sin texto no
    toca nada. Es la única celda que `construir` deja vacía a su derecha: la reescritura entera de la pestaña no la borra."""
    if texto:
        lay['valores'][lay['preop_fila']][lay['preop_col'] + 1] = texto


# --- recuento semanal de los objetivos de proceso (spec §11) ---

CLAVES_OBJETIVOS = ('sin_estrategia', 'infracciones', 'proceso_correcto')     # V5 (spec §16.8): reglas.yaml -> v5.objetivos


def _obj(R):
    return R['v5']['objetivos']


def evaluar_semana(r, manual, R):
    """Objetivos de una semana (spec §11.1). r: recuento de `motor.recuento_semanal` o None (semana sin decisiones); manual:
    lo escrito en la columna 5 (texto, número o None). Devuelve {n, valores: [valor 1..4 (None = sin evaluar)], cumple:
    [bool|None], manual, manual_cumple, k, total}. `cumple` de 1–3: valor <= maximo; de 4: % entero (por defecto) >=
    minimo_pct. Cumplidos = k/total, con total los objetivos evaluados entre 1 y 4 más el 5 si tiene un número."""
    O = _obj(R)
    r = r or {'n': 0, 'sin_estrategia': 0, 'infracciones': 0, 'correctas': 0}
    valores = [r['sin_estrategia'], r['infracciones']]
    cumple = [v <= O[k]['maximo'] for v, k in zip(valores, CLAVES_OBJETIVOS)]
    pct = r['correctas'] * 100 // r['n'] if r['n'] else None        # entero por defecto: lo que se ve es lo que se juzga
    valores.append(pct)
    cumple.append(None if pct is None else pct >= O['proceso_correcto']['minimo_pct'])
    evaluados = [c for c in cumple if c is not None]            # V5: sin columna manual
    return {'n': r['n'], 'valores': valores, 'cumple': cumple, 'manual': None, 'manual_cumple': None,
            'k': sum(evaluados), 'total': len(evaluados)}


def _marca(cumple, R):
    V = R['objetivos']['vista']
    return '' if cumple is None else (V['cumple'] if cumple else V['incumple'])


def _valor(i, v, R):
    if v is None:
        return R['objetivos']['vista']['sin_dato']
    return _obj(R)['proceso_correcto']['valor'].format(pct=v) if i == len(CLAVES_OBJETIVOS) - 1 else v


def fila_semana(rec, lunes, R):
    """Fila SEMANA del BAL (spec §11.2): {nombre, texto, rojo} de la semana que empieza el `lunes`. `rec`: recuento de
    `motor.recuento_semanal` (la semana sin decisiones no está: cuenta 0). El texto lleva los objetivos 1–4 con ✔/✘, el 5 como
    manual y los cumplidos k/N de los 4; rojo si hay algún ✘."""
    O = R['objetivos']
    V = O['vista']
    ev = evaluar_semana(rec.get(lunes), None, R)
    partes = [V['parte_bal'].format(etiqueta=_obj(R)[k]['bal'], valor=_valor(i, ev['valores'][i], R),
                                    marca=_marca(ev['cumple'][i], R)).strip() for i, k in enumerate(CLAVES_OBJETIVOS)]
    decisiones = (V['decision_una'] if ev['n'] == 1 else V['decisiones']).format(n=ev['n'])
    texto = V['bal'].format(decisiones=decisiones, partes=V['separador'].join(partes),
                            cumplidos=V['cumplidos'].format(k=ev['k'], total=ev['total']))
    nombre = O['semana']['bal'].format(lunes=lunes, viernes=motor.fin_semana(lunes, R))
    return {'nombre': nombre, 'texto': texto, 'rojo': any(c is False for c in ev['cumple'])}


def semanas_cerradas(rec, actual):
    """Lunes de las semanas cerradas (spec §11.3), la más reciente primero: desde la primera con decisiones hasta la anterior a
    la de la sesión en curso (`actual`, su lunes); las intermedias sin decisiones también salen."""
    if not rec:
        return []
    lunes, out = min(rec), []
    while lunes < actual:
        out.append(lunes)
        lunes += timedelta(days=7)
    return out[::-1]


def cabecera_objetivos(R):
    O = R['objetivos']
    c = O['columnas']
    return [c['semana'], c['decisiones']] + [_obj(R)[k]['columna'] for k in CLAVES_OBJETIVOS] + [c['cumplidos']]


def valor_manual(v):
    """Celda de la columna 5 -> texto que se guarda ('' = vacía). Un número entero se guarda sin decimales."""
    if v is None or isinstance(v, bool):
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def layout_objetivos(rec, actual, manual, R):
    """Pestaña `Objetivos semanales` entera (spec §11.3). rec: recuento; actual: lunes de la sesión en curso; manual:
    {lunes ISO: texto} de la columna 5 (SQLite). Devuelve {valores, rojos, manuales: [(fila, col)], semanas: {etiqueta: lunes
    ISO}, col_manual, n_cols}. Las celdas 1–4 llevan el número con ✔/✘ (rojo si ✘); la 5 es lo que escribió el operador."""
    O = R['objetivos']
    V = O['vista']
    cab = cabecera_objetivos(R)
    col5 = 2 + len(CLAVES_OBJETIVOS)
    vals, rojos, manuales, semanas = [cab], [], [], {}
    for lunes in semanas_cerradas(rec, actual):
        m = manual.get(lunes.isoformat(), '')
        ev = evaluar_semana(rec.get(lunes), m, R)
        etiqueta = O['semana']['historico'].format(lunes=lunes, viernes=motor.fin_semana(lunes, R))
        semanas[etiqueta] = lunes.isoformat()
        fila = len(vals)
        celdas = [V['celda'].format(valor=_valor(i, ev['valores'][i], R), marca=_marca(ev['cumple'][i], R)).strip()
                  for i in range(len(CLAVES_OBJETIVOS))]
        rojos += [(fila, 2 + i) for i, c in enumerate(ev['cumple']) if c is False]
        vals.append([etiqueta, ev['n']] + celdas + [V['cumplidos'].format(k=ev['k'], total=ev['total'])])
    return {'valores': vals, 'rojos': rojos, 'manuales': manuales, 'semanas': semanas, 'col_manual': col5,
            'n_cols': len(cab)}


def parsear_objetivos(valores, semanas, R):
    """Columna 5 de la pestaña leída -> {lunes ISO: texto} de las semanas que siguen en ella (spec §11.3), o None si no tiene
    el encabezado esperado (pestaña nueva o tocada a mano: no se toca SQLite). `semanas`: {etiqueta: lunes ISO} de
    `layout_objetivos`."""
    cab = cabecera_objetivos(R)
    if not valores or [str(c).strip() for c in valores[0][:len(cab)]] != cab:
        return None
    if R['objetivos']['manual']['columna'] not in cab:      # V5: sin columna manual
        return None
    i = cab.index(R['objetivos']['manual']['columna'])
    return {semanas[str(r[0]).strip()]: valor_manual(r[i] if len(r) > i else None)
            for r in valores[1:] if r and str(r[0]).strip() in semanas}


def _celda(r, idx):
    return r[idx] if idx is not None and len(r) > idx else ''


def parsear_hoja(valores):
    """Lee la pestaña existente -> (manual_ops {id: {...}}, thresholds {nombre: valor|None}, layout_ok).
    Las columnas se localizan por el NOMBRE del encabezado, no por posición. Tolera una pestaña
    vacía o sin el layout V4 (devuelve vacíos)."""
    manual, thr = {}, {}
    if not valores or not valores[0] or str(valores[0][0]).strip() != BAL_COLS[0]:
        return manual, thr, False
    cab = [str(c).strip() for c in valores[0]]
    i_thr = cab.index('Threshold Rithmic (manual)') if 'Threshold Rithmic (manual)' in cab else None
    i = 1
    while i < len(valores) and valores[i] and str(valores[i][0]).strip():
        r = valores[i]
        v = _celda(r, i_thr)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            n = float(v)
        else:
            n = fuentes.num(v) if isinstance(v, str) else None
        if i_thr is not None:
            thr[str(r[0]).strip()] = n
        i += 1
    h = next((j for j in range(i, len(valores))
              if valores[j] and str(valores[j][0]).strip() == OPS_COLS[0]), None)
    if h is None:
        return manual, thr, True
    cab = [str(c).strip() for c in valores[h]]
    pos = {k: (cab.index(col) if col in cab else None) for k, col in {**MANUAL_OPS, **FLOTANTES}.items()}
    for r in valores[h + 1:]:
        if not r or not str(r[0]).strip():
            continue
        manual[str(r[0]).strip()] = {k: _celda(r, idx) for k, idx in pos.items()}
    return manual, thr, True


def _preoperativa(valores, R):
    """(índice del encabezado de operaciones, de la fila 'Estrategia a usar', filas de compuerta bajo ella). La fila de la
    estrategia está encima del encabezado, con las filas de la compuerta (spec §10.6) entre las dos; en la pestaña anterior
    al 07/10/2026 estaba justo encima y sin compuerta (0 filas). None si no está."""
    P = R['preoperativa']
    h = next((j for j, r in enumerate(valores) if r and str(r[0]).strip() == OPS_COLS[0]), None)
    if not h:
        return None
    for n in (len(P['compuerta']['filas']), 0):
        j = h - 1 - n
        if j >= 0 and valores[j] and str(valores[j][0]).strip() == P['estrategia']['etiqueta']:
            return h, j, n
    return None


def leer_preoperativa(valores, R):
    """Valor de la celda 'Estrategia a usar' (spec §7.2): fila de la preoperativa con la etiqueta en A; la celda es la de
    la columna 'Estrategia' del encabezado de operaciones. None si no está."""
    pos = _preoperativa(valores, R)
    if pos is None:
        return None
    h, j, _ = pos
    cab = [str(c).strip() for c in valores[h]]
    col = cab.index(R['preoperativa']['estrategia']['columna']) if R['preoperativa']['estrategia']['columna'] in cab else None
    return str(_celda(valores[j], col)).strip() if col is not None else None


def valor_mercado(fila, R):
    """Valor de la fila `Estado del mercado` leída (columna A hasta la de Estrategia), o None si no es esa fila (pestaña
    anterior, fila movida a mano o texto cambiado). Un valor que no está en la lista (vacío) es el defecto (spec §12)."""
    M = R['preoperativa']['mercado']
    if not fila or str(fila[0]).strip() != M['etiqueta']:
        return None
    v = str(_celda(fila, OC[R['preoperativa']['estrategia']['columna']])).strip()
    return next((x for x in M['valores'] if x.lower() == v.lower()), M['defecto'])


def leer_mercado(valores, R):
    """Valor de `Estado del mercado` en la pestaña leída entera (spec §12): la fila justo encima de `Estrategia a usar`; None
    si no la tiene (pestaña anterior al 08/10/2026)."""
    pos = _preoperativa(valores, R)
    if pos is None or pos[1] < 1:
        return None
    return valor_mercado(valores[pos[1] - 1], R)


def leer_r17(valores, R):
    """Texto de R17 que hay en el sheet leído (spec §13): el de la celda de la derecha de la de la estrategia, si empieza por
    `preoperativa.r17.prefijo`. None si R17 está apagado, no hay fila de estrategia o la celda tiene otra cosa. El sync lo
    conserva cuando R17 aún no ha escrito nada en esta ejecución (arranque del demonio, `cli_v4.py sync`)."""
    C = (R.get('preoperativa') or {}).get('r17') or {}
    if not C.get('activo') or not C.get('prefijo'):
        return None
    pos = _preoperativa(valores, R)
    columna = R['preoperativa']['estrategia']['columna']
    if pos is None:
        return None
    h, j, _ = pos
    cab = [str(c).strip() for c in valores[h]]
    if columna not in cab:
        return None
    v = _celda(valores[j], cab.index(columna) + 1)
    return v if isinstance(v, str) and v.startswith(C['prefijo']) else None


def valores_compuerta(filas, R):
    """Filas leídas (la de la estrategia y las de la compuerta que la siguen; cada una de la columna A a la de Estrategia) ->
    valores normalizados de las filas de la compuerta (spec §10.6), o None si no están o su texto de la columna A no es el
    de `reglas.yaml` (pestaña anterior, filas movidas a mano o texto cambiado: esperar al siguiente sync)."""
    Q = R['preoperativa']['compuerta']
    if len(filas) < 1 + len(Q['filas']):
        return None
    col = OC[R['preoperativa']['estrategia']['columna']]
    comp = filas[1:1 + len(Q['filas'])]
    if any(not f or str(f[0]).strip() != texto for f, texto in zip(comp, Q['filas'])):
        return None
    return [str(_celda(f, col)).strip().upper() for f in comp]


def leer_compuerta(valores, R):
    """Valores de las filas de la compuerta en la pestaña leída entera (spec §10.6), o None si no las tiene."""
    pos = _preoperativa(valores, R)
    if pos is None or pos[2] == 0:
        return None
    _, j, n = pos
    return valores_compuerta([valores[k] if k < len(valores) else [] for k in range(j, j + 1 + n)], R)


def columnas_a_mostrar(valores):
    """Columnas que el demonio dejó ocultas con un layout anterior: la del OrderId leído, si ya no es la de ahora (con
    `Calificación`, 01/10/2026, el OrderId pasa de la AC a la AD y la AC es `Análisis LLM`). Solo esa: una columna que
    haya ocultado el operador no se toca."""
    h = next((j for j, r in enumerate(valores) if r and str(r[0]).strip() == OPS_COLS[0]), None)
    cab = [str(c).strip() for c in valores[h]] if h is not None else []
    i = cab.index('OrderId') if 'OrderId' in cab else None
    return [i] if i is not None and i != OC['OrderId'] else []


def celda_a1(fila, col):
    """(fila, columna) 0-based -> 'I11'."""
    s, i = '', col + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return f'{s}{fila + 1}'


def celda_de_a1(a1):
    """'I11' -> (10, 8), 0-based."""
    letras = ''.join(ch for ch in a1 if ch.isalpha())
    col = 0
    for ch in letras:
        col = col * 26 + ord(ch) - 64
    return int(a1[len(letras):]) - 1, col - 1


def guardar_manual(con, manual_hoja):
    """Paso 1 de la escritura (spec §1): columnas manuales del sheet -> SQLite, por ID.
    El sheet manda para los IDs presentes (incluido borrar); los ausentes se conservan."""
    for op_id, d in manual_hoja.items():
        # 'Análisis LLM' salió de la hoja con la V5: su columna de SQLite se conserva tal como estaba
        con.execute('INSERT INTO manual_ops (op_id, estrategia, observaciones, analisis_llm) VALUES (?, ?, ?, ?) '
                    'ON CONFLICT(op_id) DO UPDATE SET estrategia=excluded.estrategia, '
                    'observaciones=excluded.observaciones',
                    (op_id, str(d.get('estrategia') or ''), str(d.get('observaciones') or ''), ''))
    con.commit()


def parse_flotante(v):
    """Celda MAE/MFE -> float o None. Admite número o texto es_ES ('−161,5', '1.234,5', '+42')."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or '').strip().replace('−', '-').replace('\u00a0', '').replace(' ', '')
    if not s:
        return None
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def detectar_flotantes(con, manual_hoja):
    """MAE/MFE del sheet distintos de lo último que escribió el demonio -> manual (por ID de decisión).
    Celda vaciada -> vuelve al log. Sin registro de lo escrito (ID nunca escrito) no se detecta nada.
    Devuelve True si algo cambió."""
    cambio = False
    for op_id, d in manual_hoja.items():
        esc = con.execute('SELECT mae, mfe FROM escrito_ops WHERE op_id=?', (op_id,)).fetchone()
        if esc is None:
            continue
        for campo in ('mae', 'mfe'):
            if campo not in d:
                continue
            celda, escrito = parse_flotante(d[campo]), esc[campo]
            actual = con.execute(f'SELECT {campo}_manual v FROM manual_ops WHERE op_id=?', (op_id,)).fetchone()
            actual = actual['v'] if actual else None
            if celda is None:
                nuevo = None if escrito is not None else actual
            elif escrito is None or abs(celda - escrito) > TOL_FLOTANTE:
                nuevo = -abs(celda) if campo == 'mae' else abs(celda)
            else:
                nuevo = actual
            if nuevo != actual:
                con.execute(f'INSERT INTO manual_ops (op_id, {campo}_manual) VALUES (?, ?) '
                            f'ON CONFLICT(op_id) DO UPDATE SET {campo}_manual=excluded.{campo}_manual',
                            (op_id, nuevo))
                cambio = True
    con.commit()
    return cambio


def guardar_escrito(con, lay):
    """Tras escribir: MAE/MFE que quedaron en el sheet por ID (referencia para detectar ediciones)."""
    for r in lay['valores'][lay['ops_ini']:lay['ops_fin']]:
        mae, mfe = r[OC[FLOTANTES['mae']]], r[OC[FLOTANTES['mfe']]]
        con.execute('INSERT OR REPLACE INTO escrito_ops VALUES (?, ?, ?)',
                    (r[OC['ID']], None if mae == '' else mae, None if mfe == '' else mfe))
    con.commit()


def cargar_manual(con):
    return {r['op_id']: {'estrategia': r['estrategia'], 'observaciones': r['observaciones'],
                         'analisis_llm': r['analisis_llm']}
            for r in con.execute('SELECT * FROM manual_ops')}


# --- histórico unificado (spec §16.9) ---

def fila_unificado(f, ent, manual, tardias, origen):
    """Fila del histórico unificado de una decisión: las columnas de la V5 más `Origen`."""
    r = fila_op(f, ent, manual, ent.reglas['estrategia_por_defecto'], tardias)
    return r[:-1] + [origen, r[-1]]


def filas_unificado(filas, ent, manual, tardias, desde_utc=None, hasta_utc=None):
    """Filas del unificado de las decisiones (líderes) abiertas en (desde_utc, hasta_utc], lo más nuevo arriba. Origen: V5 o
    V4 según la sesión CME de la decisión (reglas.yaml -> v5.unificado.origen)."""
    from . import v5 as mv5
    R = ent.reglas
    O = R['v5']['unificado']['origen']
    decs = sorted((f for f in filas if not f.es_seguidora and (not desde_utc or f.t_apertura_utc > desde_utc)
                   and (not hasta_utc or f.t_apertura_utc <= hasta_utc)),
                  key=lambda f: (f.t_apertura_utc, f.op_id), reverse=True)
    return [fila_unificado(f, ent, manual, tardias, O['v5'] if mv5.es_v5(f.sesion, R) else O['v4']) for f in decs]


def filas_unificado_antiguas(valores, R):
    """Las decisiones de antes de la V4 (pestaña `Decisiones histórico` de la hoja antigua, leída con valores sin formato) ->
    filas del unificado, lo más nuevo arriba. Solo se rellenan las columnas de la V5 que entonces existían; el resto, vacías.
    Nº en el día, acumulado y ticks perdidos del día se calculan aquí con sus ticks (orden de apertura: no hay hora de cierre
    fiable para todas)."""
    if not valores:
        return []
    cab = [str(c).strip() for c in valores[0]]
    ix = {c: cab.index(c) for c in cab}

    def g(r, col):
        i = ix.get(col)
        return r[i] if i is not None and len(r) > i else ''

    origen = R['v5']['unificado']['origen']['antiguo']
    filas, por_dia = [], {}
    filas_validas = sorted((r for r in valores[1:] if r and str(r[0]).strip()),       # nº y acumulados del día, por apertura
                           key=lambda r: str(g(r, 'Apertura (Madrid)')).strip())
    for r in filas_validas:
        ticks = fuentes.num(g(r, 'Ticks'))
        ap = str(g(r, 'Apertura (Madrid)')).strip()
        try:
            serial = motor.serial_sheets(datetime.fromisoformat(ap))
        except ValueError:
            serial = ap
        row = [''] * len(UNIFICADO_COLS)
        u = {c: i for i, c in enumerate(UNIFICADO_COLS)}
        row[u['ID']] = str(r[0]).strip()
        row[u['Cuenta']] = g(r, 'Cuentas')
        row[u['Largo/Corto']] = {'largo': 'Largo', 'corto': 'Corto'}.get(str(g(r, 'Dirección')).strip().lower(), g(r, 'Dirección'))
        row[u['Tipo cierre']] = g(r, 'Tipo de cierre')
        row[u['Resultado (ticks)']] = '' if ticks is None else ticks
        row[u['Símbolo']] = g(r, 'Símbolo')
        row[u['Contratos']] = g(r, 'Contratos')
        row[u['Fecha y hora apertura']] = serial
        row[u['Duración (s)']] = fuentes.num(g(r, 'Duración (s)'), '')
        row[u['Origen']] = origen
        dia = por_dia.setdefault(str(g(r, 'Sesión')).strip(), [0, 0.0, 0.0])
        dia[0] += 1
        if ticks is not None:
            dia[1] += ticks
            dia[2] += -ticks if ticks < 0 else 0.0
        row[u['Nº en el día']] = dia[0]
        row[u['Acumulado del día (ticks)']] = round(dia[1], 2)
        row[u['Ticks perdidos del día']] = round(dia[2], 2)
        filas.append((ap, row))
    return [row for _, row in sorted(filas, key=lambda x: x[0], reverse=True)]
