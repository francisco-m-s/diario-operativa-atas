"""Copia por simultaneidad (spec §4.1, redefinida por el operador el 01/10/2026).

Una operación es copia de la de su líder si está en otra cuenta (no sim), con la misma dirección e instrumento y la
apertura a ≤ `copia.ventana_s` (1,0 s) de la de la líder, figure o no la relación en CopyTrading.cnf. Las copias cuentan
solo para el BAL: no son decisión (estrategia, Calificación, cortafuegos, filas del sheet).

Caso real: a las 15:40 del 01/10 el operador cambió la seguidora de E2T-250 de APEX-82 a APEX-83; la operación de
AP82 de las 15:37:47 (misma apertura, dirección e instrumento que E2T250-20261001-001) pasó a decisión propia y salió un
`4 OPS EN 1 H` con solo 3 decisiones y un `SIN SL` en AP82. Fixture: fixtures/ATAS_20261001, cuyo CopyTrading.cnf ya es
el nuevo (E2T-250 → APEX-83). El resto, filas sintéticas mínimas.
"""

import os
import shutil
from datetime import datetime, timedelta

import pytest

from atasv4 import almacen, config, cortafuegos, motor, sync, vista
from conftest import RAIZ, HojaFalsa

AQUI = os.path.dirname(os.path.abspath(__file__))
FIXT_8015 = os.path.join(AQUI, 'fixtures', 'ATAS_20261001')
R = config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'), {})
NQ, MNQ = 'NQZ6@CME', 'MNQZ6@CME'
E250, AP82, AP83 = 'fsern2776tcp25d152250', 'APEX-91120-82', 'APEX-91120-83'
LOG = 'app_20261001.log'
AHORA = '2026-10-01 13:44:00.000'                                  # 15:44 Madrid, reloj del PC


def _fila(cuenta, ms, direccion=1, simbolo=NQ, linea=None, solo_mytrade=False, sim=False, seg=0):
    """Fila con un solo fill de entrada abierto el 01/10 a las 13:37:47 UTC + seg s + ms (hora del exchange con ms). solo_mytrade:
    sin hora del exchange ni del log, solo el segundo de MyTrade (log rotado). linea: (fichero, posición) de su `Order
    trade` en el log."""
    t = datetime(2026, 10, 1, 13, 37, 47) + timedelta(seconds=seg, milliseconds=ms)
    t_exch = t.strftime('%Y-%m-%d %H:%M:%S.') + f'{ms:03d}'
    e = {'cuenta': cuenta, 'simbolo': simbolo, 'fill_id': f'{cuenta}{ms}{seg}', 'order_id': 'o', 'lado': 'Buy',
         'precio': 30000.0, 'volumen': 1.0, 't_utc': t_exch[:19] + '.000', 't_exch': None if solo_mytrade else t_exch,
         't_log': None, 'fichero': linea[0] if linea else None, 'pos': linea[1] if linea else None, 't_c': None}
    f = motor.Fila(cuenta=cuenta, simbolo=simbolo, direccion=direccion, order_id='o')
    f.entradas = [e]
    f.sim = sim
    return f


def _emparejar(filas, pares=()):
    motor.emparejar_copias(list(filas), list(pares), R)


# ===========================================================================
# Qué es copia
# ===========================================================================

def test_copia_sin_relacion_en_la_configuracion():
    """Dos cuentas sin ningún par en CopyTrading.cnf: la que abre después, 0,4 s, es copia de la primera."""
    a, b = _fila('A', 0), _fila('B', 400)
    _emparejar([a, b])
    assert b.lider is a and a.copias == [b] and a.lider is None
    assert b.es_seguidora and not a.es_seguidora


def test_configuracion_cambiada_despues_no_reescribe_el_pasado():
    """La operación de la cuenta que dejó de ser seguidora sigue siendo copia: el cambio de configuración (E2T-250 de
    APEX-82 a APEX-83) no la convierte en decisión propia."""
    for pares in ([(E250, AP82)], [(E250, AP83)], []):
        lider, copia = _fila(E250, 0), _fila(AP82, 300)
        _emparejar([lider, copia], pares)
        assert copia.lider is lider and lider.copias == [copia], pares


