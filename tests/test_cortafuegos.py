"""Cortafuegos (spec §4.5) con la sesión REAL del 22/09/2026 (fixtures/ATAS_hoy: 9 decisiones).

Nada del motor se simula: los únicos dobles son el cliente de Sheets (HojaFalsa, ya en conftest) y
la notificación del sistema (`avisar`), que aquí solo recoge los textos.
"""

from datetime import datetime, timedelta

import pytest

from atasv4 import aviso, cortafuegos, motor, sheets, vista
from conftest import filas_por

AHORA = '2026-09-22 18:00:00.000'            # 20:00 Madrid: sesión del 22/09 ya terminada
PA16, PA15, E250 = 'PA-APEX-91120-16', 'PA-APEX-91120-15', 'fsern2776tcp25d152250'
SIM = 'fsern2776_sim'
SEP = ' · '


def _md(txt):
    """'19:11:10' -> datetime del 22/09/2026 (hora de Madrid)."""
    return datetime.strptime('2026-09-22 ' + txt, '%Y-%m-%d %H:%M:%S')


def _utc(txt):
    """Hora de Madrid del 22/09 -> texto UTC, el formato que usan calcular()/vigilar()."""
    from atasv4 import fuentes
    return fuentes.fmt_ts(fuentes.madrid_a_utc(_md(txt)))


def _dec(clave, apertura, cierre=None, neto=None):
    return cortafuegos.Decision(clave=clave, fila=None, apertura=_md(apertura),
                                cierre=_md(cierre) if cierre else None, neto=neto)


def _etiquetas(e):
    """Evaluación de una decisión -> etiquetas tal y como salen en Observaciones demonio."""
    return ([i['tipo'] for i in e['infracciones']] +
            [t.format(hora=h.strftime('%H:%M:%S')) for t, h in e['preventivos']])


def _por_op(res):
    return {f.op_id: f for f in res.filas}


# ===========================================================================
# Reglas puras: ventanas móviles y espera tras pérdida (reglas.yaml -> cortafuegos)
# ===========================================================================

def test_perdida_preventivo_y_reentrada(entorno_hoy):
    R = entorno_hoy[0].reglas
    perdida = _dec('p', '10:00:00', '10:05:00', -10.0)             # ≤ −10 $ exactos: es pérdida
    ev = cortafuegos.evaluar([perdida, _dec('d', '10:15:11')], R)
    assert _etiquetas(ev['p']) == ['PÉRDIDA: NO OPERAR HASTA 10:15:12']   # cierre + 612 s
    assert _etiquetas(ev['d']) == ['REENTRADA TRAS PÉRDIDA']              # 1 s antes de la hora
    ev = cortafuegos.evaluar([perdida, _dec('d', '10:15:12')], R)         # justo a la hora: permitido
    assert _etiquetas(ev['d']) == []


def test_perdida_neutra_no_dispara(entorno_hoy):
    R = entorno_hoy[0].reglas
    ev = cortafuegos.evaluar([_dec('p', '10:00:00', '10:05:00', -9.99), _dec('d', '10:06:00')], R)
    assert _etiquetas(ev['p']) == [] and _etiquetas(ev['d']) == []


def test_ventana_10_min_preventivo_y_tercera_apertura(entorno_hoy):
    R = entorno_hoy[0].reglas
    a = _dec('a', '10:00:00', '10:01:00', 0.0)
    b = _dec('b', '10:05:00', '10:06:00', 0.0)
    # al cerrar b hay 2 aperturas en los últimos 10 min (10:00 y 10:05) -> la más antigua + 10 min
    assert _etiquetas(cortafuegos.evaluar([a, b], R)['b']) == ['2 DECISIONES EN 10 MIN: NO OPERAR HASTA 10:10:00']
    # la 3ª apertura dentro de la ventana es infracción; a las 10:10:00 ya sale de ella
    ev = cortafuegos.evaluar([a, b, _dec('c', '10:09:59')], R)
    assert _etiquetas(ev['c']) == ['SOBREOPERACIÓN 10 MIN']
    assert ev['c']['infracciones'][0]['hora_permitida'] == _md('10:10:00')
    assert ev['c']['infracciones'][0]['detalle'] == '3ª apertura en 10 min'
    assert _etiquetas(cortafuegos.evaluar([a, b, _dec('c', '10:10:00')], R)['c']) == []


