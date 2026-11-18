"""Registro de cuentas (cuentas.yaml) y acciones del operador: quemar, retiro, sim, reset."""

import re

from . import almacen, reloj as mreloj, resets as mresets


def inferir(nombre, reglas):
    """Firma/tipo/tamaño por patrón del nombre (spec §3). None = SIN REGLA."""
    for p in reglas['patrones_cuenta']:
        if re.search(p['regex'], nombre):
            return {'firma': p['firma'], 'tipo': p['tipo'], 'tamano': p.get('tamano')}
    return None


def _tabla_firma(firma, reglas):
    """Tabla {tamaño: regla} de la firma, siguiendo los alias 'como'. None si no existe o si los alias dan una vuelta
    (V5.1: antes `tamano_por_balance` no tenía guarda y un ciclo A→B→A dejaba el demonio al 100 % de CPU sin log)."""
    f = reglas['firmas'].get(firma)
    visto = set()
    while isinstance(f, dict) and 'como' in f and f['como'] not in visto:
        visto.add(f['como'])
        f = reglas['firmas'].get(f['como'])
    return f if isinstance(f, dict) and 'como' not in f else None


def regla_firma(firma, tamano, reglas):
    """{'drawdown', 'tope'} de la firma y tamaño; sigue los alias 'como'. None si no hay regla."""
    f = _tabla_firma(firma, reglas)
    if f is None or tamano is None:
        return None
    return f.get(int(tamano))


def tamano_por_balance(firma, balance, reglas):
    """Tamaño nominal de la firma más cercano al balance (el nombre de Apex no lo lleva)."""
    f = _tabla_firma(firma, reglas)
    if f is None or balance is None:
        return None
    return min((int(k) for k in f), key=lambda s: abs(s - balance))


def _bal_inicial(ent, con, cuenta):
    """(hora cruda del log, balance) del que parte una cuenta: el último 'Bal:' plano (almacen.ultimo_bal_plano) o, en un reset
    (spec §14.1), el primero tras el corte: la cuenta nueva no hereda nada de la vida anterior."""
    if mresets.es_reset(ent.resets, cuenta):
        return almacen.primer_bal(con, cuenta)
    return almacen.ultimo_bal_plano(con, cuenta, reloj=mreloj.cargar(con, ent.reglas))


def _sesion_alta(ent, cuenta, sesion_actual):
    """Sesión CME del alta: la actual; en un reset, la del corte (la cuenta nace ahí aunque se registre días después, y
    su margen cuenta desde entonces, como si se hubiera dado de alta en vivo)."""
    corte = mresets.corte_de(ent.resets, cuenta)
    if corte is None:
        return sesion_actual
    from . import motor            # import tardío: motor importa este módulo
    return motor.sesion_cme(mresets.desde_utc(corte))


def registrar_nuevas(ent, con, sesion_actual, avisos):
    """Alta automática de cuentas vistas en las fuentes. Devuelve la lista de altas. Un reset (`<ID>-resetN`, spec §14.1) se
    da de alta igual: firma y tipo por el patrón del ID físico, tamaño por su primer balance, sin herencia de la cuenta anterior."""
    fichas = ent.cuentas['cuentas']
    queme = almacen.quemadas(con)
    altas = []
    for c in almacen.cuentas_vistas(con):
        if c in fichas or c in queme:
            continue
        if ent.es_sim(c) and not ent.sim_activa(c):
            continue
        _, bal = _bal_inicial(ent, con, c)
        inf = inferir(mresets.fisica_de(ent.resets, c), ent.reglas)
        ficha = {'firma': None, 'tipo': None, 'tamano': None, 'sin_regla': True,
                 'alta': _sesion_alta(ent, c, sesion_actual).isoformat(), 'retiros': 0,
                 'ancla_inicial': None, 'ancla_estimada': True}
        if inf:
            tam = inf['tamano'] or tamano_por_balance(inf['firma'], bal, ent.reglas)
            ficha.update(firma=inf['firma'], tipo=inf['tipo'], tamano=tam,
                         sin_regla=regla_firma(inf['firma'], tam, ent.reglas) is None)
        if ficha['sin_regla']:
            avisos.append(f"[AVISO] Cuenta nueva {c}: SIN REGLA de firma/tamaño.")
        fichas[c] = ficha
        altas.append(c)
    completadas = _completar_tamano(ent, con)
    if altas or completadas:
        ent.guardar_cuentas()
    return altas


def _completar_tamano(ent, con):
    """Apex/Bulenox dados de alta sin 'Bal:' plano todavía: se reintenta el tamaño en cada ingesta."""
    cambios = False
    for c, ficha in ent.cuentas['cuentas'].items():
        if ficha.get('tamano') is not None or not ficha.get('firma'):
            continue
        _, bal = _bal_inicial(ent, con, c)
        tam = tamano_por_balance(ficha['firma'], bal, ent.reglas)
        if tam is not None:
            ficha['tamano'] = tam
            ficha['sin_regla'] = regla_firma(ficha['firma'], tam, ent.reglas) is None
            cambios = True
    return cambios