def test_la_seguidora_puede_abrir_antes_que_la_lider():
    """Con el CopyTrading.cnf actual manda la cuenta que figura como líder, aunque su operación sea 0,3 s posterior."""
    copia, lider = _fila(AP82, 0), _fila(E250, 300)
    _emparejar([copia, lider], [(E250, AP83)])
    assert copia.lider is lider and lider.lider is None


def test_ventana_limite_1_0_dentro_1_01_fuera():
    assert R['copia']['ventana_s'] == 1.0
    a, dentro = _fila('A', 0), _fila('B', 0, seg=1)                 # exactamente 1,000 s
    _emparejar([a, dentro])
    assert dentro.lider is a
    a, fuera = _fila('A', 0), _fila('B', 10, seg=1)                 # 1,010 s
    _emparejar([a, fuera])
    assert fuera.lider is None and a.copias == [] and a.lider is None
    # también en el otro sentido: la que abre antes es la copia de la que figura como líder
    a, antes = _fila('A', 10, seg=1), _fila('B', 0)
    _emparejar([a, antes], [('A', 'X')])
    assert antes.lider is None
    a, antes = _fila('A', 0, seg=1), _fila('B', 0)
    _emparejar([a, antes], [('A', 'X')])
    assert antes.lider is a


def test_ventana_con_solo_el_segundo_de_mytrade():
    """Las dos solo en MyTrade (resolución de 1 s): 1 s de diferencia entra y 2 s no (el mismo reloj en las dos)."""
    a, b = _fila('A', 0, solo_mytrade=True), _fila('B', 0, solo_mytrade=True, seg=1)
    _emparejar([a, b])
    assert b.lider is a
    a, c = _fila('A', 0, solo_mytrade=True), _fila('C', 0, solo_mytrade=True, seg=2)
    _emparejar([a, c])
    assert c.lider is None


def test_lo_que_no_es_copia():
    """Dirección opuesta, otro instrumento, la misma cuenta y una cuenta sim no son copia."""
    largo = _fila('A', 0)
    corto = _fila('B', 100, direccion=-1)
    otro = _fila('C', 100, simbolo=MNQ)
    mismo = _fila('A', 200)                                          # otra entrada de la misma cuenta
    sim = _fila('S', 100, sim=True)
    _emparejar([largo, corto, otro, mismo, sim])
    assert all(f.lider is None and not f.copias for f in (largo, corto, otro, mismo, sim))
    # una sim tampoco es líder aunque su operación sea la más temprana, ni aparece en la configuración
    sim, real = _fila('S', 0, sim=True), _fila('B', 100)
    _emparejar([sim, real], [('S', 'B')])
    assert real.lider is None and not sim.copias


def test_sentidos_opuestos_se_emparejan_cada_uno_con_los_suyos():
    """Un largo y un corto casi a la vez en las dos cuentas: cada uno con su homólogo, nunca cruzados."""
    al, ac, bl, bc = _fila('A', 0), _fila('A', 50, direccion=-1), _fila('B', 100), _fila('B', 150, direccion=-1)
    _emparejar([al, ac, bl, bc])
    assert bl.lider is al and bc.lider is ac and al.lider is None and ac.lider is None


# ===========================================================================
# Qué cuenta es la líder
# ===========================================================================

def test_lider_por_configuracion_frente_a_lider_por_apertura_mas_temprana():
    x, y = _fila('X', 0), _fila('Y', 500)
    _emparejar([x, y], [('Y', 'Z')])                                 # Y figura como líder (de otra cuenta): manda
    assert x.lider is y and y.lider is None
    x, y = _fila('X', 0), _fila('Y', 500)
    _emparejar([x, y], [('X', 'Z')])
    assert y.lider is x and x.lider is None
    x, y = _fila('X', 0), _fila('Y', 500)
    _emparejar([x, y])                                               # ninguna figura: la más temprana
    assert y.lider is x and x.lider is None
    x, y = _fila('X', 500), _fila('Y', 0)
    _emparejar([x, y])
    assert x.lider is y