def test_ventana_60_min_preventivo_y_quinta_apertura(entorno_hoy):
    R = entorno_hoy[0].reglas
    base = [_dec(str(i), t, '10:59:00', 0.0) for i, t in
            enumerate(('10:00:00', '10:10:00', '10:20:00', '10:30:00'))]
    ev = cortafuegos.evaluar(base + [_dec('q', '10:59:59')], R)
    assert 'SOBREOPERACIÓN 1 H' in _etiquetas(ev['q'])
    assert ev['q']['infracciones'][0]['hora_permitida'] == _md('11:00:00')   # 1ª de las 4 + 60 min
    assert _etiquetas(cortafuegos.evaluar(base + [_dec('q', '11:00:00')], R)['q']) == []
    # con solo 3 aperturas previas no hay nada
    assert _etiquetas(cortafuegos.evaluar(base[:3] + [_dec('q', '10:40:00')], R)['q']) == []


def test_varios_a_la_vez_se_muestran_todos(entorno_hoy):
    R = entorno_hoy[0].reglas
    decs = [_dec('a', '10:00:00', '10:01:00', 0.0), _dec('b', '10:05:00', '10:06:00', 0.0),
            _dec('c', '10:08:00', '10:09:00', -50.0)]
    et = _etiquetas(cortafuegos.evaluar(decs, R)['c'])
    assert et == ['SOBREOPERACIÓN 10 MIN',
                  'PÉRDIDA: NO OPERAR HASTA 10:19:12',
                  '2 DECISIONES EN 10 MIN: NO OPERAR HASTA 10:15:00']


def test_sin_sl_a_tiempo_bordes(entorno_hoy):
    R = entorno_hoy[0].reglas                       # cortafuegos.sin_sl_s = 5
    t0, fin = '2026-09-22 19:11:09.547', '2026-09-22 19:20:18.880'
    assert motor.sin_sl_a_tiempo(t0, fin, [], R) is True
    assert motor.sin_sl_a_tiempo(t0, fin, ['2026-09-22 19:11:14.547'], R) is False   # justo a los 5 s
    assert motor.sin_sl_a_tiempo(t0, fin, ['2026-09-22 19:11:14.548'], R) is True    # 1 ms tarde
    assert motor.sin_sl_a_tiempo(t0, fin, ['2026-09-22 19:11:09.546'], R) is True    # anterior al fill
    # posición cerrada antes de que venzan los 5 s: no se juzga
    assert motor.sin_sl_a_tiempo(t0, '2026-09-22 19:11:14.547', [], R) is False


# ===========================================================================
# Sesión real del 22/09/2026: qué cortafuegos salta en cada decisión
# ===========================================================================

CORTAFUEGOS_REALES = {
    'E2T250-20260922-001': [],
    'PA16-20260922-001': [],
    'PA16-20260922-002': [],
    'PA16-20260922-003': ['PÉRDIDA: NO OPERAR HASTA 17:46:28',
                          '2 DECISIONES EN 10 MIN: NO OPERAR HASTA 17:40:01',
                          '4 DECISIONES EN 1 H: NO OPERAR HASTA 17:59:20'],
    'PA16-20260922-004': ['REENTRADA TRAS PÉRDIDA', 'SOBREOPERACIÓN 10 MIN', 'SOBREOPERACIÓN 1 H',
                          '4 DECISIONES EN 1 H: NO OPERAR HASTA 18:02:12'],
    'PA16-20260922-005': ['PÉRDIDA: NO OPERAR HASTA 18:28:59',
                          '4 DECISIONES EN 1 H: NO OPERAR HASTA 18:30:01'],
    'PA16-20260922-006': ['PÉRDIDA: NO OPERAR HASTA 18:48:21'],
    'E2T250-20260922-002': ['REENTRADA TRAS PÉRDIDA', '2 DECISIONES EN 10 MIN: NO OPERAR HASTA 18:45:33'],
    'PA16-20260922-007': ['PÉRDIDA: NO OPERAR HASTA 19:30:31'],
}


def test_sesion_real_nueve_decisiones(entorno_hoy):
    ent, con, s = entorno_hoy
    res = s.calcular(AHORA)
    decs = cortafuegos.decisiones(res.filas, res.filas_abiertas)
    assert [d.fila.op_id for d in decs] == list(CORTAFUEGOS_REALES)      # orden por hora de apertura
    assert not res.filas_abiertas and len(res.filas) == 16               # 9 decisiones + 7 copias