def asegurar_base(ent, con, cuenta):
    """Balance de arranque (último Bal: plano del log) guardado una sola vez. Un 'Bal:' de transición (el que ATAS
    8/Apex escribe en el mismo ms justo antes de la vuelta a plano) no vale: almacen.bal_plano (orden de línea). En un reset
    (spec §14.1) es el primer 'Bal:' tras el corte (_bal_inicial)."""
    r = con.execute('SELECT bal_base FROM cuentas_estado WHERE cuenta=?', (cuenta,)).fetchone()
    if r:
        return True
    t, bal = _bal_inicial(ent, con, cuenta)
    if bal is None:
        return False
    con.execute('INSERT INTO cuentas_estado VALUES (?, ?, ?)', (cuenta, bal, t))
    con.commit()
    return True


def quemar(ent, con, cuenta, nota=''):
    if cuenta not in ent.cuentas['cuentas']:
        raise ValueError(f"Cuenta desconocida: {cuenta}")
    con.execute('INSERT OR REPLACE INTO cuentas_quemadas VALUES (?, ?, ?)',
                (cuenta, almacen.ahora_madrid(), nota))
    con.commit()


def retiro(ent, con, cuenta, importe):
    """Retiro de X $: baja el balance y la cuenta pasa a <nombre>_retiros_N (hereda margen, racha y ancla)."""
    ficha = ent.cuentas['cuentas'].get(cuenta)
    if ficha is None:
        raise ValueError(f"Cuenta desconocida: {cuenta}")
    if importe <= 0:
        raise ValueError("El importe del retiro debe ser positivo")
    con.execute('INSERT INTO movimientos (cuenta, t, importe, tipo, nota) VALUES (?, ?, ?, ?, ?)',
                (cuenta, almacen.ahora_madrid(), -abs(importe), 'retiro', ''))
    con.commit()
    ficha['retiros'] = int(ficha.get('retiros') or 0) + 1
    ent.guardar_cuentas()
    return ent.nombre_visible(cuenta)


def sim(ent, cuenta, activar):
    if not ent.es_sim(cuenta):
        raise ValueError(f"{cuenta} no es una cuenta sim")
    act = ent.cuentas.setdefault('sim_activas', [])
    if activar and cuenta not in act:
        act.append(cuenta)
    elif not activar and cuenta in act:
        act.remove(cuenta)
    ent.guardar_cuentas()


def reset(ent, con, fisica, desde):
    """Reset de la cuenta física `fisica` desde `desde` (spec §14, hora del log, Madrid): la cuenta lógica `<ID>-resetN` recibe lo
    que la cuenta tenga desde entonces. Registra el reset en cuentas.yaml (N automático) y reasigna lo ya ingerido
    (almacen.reasignar_resets, idempotente); no quema ni desquema nada. Con el demonio parado (como cualquier edición de
    cuentas.yaml). Devuelve (nombre del reset, {tabla: filas reasignadas}, ID de operación que cambian)."""
    from . import motor            # import tardío: motor importa este módulo
    if mresets.es_reset(ent.resets, fisica):
        raise ValueError(f"{fisica} es el reset de {mresets.fisica_de(ent.resets, fisica)}: indica la cuenta física")
    if ent.ficha(fisica) is None:
        raise ValueError(f"Cuenta desconocida: {fisica}")
    if ent.es_sim(fisica):
        raise ValueError(f"{fisica} es una cuenta sim: se ignora, no tiene resets")
    desde = mresets.normalizar_desde(desde)
    previos = mresets.cortes(ent.resets, fisica)
    if previos and desde <= previos[-1][0]:
        raise ValueError(f"El reset de {fisica} ha de ser posterior al anterior ({previos[-1][1]}, desde {previos[-1][0]})")
    nombre = mresets.nombre_siguiente(ent.resets, fisica, ent.reglas['reset']['sufijo'])
    if nombre in ent.cuentas['cuentas']:
        raise ValueError(f"Ya existe una cuenta llamada {nombre}")
    if motor.cuenta_corta_por_patron(nombre, ent.reglas) is None:
        raise ValueError(f"reglas.yaml -> cuenta_corta no tiene patrón para {nombre}: sin él el ID corto sería RESET1 en todas "
                         f"las cuentas y los ID de operación chocarían. Añádelo antes.")
    mresets.anotar(ent.cuentas.setdefault('resets', {}), fisica, desde, nombre)
    ent.guardar_cuentas()           # primero el registro: si se corta aquí, el siguiente sync reasigna (huella en estado)
    movidas = almacen.reasignar_resets(con, ent.resets)
    return nombre, movidas, almacen.operaciones_con_id_antiguo(con, ent.resets, fisica)