def test_varias_cuentas_que_figuran_como_lider_gana_la_mas_temprana():
    x, y = _fila('X', 400), _fila('Y', 0)
    _emparejar([x, y], [('X', 'Z'), ('Y', 'W')])
    assert x.lider is y


def test_empate_por_orden_de_linea_y_despues_por_nombre():
    """Misma hora (ms del exchange, o el mismo segundo de MyTrade): la primera línea del log; sin línea, el nombre."""
    for kw in ({}, {'solo_mytrade': True}):
        b = _fila('B', 0, linea=(LOG, 100), **kw)
        a = _fila('A', 0, linea=(LOG, 200), **kw)
        _emparejar([a, b])
        assert a.lider is b and b.lider is None, kw                  # B va antes en el log, aunque se llame B
        a, b = _fila('A', 0, **kw), _fila('B', 0, **kw)
        _emparejar([b, a])                                          # el orden de entrada no cuenta
        assert b.lider is a and a.lider is None, kw
        a, b = _fila('A', 0, linea=(LOG, 200), **kw), _fila('B', 0, **kw)       # solo una tiene línea: nombre
        _emparejar([a, b])
        assert b.lider is a, kw


def test_empate_con_mytrade_cuenta_el_mismo_segundo_aunque_los_ms_difieran():
    """Con solo el segundo de MyTrade, 47,100 y 47,900 empatan: gana la línea del log, no el ms (que no se conoce)."""
    a, b = _fila('A', 100, solo_mytrade=True, linea=(LOG, 900)), _fila('B', 900, solo_mytrade=True, linea=(LOG, 100))
    _emparejar([a, b])
    assert a.lider is b


def test_todas_las_seguidoras_se_miden_contra_la_lider_no_en_cadena():
    a, b, c = _fila('A', 0), _fila('B', 800), _fila('C', 600, seg=1)          # A 0,0 · B 0,8 · C 1,6
    _emparejar([a, b, c])
    assert b.lider is a and c.lider is None and a.copias == [b] and not c.copias       # C está a 0,8 de B, a 1,6 de A
    a, b, c = _fila('A', 0), _fila('B', 800), _fila('C', 600, seg=1)
    _emparejar([a, b, c], [('B', 'X')])                              # con B líder, A y C están a 0,8 s: las dos copian
    assert a.lider is b and c.lider is b and sorted(f.cuenta for f in b.copias) == ['A', 'C']


def test_lider_con_varias_seguidoras():
    """APEX-78 con 79 y 80 (como en CopyTrading.cnf) y una cuarta cuenta sin relación: todas copias de la misma líder."""
    l = _fila('APEX-91120-78', 0)
    s = [_fila(c, 100 * i) for i, c in enumerate(('APEX-91120-79', 'APEX-91120-80', 'APEX-91120-81'), 1)]
    _emparejar([s[2], l, s[1], s[0]], [('APEX-91120-78', 'APEX-91120-79'), ('APEX-91120-78', 'APEX-91120-80')])
    assert [c.cuenta for c in l.copias] == ['APEX-91120-79', 'APEX-91120-80', 'APEX-91120-81']
    assert all(c.lider is l for c in s)


def test_la_seguidora_hereda_el_sl_de_la_lider():
    lider, copia = _fila('A', 0), _fila('B', 100)
    lider.sl_ini, lider.sl_max, lider.sin_sl = 25.0, 30.0, False
    copia.sin_sl = True                                               # sin SL propio: ATAS copia posiciones, no órdenes
    _emparejar([lider, copia])
    assert (copia.sl_ini, copia.sl_max, copia.sin_sl) == (25.0, 30.0, False)


# ===========================================================================
# Entradas escalonadas y giros: cada fila con su homóloga
# ===========================================================================

def test_escalonada_cada_entrada_con_su_homologa():
    a1, a2 = _fila('A', 0), _fila('A', 0, seg=30)
    b1, b2 = _fila('B', 200), _fila('B', 300, seg=30)
    _emparejar([b2, a2, b1, a1])
    assert b1.lider is a1 and b2.lider is a2 and a1.copias == [b1] and a2.copias == [b2]