def test_sesion_real_cortafuegos_por_decision(entorno_hoy):
    ent, con, s = entorno_hoy
    res = s.calcular(AHORA)
    ops = _por_op(res)
    for op_id, esperado in CORTAFUEGOS_REALES.items():
        assert _etiquetas(res.cortafuegos[ops[op_id].clave]) == esperado, op_id
        for etiqueta in esperado:
            assert etiqueta in ops[op_id].obs
    # la única infracción de entrada de la sesión: 2 contratos con 1 permitido
    assert 'CONTRATOS > PERMITIDOS' in ops['PA16-20260922-006'].obs
    assert ops['PA16-20260922-006'].pos_max == 2 and ops['PA16-20260922-006'].permitidos == 1
    # las seis decisiones marcadas en rojo son exactamente las que llevan etiquetas
    assert sorted(op_id for op_id, f in ops.items() if not f.es_seguidora and f.cortafuegos) == \
        sorted(op_id for op_id, et in CORTAFUEGOS_REALES.items() if et)


def test_la_copia_no_cuenta_en_las_ventanas(entorno_hoy):
    ent, con, s = entorno_hoy
    res = s.calcular(AHORA)
    decs = cortafuegos.decisiones(res.filas, res.filas_abiertas)
    assert all(not d.fila.es_seguidora for d in decs)
    # ninguna fila de PA-15 (seguidora) lleva preventivos ni infracciones de decisión
    for f in filas_por(res, PA15):
        assert f.es_seguidora
        assert not any(x in f.obs for x in ('NO OPERAR HASTA', 'REENTRADA', 'SOBREOPERACIÓN'))
    # si las copias contaran, PA16-002 (2ª apertura de la pareja en 10 min) ya sería sobreoperación
    con_copias = cortafuegos.evaluar(
        sorted([cortafuegos.Decision(f.clave, f, motor.fuentes.utc_a_madrid(f.t_apertura_utc),
                                     motor.fuentes.utc_a_madrid(f.t_cierre_utc), f.neto) for f in res.filas],
               key=lambda d: (d.apertura, d.clave)), ent.reglas)
    ops = _por_op(res)
    assert _etiquetas(con_copias[ops['PA16-20260922-002'].clave]) != []      # contando copias sí saltaría
    assert _etiquetas(res.cortafuegos[ops['PA16-20260922-002'].clave]) == []  # como decisión, no


def _dejar_lider_abierta(con):
    """Borra el fill de salida de PA16-007 (no el de su copia): líder abierta, seguidora cerrada."""
    con.execute("DELETE FROM fills WHERE cuenta=? AND fill_id='1338161'", (PA16,))
    con.execute("DELETE FROM fills_log WHERE cuenta=? AND fill_id='1338161'", (PA16,))
    con.execute("DELETE FROM posiciones_hist WHERE cuenta=? AND t_log>='2026-09-22 19:12'", (PA16,))
    con.commit()


def test_seguidora_cerrada_con_su_lider_abierta_no_es_decision(entorno_hoy):
    """La salida de la líder y la de su copia no llegan en el mismo ciclo. Mientras dure ese desfase
    la seguidora sigue siendo copia: ni cuenta en las ventanas ni abre fila propia."""
    ent, con, s = entorno_hoy
    _dejar_lider_abierta(con)
    res = s.calcular(_utc('19:15:00'))
    (abierta,) = res.filas_abiertas
    copia = next(f for f in res.filas if f.cuenta == PA15 and f.t_apertura_utc.startswith('2026-09-22 17:11'))
    assert abierta.cuenta == PA16 and copia.lider is abierta and copia.es_seguidora
    assert PA15 not in [d.fila.cuenta for d in cortafuegos.decisiones(res.filas, res.filas_abiertas)]
    # la líder todavía no tiene ID: la infracción de la copia no se guarda con decision_id vacío
    assert abierta.op_id == '' and copia.decision_id == copia.op_id
    cortafuegos.registrar(con, res.filas, res.cortafuegos, ent.reglas)
    assert not [r for r in _saltados(con) if not r[0]]


def test_entrada_escalonada_cuenta_como_decision(entorno):
    """21/09: dos entradas que aumentan la posición en PA-16 = dos decisiones (y sus dos copias fuera)."""
    ent, con, s = entorno
    res = s.calcular('2026-09-22 12:00:00.000')
    decs = {d.fila.op_id for d in cortafuegos.decisiones(res.filas, res.filas_abiertas)}
    assert {'PA16-20260921-001', 'PA16-20260921-002'} <= decs
    assert not {f.op_id for f in filas_por(res, PA15)} & decs