def test_escalonada_con_dos_entradas_de_la_seguidora_dentro_de_la_ventana():
    """A1 0,00 y A2 0,95; B1 0,40 y B2 0,90, con A como líder de la configuración: A1 se queda con la más cercana (B1) y
    A2 con la que queda (B2). Sin configuración manda la apertura más temprana: B2 (0,90) abre antes que A2 (0,95)."""
    a1, a2 = _fila('A', 0), _fila('A', 950)
    b1, b2 = _fila('B', 400), _fila('B', 900)
    _emparejar([a1, a2, b2, b1], [('A', 'B')])                       # b2 primero en la entrada: cuenta la cercanía
    assert b1.lider is a1 and b2.lider is a2
    a1, a2 = _fila('A', 0), _fila('A', 950)
    b1, b2 = _fila('B', 400), _fila('B', 900)
    _emparejar([a1, a2, b1, b2])
    assert b1.lider is a1 and a2.lider is b2


def test_una_entrada_de_la_seguidora_solo_tiene_una_lider():
    """Dos entradas de A a 0,3 s de la única de B: B copia a la primera; la segunda de A no se la queda también."""
    a1, a2 = _fila('A', 0), _fila('A', 500)
    b1 = _fila('B', 300)
    _emparejar([a1, a2, b1], [('A', 'B')])
    assert b1.lider is a1 and a1.copias == [b1] and a2.copias == [] and a2.lider is None


def test_la_ventana_cruza_el_cambio_de_minuto():
    lider, copia = _fila('A', 800, seg=12), _fila('B', 300, seg=13)       # 13:37:59,800 y 13:38:00,300
    _emparejar([lider, copia])
    assert copia.lider is lider


def test_una_entrada_de_mas_en_la_seguidora_es_decision_propia():
    a1 = _fila('A', 0)
    b1, b2 = _fila('B', 100), _fila('B', 0, seg=30)
    _emparejar([a1, b1, b2])
    assert b1.lider is a1 and b2.lider is None


def test_giro_cierra_y_abre_cada_fila_con_la_suya():
    """Un giro son dos operaciones (largo y corto) en cada cuenta; cada una con su homóloga."""
    al, ac = _fila('A', 0, direccion=1), _fila('A', 0, direccion=-1, seg=60)
    bl, bc = _fila('B', 300, direccion=1), _fila('B', 250, direccion=-1, seg=60)
    _emparejar([al, ac, bl, bc], [('A', 'B')])
    assert bl.lider is al and bc.lider is ac


# ===========================================================================
# Textos de los preventivos
# ===========================================================================

def test_textos_de_los_preventivos():
    v = {x['minutos']: x for x in R['cortafuegos']['ventanas']}
    assert v[10]['preventivo'] == '2 DECISIONES EN 10 MIN: NO OPERAR HASTA {hora}'
    assert v[60]['preventivo'] == '4 DECISIONES EN 1 H: NO OPERAR HASTA {hora}'
    assert (v[10]['infraccion'], v[60]['infraccion']) == ('SOBREOPERACIÓN 10 MIN', 'SOBREOPERACIÓN 1 H')   # no cambian
    base = datetime(2026, 10, 1, 15, 0, 0)
    decs = [cortafuegos.Decision(f'd{i}', None, base + timedelta(minutes=m), base + timedelta(minutes=m, seconds=30), 0.0)
            for i, m in enumerate((0, 5, 20, 40))]
    ev = cortafuegos.evaluar(decs, R)
    etq = lambda k: [t.format(hora=h.strftime('%H:%M:%S')) for t, h in ev[k]['preventivos']]       # noqa: E731
    assert etq('d1') == ['2 DECISIONES EN 10 MIN: NO OPERAR HASTA 15:10:00']
    assert etq('d3') == ['4 DECISIONES EN 1 H: NO OPERAR HASTA 16:00:00']
    assert not any('OPS EN' in t for k in ev for t in etq(k))


# ===========================================================================
# Caso real del 01/10/2026: AP82 + E2T250
# ===========================================================================

def _entorno_real(tmp_path):
    base = str(tmp_path / 'base')
    os.makedirs(base, exist_ok=True)
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), os.path.join(base, 'reglas.yaml'))
    cfg = config.cargar_yaml(os.path.join(RAIZ, 'config.yaml'), {})
    cfg.update({'atas_dir': FIXT_8015, 'sqlite': os.path.join(base, 't.sqlite'), 'backups_dir': os.path.join(base, 'b'),
                'inicio_sesion': '2026-10-01', 'quemadas_iniciales': [], 'logs_respaldo': None})
    config.guardar_yaml(os.path.join(base, 'config.yaml'), cfg)
    ficha = lambda firma, tipo: {'firma': firma, 'tipo': tipo, 'tamano': 25000, 'sin_regla': False,       # noqa: E731
                                 'alta': '2026-10-01', 'retiros': 0, 'ancla_inicial': None, 'ancla_estimada': True}
    config.guardar_yaml(os.path.join(base, 'cuentas.yaml'), {'cuentas': {
        E250: ficha('E2T', 'TCP'), AP82: ficha('Apex', 'evaluacion')}, 'sim_activas': []})
    ent = config.cargar_entorno(base)
    con = almacen.conectar(ent.sqlite_path)
    s = sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)
    s.ingerir()
    return ent, con, s


def test_caso_real_ap82_y_e2t250_una_sola_decision(tmp_path):
    """El CopyTrading.cnf del fixture ya dice E2T-250 → APEX-83: APEX-82 no figura. Misma apertura (13:37:47), corto de
    1 NQ en las dos: AP82 es copia de E2T250, hereda su SL (APEX-82 no puso ninguno) y no hay decisión propia ni `SIN SL`."""
    ent, con, s = _entorno_real(tmp_path)
    assert (E250, AP83) in s.pares_copia() and not any(AP82 in p for p in s.pares_copia())
    res = s.calcular(AHORA)
    f = {x.op_id: x for x in res.filas}
    e, a = f['E2T250-20261001-001'], f['AP82-20261001-001']
    assert a.lider is e and e.copias == [a] and e.lider is None and a.decision_id == e.op_id
    assert e.direccion == a.direccion == -1 and e.simbolo == a.simbolo
    assert [d.fila.op_id for d in cortafuegos.decisiones(res.filas, res.filas_abiertas)] == ['E2T250-20261001-001']
    assert not e.sin_sl and not a.sin_sl and (a.sl_ini, a.sl_max) == (e.sl_ini, e.sl_max) == (999, 999)
    assert ent.reglas['observaciones']['sin_sl'] not in a.obs
    # cuenta para el BAL: su balance, su margen y su operación
    bal = {b['cuenta']: b for b in res.bal}
    assert bal[AP82]['n_ops'] == bal[E250]['n_ops'] == 1
    assert bal[AP82]['margen'] == pytest.approx(ent.reglas['margen']['inicial'] + a.consumo)
    assert a.margen_tras == pytest.approx(bal[AP82]['margen'])


def test_cuenta_sim_activada_no_es_copia_en_el_calculo_completo(tmp_path):
    """Una sim activada para pruebas que abre a la vez que E2T250 (mismo corto de 1 NQ) no es su copia ni cambia la de
    APEX-82. Con la `Fila.sim` que pone `calcular` desde `Entorno.es_sim`."""
    ent, con, s = _entorno_real(tmp_path)
    sim = 'fsern2776_sim'
    ent.cuentas['cuentas'][sim] = {'firma': 'SIM', 'tipo': 'sim', 'tamano': 25000, 'sin_regla': False,
                                   'alta': '2026-10-01', 'retiros': 0, 'ancla_inicial': None, 'ancla_estimada': True}
    ent.cuentas['sim_activas'] = [sim]
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (sim, NQ, '9001', 'S1', 'Sell', 30821.25, 1, '2026-10-01 13:37:47.000'))
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (sim, NQ, '9002', 'S2', 'Buy', 30826.25, 1, '2026-10-01 13:39:15.000'))
    con.commit()
    res = s.calcular(AHORA)
    por = {f.cuenta: f for f in res.filas}
    assert sim in por and por[sim].sim and por[sim].lider is None and not por[sim].copias
    assert por[AP82].lider is por[E250] and por[E250].copias == [por[AP82]]