def test_sin_penalizacion_de_margen(entorno_hoy):
    """Los cortafuegos no tocan el margen: sigue siendo 500 + Σ (ticks×0,2 − |MAE|)."""
    ent, con, s = entorno_hoy
    res = s.calcular(AHORA)
    fs = filas_por(res, PA16)
    assert [f.op_id for f in fs][-1] == 'PA16-20260922-007' and fs[-1].cortafuegos
    esperado = ent.reglas['margen']['inicial'] + sum(f.consumo for f in fs)
    assert fs[-1].margen_tras == pytest.approx(esperado)
    assert {b['cuenta']: b for b in res.bal}[PA16]['margen'] == pytest.approx(esperado)


# ===========================================================================
# Infracciones de entrada
# ===========================================================================

def test_contratos_excedidos_en_lider_y_copia(entorno_hoy):
    ent, con, s = entorno_hoy
    res = s.calcular(AHORA)
    ops = _por_op(res)
    lider, copia = ops['PA16-20260922-006'], ops['PA15-20260922-006']
    assert copia.lider is lider
    for f in (lider, copia):
        assert [t for t, _ in cortafuegos.infracciones_de_entrada(f, ent.reglas)] == ['CONTRATOS > PERMITIDOS']
        assert f.cortafuegos
    assert cortafuegos.infracciones_de_entrada(lider, ent.reglas)[0][1] == '2 contratos con 1 permitidos'


def test_sin_sl_real_al_borrar_la_orden_de_proteccion(entorno_hoy):
    """PA16-007 (19:11:09,5 entrada; SL a 19:11:09,696). Sin ese alta no hay SL a los 5 s."""
    ent, con, s = entorno_hoy
    assert _por_op(s.calcular(AHORA))['PA16-20260922-007'].sl_ini == 88
    con.execute("DELETE FROM ordenes WHERE cuenta=? AND ext_id='2902156982'", (PA16,))
    con.commit()
    f = _por_op(s.calcular(AHORA))['PA16-20260922-007']
    assert f.sin_sl and f.sl_ini is None and f.telemetria
    assert 'SIN SL' in f.obs.split(SEP) and f.cortafuegos
    assert [t for t, _ in cortafuegos.infracciones_de_entrada(f, ent.reglas)] == ['SIN SL']


def test_sl_cancelado_con_posicion_abierta_es_infraccion(entorno):
    ent, con, s = entorno
    b = filas_por(s.calcular('2026-09-22 12:00:00.000'), SIM)[1]
    assert b.sl_cancelado
    assert [t for t, _ in cortafuegos.infracciones_de_entrada(b, ent.reglas)] == \
        ['SL CANCELADO CON POSICIÓN ABIERTA']
    assert b.cortafuegos and 'SL CANCELADO CON POSICIÓN ABIERTA' in b.obs.split(SEP)


# ===========================================================================
# Tabla cortafuegos_saltados (solo infracciones, idempotente)
# ===========================================================================

INFRACCIONES_REALES = [
    ('PA16-20260922-004', PA16, 'REENTRADA TRAS PÉRDIDA',
     '2026-09-22 17:38:53.509', '2026-09-22 17:46:28.579'),
    ('PA16-20260922-004', PA16, 'SOBREOPERACIÓN 10 MIN',
     '2026-09-22 17:38:53.509', '2026-09-22 17:40:01.983'),
    ('PA16-20260922-004', PA16, 'SOBREOPERACIÓN 1 H',
     '2026-09-22 17:38:53.509', '2026-09-22 17:59:20.280'),
    ('PA16-20260922-006', PA15, 'CONTRATOS > PERMITIDOS', '2026-09-22 18:35:33.490', None),
    ('PA16-20260922-006', PA16, 'CONTRATOS > PERMITIDOS', '2026-09-22 18:35:33.334', None),
    ('E2T250-20260922-002', E250, 'REENTRADA TRAS PÉRDIDA',
     '2026-09-22 18:41:51.966', '2026-09-22 18:48:21.226'),
]


def _saltados(con):
    return [tuple(r) for r in con.execute(
        'SELECT decision_id, cuenta, tipo, hora_evento, hora_permitida FROM cortafuegos_saltados '
        'ORDER BY decision_id, cuenta, tipo')]


def test_tabla_cortafuegos_saltados_contenido(entorno_hoy):
    ent, con, s = entorno_hoy
    s.sync(AHORA)
    assert _saltados(con) == sorted(INFRACCIONES_REALES)
    # los preventivos NO se guardan: PA16-003 lleva tres y no aparece
    assert not [r for r in _saltados(con) if r[0] == 'PA16-20260922-003']


def test_infraccion_de_seguidora_espera_al_id_definitivo_del_lider(entorno_hoy):
    """Con la líder aún abierta, decision_id sería el de la copia; guardarlo dejaría una fila huérfana
    que el UNIQUE no frena al reinsertarse con el ID del líder. Se espera al ciclo siguiente."""
    ent, con, s = entorno_hoy
    res = s.calcular(AHORA)
    copia = _por_op(res)['PA15-20260922-006']            # su infracción propia: CONTRATOS > PERMITIDOS
    copia.lider.op_id = ''                               # la líder aún no ha cerrado en este ciclo
    assert copia.es_seguidora and copia.decision_id == copia.op_id
    cortafuegos.registrar(con, res.filas, res.cortafuegos, ent.reglas)
    assert not [r for r in _saltados(con) if r[0] == copia.op_id]
    # y en el ciclo siguiente, con la líder ya cerrada, se guarda con el ID de la decisión
    res = s.calcular(AHORA)
    cortafuegos.registrar(con, res.filas, res.cortafuegos, ent.reglas)
    assert ('PA16-20260922-006', PA15, 'CONTRATOS > PERMITIDOS') in [r[:3] for r in _saltados(con)]


def test_la_seguidora_no_duplica_infracciones_heredadas_del_lider(entorno_hoy):
    """La copia hereda sin_sl del líder (no tiene órdenes propias): SIN SL se guarda una sola vez.
    CONTRATOS > PERMITIDOS sí es propio de cada cuenta, porque el margen lo es."""
    ent, con, s = entorno_hoy
    con.execute("DELETE FROM ordenes WHERE cuenta=? AND ext_id='2902156982'", (PA16,))
    con.commit()
    res = s.calcular(AHORA)
    ops = _por_op(res)
    lider, copia = ops['PA16-20260922-007'], ops['PA15-20260922-007']
    assert lider.sin_sl and copia.sin_sl and copia.lider is lider
    cortafuegos.registrar(con, res.filas, res.cortafuegos, ent.reglas)
    sin_sl = [r for r in _saltados(con) if r[2] == 'SIN SL']
    assert [r[1] for r in sin_sl] == [PA16]
    assert sorted(r[1] for r in _saltados(con) if r[2] == 'CONTRATOS > PERMITIDOS') == sorted([PA15, PA16])


def test_tabla_cortafuegos_saltados_idempotente_en_reingesta(entorno_hoy):
    ent, con, s = entorno_hoy
    s.sync(AHORA)
    antes = [tuple(r) for r in con.execute('SELECT * FROM cortafuegos_saltados ORDER BY id')]
    con.execute('DELETE FROM log_offsets')
    con.execute('DELETE FROM cdb_mtime')
    s.ingerir(forzar_cdb=True)
    s.sync(AHORA)
    s.sync(AHORA)
    assert [tuple(r) for r in con.execute('SELECT * FROM cortafuegos_saltados ORDER BY id')] == antes


# ===========================================================================
# Sheet: etiquetas en mayúsculas, fondo rojo y letra negra, solo en operaciones
# ===========================================================================

def test_etiquetas_de_cortafuegos_en_mayusculas(entorno_hoy):
    C = entorno_hoy[0].reglas['cortafuegos']
    O = entorno_hoy[0].reglas['observaciones']
    textos = [C['perdida']['preventivo'], C['perdida']['infraccion']]
    textos += [v[k] for v in C['ventanas'] for k in ('preventivo', 'infraccion')]
    textos += [O['sin_sl'], O['sl_cancelado'], O['contratos_excedidos']]
    for t in textos:
        sin_marca = t.replace('{hora}', '')
        assert sin_marca == sin_marca.upper(), t