def test_caso_real_sin_cuarta_apertura(tmp_path):
    """A las 16:15 de Madrid, con la pareja de las 15:37 y otras dos decisiones en la hora, hay 3 decisiones y no se
    llega a `4 DECISIONES EN 1 H`. Contando AP82 como decisión propia (como antes del cambio) salían 4."""
    ent, con, s = _entorno_real(tmp_path)
    res = s.calcular(AHORA)
    reales = cortafuegos.decisiones(res.filas, res.filas_abiertas)
    assert len(reales) == 1
    ap = reales[0].apertura
    extra = [cortafuegos.Decision(f'x{i}', None, ap + timedelta(minutes=m), ap + timedelta(minutes=m, seconds=40), 0.0)
             for i, m in enumerate((30, 38))]
    ev = cortafuegos.evaluar(reales + extra, ent.reglas)
    todo = [t for e in ev.values() for t, _ in e['preventivos']] + [i['tipo'] for e in ev.values() for i in e['infracciones']]
    assert not [t for t in todo if '1 H' in t]
    propia = cortafuegos.Decision('AP82', None, ap, reales[0].cierre, reales[0].neto)      # el fallo: AP82 como decisión
    ev = cortafuegos.evaluar(sorted(reales + [propia] + extra, key=lambda d: (d.apertura, d.clave)), ent.reglas)
    assert [t for t, _ in ev['x1']['preventivos'] if '1 H' in t] == ['4 DECISIONES EN 1 H: NO OPERAR HASTA {hora}']


def test_caso_real_el_sync_escribe_una_fila_con_la_copia(tmp_path):
    """Sheet: una fila (la de E2T250) con APEX-82 en `Copias`; SQLite: las dos operaciones, la de AP82 con su líder; sin
    `SIN SL` en `cortafuegos_saltados` ni aviso en vivo propio de AP82; Calificación y estrategia, las de la decisión."""
    ent, con, s = _entorno_real(tmp_path)
    res, lay = s.sync(AHORA)
    assert lay['ops_fin'] - lay['ops_ini'] == 1
    fila = lay['valores'][lay['ops_ini']]
    assert fila[vista.OC['ID']] == 'E2T250-20261001-001' and fila[vista.OC['Copias']] == AP82
    ops = {r['op_id']: r for r in con.execute('SELECT * FROM operaciones')}
    assert set(ops) == {'E2T250-20261001-001', 'AP82-20261001-001'}
    assert ops['AP82-20261001-001']['lider_id'] == 'E2T250-20261001-001' and ops['E2T250-20261001-001']['lider_id'] is None
    assert not [r for r in con.execute('SELECT * FROM cortafuegos_saltados') if r['cuenta'] == AP82]
    s.vigilar(lambda titulo, texto: None, AHORA)
    assert not [r for r in con.execute('SELECT clave FROM avisos_emitidos') if AP82 in r['clave']]


def test_caso_real_calificacion_y_estrategia_de_la_decision(tmp_path):
    """La seguidora usa la estrategia de su líder: con estrategia elegida, las dos salen con el proceso correcto; sin ella
    (`Sin estrategia`), las dos incorrecto. Ni la copia ni su SL heredado la hacen distinta de la decisión."""
    ent, con, s = _entorno_real(tmp_path)
    res = s.calcular(AHORA)
    e = next(x for x in res.filas if x.op_id == 'E2T250-20261001-001')
    a = next(x for x in res.filas if x.op_id == 'AP82-20261001-001')
    motor.calificar(res.filas, res.cortafuegos, {e.op_id: {'estrategia': 'Algo'}}, set(), ent.reglas)
    assert (e.proceso_ok, a.proceso_ok) == (True, True) and e.calificacion == a.calificacion == 'Buen Trade'
    motor.calificar(res.filas, res.cortafuegos, {}, set(), ent.reglas)
    assert (e.proceso_ok, a.proceso_ok) == (False, False) and e.calificacion == a.calificacion == 'Trade Basura'