def test_celda_de_observaciones_en_rojo_con_letra_negra(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    col = vista.OC['Observaciones demonio']
    filas = {lay['valores'][f][vista.OC['ID']]: c for f, c in lay['celdas_cortafuegos']}
    assert set(filas) == {'PA16-20260922-003', 'PA16-20260922-004', 'PA16-20260922-005',
                          'PA16-20260922-006', 'E2T250-20260922-002', 'PA16-20260922-007'}
    assert set(filas.values()) == {col}
    assert all(lay['ops_ini'] <= f < lay['ops_fin'] for f, _ in lay['celdas_cortafuegos'])
    # peticiones reales a la API: fondo rojo (el de Duración) y letra negra
    hoja = sheets.HojaV4.__new__(sheets.HojaV4)
    hoja.tab_id, hoja.filas, hoja.R = 0, len(lay['valores']) + 50, ent.reglas
    rojo = sheets._color(ent.reglas['colores']['rojo'])
    pintadas = [r['repeatCell'] for r in hoja._formatos(lay) if 'repeatCell' in r
                and r['repeatCell']['cell'].get('userEnteredFormat', {}).get('backgroundColor') == rojo]
    assert len(pintadas) == len(lay['celdas_cortafuegos'])
    for p in pintadas:
        assert p['cell']['userEnteredFormat']['textFormat']['foregroundColor'] == sheets._color('#000000')
        assert (p['range']['startColumnIndex'], p['range']['endColumnIndex']) == (col, col + 1)
    # misma familia de rojo que la regla condicional de Duración
    dur = [r['addConditionalFormatRule']['rule'] for r in hoja._formatos(lay) if 'addConditionalFormatRule' in r]
    assert any(x['ranges'][0]['startColumnIndex'] == vista.OC['Duración (s)']
               and x['booleanRule']['format']['backgroundColor'] == rojo for x in dur)


def _rojas(lay):
    return {lay['valores'][f][vista.OC['ID']] for f, _ in lay['celdas_cortafuegos']}


def test_duracion_y_mae_manual_no_ponen_rojo(entorno_hoy):
    """Duración y MAE/MFE manual son etiquetas de análisis: no encienden la celda."""
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    assert 'LARGA AGUANTANDO PÉRDIDA' in _por_op(res)['PA16-20260922-001'].obs
    assert 'PA16-20260922-001' not in _rojas(lay)
    i = next(k for k, r in enumerate(s.hoja.valores) if r and r[0] == 'PA16-20260922-001')
    s.hoja.valores[i][vista.OC[vista.FLOTANTES['mae']]] = '-70'      # sin bajar el margen a 0 contratos
    res, lay = s.sync(AHORA)
    f = _por_op(res)['PA16-20260922-001']
    assert f.mae_manual and 'MAE manual' in f.obs and not f.cortafuegos
    assert 'PA16-20260922-001' not in _rojas(lay)


def test_sin_telemetria_no_pone_rojo(entorno_hoy):
    ent, con, s = entorno_hoy
    con.execute("DELETE FROM fills_log WHERE cuenta=? AND fill_id='1304860'", (PA16,))   # log rotado
    con.commit()
    res, lay = s.sync(AHORA)
    f = _por_op(res)['PA16-20260922-001']
    assert not f.telemetria and f.obs.startswith('[SIN TELEMETRÍA]') and not f.cortafuegos
    assert 'PA16-20260922-001' not in _rojas(lay)


def test_bal_no_lleva_etiquetas_de_cortafuegos(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    for fila in lay['valores'][1:1 + lay['n_bal']]:
        obs = fila[vista.BC['Observaciones demonio']]
        assert not any(x in obs for x in ('NO OPERAR HASTA', 'REENTRADA', 'SOBREOPERACIÓN',
                                          'CONTRATOS > PERMITIDOS', 'SIN SL'))


def test_columnas_manuales_intactas_tras_los_cortafuegos(entorno_hoy):
    ent, con, s = entorno_hoy
    s.sync(AHORA)
    i = next(k for k, r in enumerate(s.hoja.valores) if r and r[0] == 'PA16-20260922-004')
    s.hoja.valores[i][vista.OC['Estrategia']] = 'Ruptura de estructura M208'
    s.hoja.valores[i][vista.OC['Observaciones']] = 'entré caliente'
    res, lay = s.sync(AHORA)
    fila = next(r for r in s.hoja.valores if r and r[0] == 'PA16-20260922-004')
    assert fila[vista.OC['Estrategia']] == 'Ruptura de estructura M208'
    assert fila[vista.OC['Observaciones']] == 'entré caliente'
    assert 'SOBREOPERACIÓN 1 H' in fila[vista.OC['Observaciones demonio']]


# ===========================================================================
# Aviso en vivo (sonido + toast): una vez por evento, en el momento del evento
# ===========================================================================

class Avisos:
    """Doble de la notificación del sistema: lo único que se simula."""

    def __init__(self):
        self.textos = []

    def __call__(self, titulo, texto):
        self.titulo = titulo
        self.textos.append(texto)


def test_aviso_preventivo_da_la_hora_mas_tardia(entorno_hoy):
    ent, con, s = entorno_hoy
    a = Avisos()
    s.vigilar(a, _utc('17:36:20'))                       # PA16-003 cerró a las 17:36:16,579
    assert a.titulo == 'CORTAFUEGOS ATAS'
    assert a.textos == ['NO OPERAR HASTA 17:59:20 (PÉRDIDA: NO OPERAR HASTA 17:46:28; '
                        '2 DECISIONES EN 10 MIN: NO OPERAR HASTA 17:40:01; '
                        '4 DECISIONES EN 1 H: NO OPERAR HASTA 17:59:20)']


def test_aviso_de_infraccion_al_abrir(entorno_hoy):
    ent, con, s = entorno_hoy
    a = Avisos()
    s.vigilar(a, _utc('17:36:20'))                       # preventivo de PA16-003, ya avisado
    s.vigilar(a, _utc('17:38:55'))                       # PA16-004 abre a las 17:38:53,509
    assert a.textos[1:] == [
        'INFRACCIÓN: REENTRADA TRAS PÉRDIDA (PA-APEX-91120-16 MNQZ6). No operar hasta 17:46:28',
        'INFRACCIÓN: SOBREOPERACIÓN 10 MIN (PA-APEX-91120-16 MNQZ6). No operar hasta 17:40:01',
        'INFRACCIÓN: SOBREOPERACIÓN 1 H (PA-APEX-91120-16 MNQZ6). No operar hasta 17:59:20']


def test_aviso_una_sola_vez_por_evento(entorno_hoy):
    ent, con, s = entorno_hoy
    a = Avisos()
    s.vigilar(a, _utc('17:38:55'))
    n = len(a.textos)
    assert n
    for _ in range(3):
        s.vigilar(a, _utc('17:38:57'))
    assert len(a.textos) == n
    claves = [r[0] for r in con.execute('SELECT clave FROM avisos_emitidos')]
    assert len(claves) == len(set(claves))


def test_evento_viejo_se_registra_sin_avisar(entorno_hoy):
    """Arranque del demonio o reingesta: todo lo anterior a max_antiguedad_s entra mudo."""
    ent, con, s = entorno_hoy
    a = Avisos()
    s.vigilar(a, AHORA)                                  # 20:00 Madrid: toda la sesión es pasado
    assert a.textos == []
    emitidos = {r['clave']: r['notificado'] for r in con.execute('SELECT clave, notificado FROM avisos_emitidos')}
    assert emitidos and set(emitidos.values()) == {0}
    assert 'SOBREOPERACIÓN 1 H|PA-APEX-91120-16|1315008' in emitidos


def test_aviso_en_vivo_con_posicion_abierta_sin_sl(entorno_hoy):
    """Sin esperar al cierre: PA16-007 con la orden de SL borrada avisa a los 5 s del fill."""
    ent, con, s = entorno_hoy
    con.execute("DELETE FROM ordenes WHERE cuenta=? AND ext_id='2902156982'", (PA16,))
    con.commit()
    _dejar_lider_abierta(con)
    a = Avisos()
    res = s.vigilar(a, _utc('19:11:13'))                 # 3,5 s tras el fill: aún no
    assert [f.op_id for f in res.filas_abiertas] == ['']      # sigue abierta, todavía sin ID
    assert not [t for t in a.textos if 'SIN SL' in t]
    s.vigilar(a, _utc('19:11:16'))                       # pasados los 5 s
    assert a.textos[-1] == 'INFRACCIÓN: SIN SL (PA-APEX-91120-16 MNQZ6) sin SL a los 5 s'


def test_avisador_usa_sonido_y_titulo_de_reglas(entorno_hoy, monkeypatch):
    R = entorno_hoy[0].reglas
    R['cortafuegos']['aviso'].update(sonido='SystemExclamation', notificacion=True)    # apagados en reglas.yaml desde el 11/11/2026
    llamadas = []
    monkeypatch.setattr(aviso, 'notificar', lambda t, x, s, d, c, toast: llamadas.append((t, x, s, d, c, toast)))
    aviso.avisador(R)(R['cortafuegos']['aviso']['titulo'], 'prueba')
    assert llamadas == [('CORTAFUEGOS ATAS', 'prueba', 'SystemExclamation',
                         R['cortafuegos']['aviso']['duracion_s'],
                         R['cortafuegos']['aviso']['repetir_cada_s'], True)]


def test_el_sonido_insiste_hasta_cubrir_la_duracion(monkeypatch):
    """Un pitido suelto se ignora (decisión del operador): repite hasta los segundos configurados."""
    sonidos, esperas = [], []
    reloj = {'t': 0.0}
    monkeypatch.setattr(aviso, '_sonar', lambda s: sonidos.append(s))
    monkeypatch.setattr(aviso.time, 'monotonic', lambda: reloj['t'])

    def dormir(s):
        esperas.append(s)
        reloj['t'] += s

    aviso._insistir('SystemExclamation', 8, 1.2, dormir=dormir)
    assert sonidos == ['SystemExclamation'] * 7      # 0; 1,2; …; 7,2 s: el 8º caería fuera de los 8 s
    assert sum(esperas) == pytest.approx(7.2)


def test_notificar_insiste_en_un_hilo_y_no_bloquea(monkeypatch):
    """El demonio no puede quedarse 8 s parado: la insistencia va en un hilo demonio aparte."""
    class _HiloFalso:
        def start(self):
            pass

    hilos = []
    monkeypatch.setattr(aviso.subprocess, 'Popen', lambda cmd, **k: None)
    monkeypatch.setattr(aviso.threading, 'Thread',
                        lambda target, args, daemon: (hilos.append((target, args, daemon)), _HiloFalso())[1])
    aviso.notificar('t', 'x', 'SystemExclamation', 8, 1.2)
    assert hilos == [(aviso._insistir, ('SystemExclamation', 8, 1.2), True)]


def test_sin_sonido_pero_con_notificacion_solo_sale_el_toast(entorno_hoy, monkeypatch):
    """`cortafuegos.aviso.sonido` vacío y `notificacion` encendida -> ni pitido ni hilo de insistencia; el toast sale."""
    R = entorno_hoy[0].reglas
    R['cortafuegos']['aviso'].update(sonido=None, notificacion=True)
    sonidos, hilos, cmds = [], [], []
    monkeypatch.setattr(aviso, '_sonar', lambda s: sonidos.append(s))
    monkeypatch.setattr(aviso.winsound, 'PlaySound', lambda *a, **k: sonidos.append(a))
    monkeypatch.setattr(aviso.threading, 'Thread', lambda **k: hilos.append(k))
    monkeypatch.setattr(aviso.subprocess, 'Popen', lambda cmd, **k: cmds.append(cmd))
    aviso.avisador(R)(R['cortafuegos']['aviso']['titulo'], 'prueba')
    assert sonidos == [] and hilos == [] and len(cmds) == 1 and 'prueba' in cmds[0][-1]


def test_notificar_no_bloquea_y_escapa_comillas(monkeypatch):
    lanzado = {}
    monkeypatch.setattr(aviso.winsound, 'PlaySound', lambda *a, **k: lanzado.setdefault('sonido', a))
    monkeypatch.setattr(aviso.subprocess, 'Popen', lambda cmd, **k: lanzado.setdefault('cmd', cmd))
    aviso.notificar("CORTAFUEGOS 'ATAS'", "no operar hasta 17:59:20", 'SystemExclamation')
    assert lanzado['sonido'][0] == 'SystemExclamation'
    script = lanzado['cmd'][-1]
    assert "CORTAFUEGOS ''ATAS''" in script and 'no operar hasta 17:59:20' in script
    assert lanzado['cmd'][0] == 'powershell.exe' and '-NonInteractive' in lanzado['cmd']


def test_el_ciclo_del_demonio_vigila_sin_esperar_al_sync(entorno_hoy):
    """El demonio evalúa los cortafuegos en cada paso, aunque no toque escribir el sheet."""
    import demonio_v4
    ent, con, s = entorno_hoy
    a = Avisos()
    logs = []
    ciclo = demonio_v4.Ciclo(ent, con, s, logs.append, avisar=a)
    ciclo.paso()
    assert con.execute('SELECT COUNT(*) FROM avisos_emitidos').fetchone()[0] == 11
    assert not [x for x in logs if 'ERROR' in x]


def test_las_copias_no_duplican_el_aviso_de_contratos(entorno_hoy):
    ent, con, s = entorno_hoy
    a = Avisos()
    s.vigilar(a, _utc('18:35:40'))                       # PA16-006 y su copia abren 2 contratos
    excedidos = [t for t in a.textos if 'CONTRATOS > PERMITIDOS' in t]
    assert excedidos == ['INFRACCIÓN: CONTRATOS > PERMITIDOS (PA-APEX-91120-16 MNQZ6) '
                         '2 contratos con 1 permitidos']
