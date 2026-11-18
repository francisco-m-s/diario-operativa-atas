"""Máxima amplitud del movimiento y Calificación (spec §4.2 y §4.3, decisión del operador del 01/10/2026).

- Amplitud = |MAE| + |MFE| en ticks por contrato, con el manual de cada uno si existe; vacía si falta alguno. El fill de
  salida más alejado de la entrada sigue dando el Tipo cierre.
- Calificación = proceso × resultado, en todas las operaciones de SQLite; el sheet muestra la de la líder.
Fixtures reales: `entorno` (16–22/09) y `entorno_hoy` (22/09, PA-16 copiada en PA-15).
"""

import copy
import json
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest

from atasv4 import config, fuentes, motor, sheets, vista
from conftest import RAIZ

AHORA = '2026-09-22 12:00:00.000'          # fixture entorno
AHORA_HOY = '2026-09-22 18:00:00.000'      # fixture entorno_hoy: la sesión del 22/09 ya terminó
X = 'Continuación de M204s confirmada por M17'
MAE_C, MFE_C = vista.FLOTANTES['mae'], vista.FLOTANTES['mfe']
AMP, CAL = 'Máxima amplitud del movimiento', 'Calificación'
# Layout de operaciones hasta el 30/09/2026: el de ahora sin 'Calificación' (29 columnas, OrderId oculto en la AC)
OPS_VIEJO = [c for c in vista.OPS_COLS if c != CAL]


@pytest.fixture(scope='module')
def R():
    return config.cargar_yaml(os.path.join(RAIZ, 'reglas.yaml'))


def _fila(res, op_id):
    return next(f for f in res.filas if f.op_id == op_id)


def _celda(s, op_id, col):
    i = next(k for k, r in enumerate(s.hoja.valores) if r and r[0] == op_id)
    return i, vista.OC[col]


def _editar(s, op_id, col, valor):
    i, c = _celda(s, op_id, col)
    s.hoja.valores[i][c] = valor


def _ver(s, op_id, col):
    i, c = _celda(s, op_id, col)
    return s.hoja.valores[i][c]


def _ops_sqlite(con):
    return {r['op_id']: dict(json.loads(r['datos']), lider_id=r['lider_id'])
            for r in con.execute('SELECT op_id, lider_id, datos FROM operaciones')}


# ===========================================================================
# Máxima amplitud del movimiento
# ===========================================================================

def test_amplitud_del_log_en_ticks(entorno):
    ent, con, s = entorno
    res, _ = s.sync(AHORA)
    f = _fila(res, 'SIM-20260922-002')
    assert (f.mae, f.mfe, f.max_amplitud) == (-161, 42, 203)            # |−161| + |42|
    assert _ver(s, 'SIM-20260922-002', AMP) == 203
    assert _ops_sqlite(con)['SIM-20260922-002']['max_amplitud'] == 203
    seg = _fila(res, 'PA15-20260921-001')                                # la seguidora, con su propio flotante
    assert (seg.mae, seg.mfe, seg.max_amplitud) == (-21, 172, 193)


@pytest.mark.parametrize('mae, mfe, esperado', [
    ('-200,5', None, 200.5 + 42),          # solo el MAE manual (el MFE del log)
    (None, 50, 161 + 50),                  # solo el MFE manual (el MAE del log)
    (-100, '60', 100 + 60),                # los dos manuales
])
def test_amplitud_con_manuales_cada_uno_por_separado(entorno, mae, mfe, esperado):
    ent, con, s = entorno
    s.sync(AHORA)
    if mae is not None:
        _editar(s, 'SIM-20260922-002', MAE_C, mae)
    if mfe is not None:
        _editar(s, 'SIM-20260922-002', MFE_C, mfe)
    res, _ = s.sync(AHORA)                                                # recalculada en ese mismo sync
    f = _fila(res, 'SIM-20260922-002')
    assert f.max_amplitud == pytest.approx(esperado)
    assert _ver(s, 'SIM-20260922-002', AMP) == pytest.approx(esperado)
    assert _ops_sqlite(con)['SIM-20260922-002']['max_amplitud'] == pytest.approx(esperado)
    assert (f.mae_log, f.mfe_log) == (-161, 42)                          # el log se conserva


def test_amplitud_sin_telemetria_vacia_hasta_tener_los_dos_manuales(entorno):
    ent, con, s = entorno
    con.execute("DELETE FROM fills_log WHERE cuenta='PA-APEX-91120-16' AND fill_id='1478215'")   # log rotado
    con.commit()
    res, _ = s.sync(AHORA)
    f = _fila(res, 'PA16-20260916-001')
    assert not f.telemetria and f.max_amplitud is None and _ver(s, 'PA16-20260916-001', AMP) == ''
    _editar(s, 'PA16-20260916-001', MAE_C, -100)                          # falta el MFE: sigue vacía
    res, _ = s.sync(AHORA)
    assert _fila(res, 'PA16-20260916-001').max_amplitud is None and _ver(s, 'PA16-20260916-001', AMP) == ''
    _editar(s, 'PA16-20260916-001', MFE_C, 30)
    res, _ = s.sync(AHORA)
    assert _fila(res, 'PA16-20260916-001').max_amplitud == 130 and _ver(s, 'PA16-20260916-001', AMP) == 130


def test_tipo_cierre_sigue_saliendo_del_fill_mas_alejado(entorno):
    """El Tipo cierre no cambia en ninguna operación del fixture (los mismos valores que antes del cambio), ni al
    corregir a mano el MAE/MFE, que sí cambia la amplitud."""
    esperado = {
        'E2T249-20260916-001': 'Mercado', 'E2T249-20260916-002': 'Mercado', 'E2T249-20260916-003': 'TP',
        'PA16-20260916-001': 'SL', 'BX50-20260916-001': 'Límite', 'BX50-20260916-002': 'Mercado',
        'PA16-20260921-001': 'SL', 'PA15-20260921-001': 'Mercado', 'PA16-20260921-002': 'SL',
        'PA15-20260921-002': 'Mercado', 'SIM-20260922-001': 'Mercado', 'SIM-20260922-002': 'Mercado',
    }
    ent, con, s = entorno
    res, _ = s.sync(AHORA)
    assert {f.op_id: f.tipo_cierre for f in res.filas} == esperado
    # E249-002: dos salidas, la más alejada a mercado (29429) y la última por el SL. Manda la más alejada.
    _editar(s, 'E2T249-20260916-002', MAE_C, -5)
    _editar(s, 'E2T249-20260916-002', MFE_C, 7)
    res, _ = s.sync(AHORA)
    f = _fila(res, 'E2T249-20260916-002')
    assert (f.max_amplitud, f.tipo_cierre, motor._salida_max_amplitud(f)['precio']) == (12, 'Mercado', 29429)
    assert _ver(s, 'E2T249-20260916-002', 'Tipo cierre') == 'Mercado'


def test_tipo_cierre_no_cambia_en_la_sesion_del_22_09(entorno_hoy):
    ent, con, s = entorno_hoy
    res, _ = s.sync(AHORA_HOY)
    assert {f.op_id: f.tipo_cierre for f in res.filas} == {
        'E2T250-20260922-001': 'SL', 'E2T250-20260922-002': 'TP',
        'PA16-20260922-001': 'SL', 'PA16-20260922-002': 'Mercado', 'PA16-20260922-003': 'Mercado',
        'PA16-20260922-004': 'SL', 'PA16-20260922-005': 'SL', 'PA16-20260922-006': 'Mercado', 'PA16-20260922-007': 'SL',
        **{f'PA15-20260922-00{n}': 'Mercado' for n in range(1, 8)}}


# ===========================================================================
# Calificación: reglas puras sobre filas sintéticas
# ===========================================================================

def _op(**kw):
    """Operación limpia: telemetría, SL, contratos dentro del límite, duración CORRECTA, +20 t."""
    f = motor.Fila(cuenta='C', simbolo='NQZ6@CME', direccion=1, order_id='1')
    for k, v in {'op_id': 'C-1', 'clave': 'C|1', 'ticks': 20.0, 'telemetria': True, 'simbolo_ok': True,
                 'permitidos': 1, 'pos_max': 1, 'etq_duracion': 'CORRECTA', **kw}.items():
        setattr(f, k, v)
    return f


def _calificar(R, *filas, ev=None, manual=None, tardias=()):
    manual = {f.decision_id: {'estrategia': X} for f in filas} if manual is None else manual
    motor.calificar(list(filas), ev or {}, manual, set(tardias), R)
    return filas[0]


def _ev(clave, *tipos):
    return {clave: {'preventivos': [], 'infracciones': [
        {'tipo': t, 'hora_evento': None, 'hora_permitida': None, 'detalle': ''} for t in tipos]}}


def test_la_lista_de_reglas_usa_los_textos_de_reglas_yaml_sin_duplicarlos(R):
    P = R['calificacion']['proceso_incorrecto']
    O, C, Pr = R['observaciones'], R['cortafuegos'], R['pruebas']
    assert P['infracciones'] == [O['sin_sl'], O['sl_cancelado'], O['contratos_excedidos'], C['perdida']['infraccion'],
                                 C['seguidas']['infraccion'],      # §10.4 (07/10/2026)
                                 C['ventanas'][0]['infraccion'], C['ventanas'][1]['infraccion'],
                                 Pr['objetivo_alcanzado']['infraccion'], Pr['perdida_diaria']['infraccion']]
    assert P['estrategias'] == [R['estrategia_por_defecto']] and P['etiquetas'] == [O['estrategia_tardia']]
    assert P['larga'] == [O['larga']['aguantando_perdida']['etiqueta']]


# cada condición de proceso incorrecto por separado: (atributos de la fila, argumentos de calificar)
INCORRECTO = {
    'SIN SL': lambda R: ({'sin_sl': True}, {}),
    'SL CANCELADO': lambda R: ({'sl_cancelado': True}, {}),
    'CONTRATOS > PERMITIDOS (margen)': lambda R: ({'pos_max': 2}, {}),
    'CONTRATOS > PERMITIDOS (prueba)': lambda R: ({'permitidos': None, 'neq': 2.1, 'neq_max': 2}, {}),
    'REENTRADA TRAS PÉRDIDA': lambda R: ({}, {'ev': _ev('C|1', R['cortafuegos']['perdida']['infraccion'])}),
    'SOBREOPERACIÓN 10 MIN': lambda R: ({}, {'ev': _ev('C|1', R['cortafuegos']['ventanas'][0]['infraccion'])}),
    'SOBREOPERACIÓN 1 H': lambda R: ({}, {'ev': _ev('C|1', R['cortafuegos']['ventanas'][1]['infraccion'])}),
    'OPERACIÓN TRAS OBJETIVO': lambda R: (
        {'infracciones_cuenta': [(R['pruebas']['objetivo_alcanzado']['infraccion'], 'x')]}, {}),
    'OPERACIÓN TRAS PÉRDIDA DIARIA': lambda R: (
        {'infracciones_cuenta': [(R['pruebas']['perdida_diaria']['infraccion'], 'x')]}, {}),
    'Sin estrategia (sin manual)': lambda R: ({}, {'manual': {}}),
    'Sin estrategia (celda vaciada)': lambda R: ({}, {'manual': {'C-1': {'estrategia': ''}}}),
    'sin estrategia (escrita a mano, otras mayúsculas)': lambda R: ({}, {'manual': {'C-1': {'estrategia': ' sin ESTRATEGIA '}}}),
    'ESTRATEGIA ELEGIDA CON LA OPERACIÓN ABIERTA': lambda R: ({}, {'tardias': {'C-1'}}),
    'LARGA AGUANTANDO PÉRDIDA': lambda R: ({'etq_duracion': 'LARGA AGUANTANDO PÉRDIDA'}, {}),
}


@pytest.mark.parametrize('caso', list(INCORRECTO))
@pytest.mark.parametrize('ticks, cuadrante', [(20, 'Trade Peligroso'), (-20, 'Trade Basura')])
def test_cada_condicion_de_proceso_incorrecto(R, caso, ticks, cuadrante):
    attrs, kw = INCORRECTO[caso](R)
    f = _calificar(R, _op(ticks=ticks, **attrs), **kw)
    assert (f.proceso_ok, f.calificacion) == (False, cuadrante)


@pytest.mark.parametrize('caso, attrs, kw', [
    ('limpia, con estrategia', {}, {}),
    ('estrategia manual en vez de Sin estrategia', {}, {'manual': {'C-1': {'estrategia': 'Cualquier texto'}}}),
    ('LARGA SIN GESTIÓN', {'etq_duracion': 'LARGA SIN GESTIÓN'}, {}),
    ('LARGA DEJANDO CORRER', {'etq_duracion': 'LARGA DEJANDO CORRER'}, {}),
    ('LARGA sin MAE', {'etq_duracion': 'LARGA'}, {}),
    ('MICRO-RUIDO', {'etq_duracion': 'MICRO-RUIDO'}, {}),
    ('sin telemetría: no hay SIN SL', {'telemetria': False, 'sin_sl': True}, {}),
    ('preventivos de la decisión', {}, {'ev': {'C|1': {'preventivos': [('PÉRDIDA: NO OPERAR HASTA {hora}', None)],
                                                       'infracciones': []}}}),
    ('preventivos de la cuenta', {'etiquetas_cuenta': ['PÉRDIDA DIARIA −75 T: NO OPERAR MÁS HOY',
                                                       '+50 DEL DÍA ALCANZADO: NO OPERAR MÁS HOY',
                                                       'OBJETIVO ALCANZADO: NO OPERAR MÁS',
                                                       'MÁXIMO BAL DE HOY: 26.025 · REDUCIR BAL']}, {}),
])
def test_lo_que_no_cuenta_deja_el_proceso_correcto(R, caso, attrs, kw):
    f = _calificar(R, _op(**attrs), **kw)
    assert (f.proceso_ok, f.calificacion) == (True, 'Trade Perfecto')


@pytest.mark.parametrize('ticks, categoria', [
    (10, 'ganador'), (10.25, 'ganador'), (-10, 'perdedor'), (-10.25, 'perdedor'),
    (9.75, 'neutro'), (-9.75, 'neutro'), (0, 'neutro')])
def test_bordes_del_resultado(R, ticks, categoria):
    assert motor.categoria_resultado(ticks, R) == categoria
    for kw in ({}, {'manual': {}}):                                      # con proceso correcto e incorrecto
        f = _calificar(R, _op(ticks=ticks), **kw)
        assert f.categoria_resultado == categoria
        if categoria == 'neutro':
            assert f.calificacion == 'Neutro'


@pytest.mark.parametrize('kw, ticks, cuadrante, proceso, categoria', [
    ({}, 10, 'Trade Perfecto', True, 'ganador'),
    ({}, -10, 'Buen Trade', True, 'perdedor'),
    ({'manual': {}}, 10, 'Trade Peligroso', False, 'ganador'),
    ({'manual': {}}, -10, 'Trade Basura', False, 'perdedor'),
    ({}, 9.75, 'Neutro', True, 'neutro'),                  # el neutro guarda su proceso para separarlos después
    ({'manual': {}}, -9.75, 'Neutro', False, 'neutro'),
])
def test_los_cinco_cuadrantes(R, kw, ticks, cuadrante, proceso, categoria):
    f = _calificar(R, _op(ticks=ticks), **kw)
    assert (f.calificacion, f.proceso_ok, f.categoria_resultado) == (cuadrante, proceso, categoria)


def test_umbrales_y_textos_salen_de_reglas_yaml(R):
    R2 = copy.deepcopy(R)
    R2['calificacion']['resultado']['ganador_min'] = 30
    R2['calificacion']['cuadrantes']['correcto']['perdedor'] = 'OTRO TEXTO'
    R2['calificacion']['proceso_incorrecto']['infracciones'].remove(R['observaciones']['sin_sl'])
    assert _calificar(R2, _op(ticks=20)).calificacion == 'Neutro'
    assert _calificar(R2, _op(ticks=-20)).calificacion == 'OTRO TEXTO'
    assert _calificar(R2, _op(ticks=-20, sin_sl=True)).proceso_ok is True      # ya no está en la lista


def test_seguidora_frente_a_lider(R):
    """Cada una con sus etiquetas: la seguidora no lleva REENTRADA ni SOBREOPERACIÓN (son de la fila de la decisión,
    §4.5), sí sus propias infracciones y su resultado. Estrategia y tardía son de la decisión: las de la líder."""
    def par(**kw_seg):
        lider = _op(op_id='L-1', clave='L|1', ticks=12)
        seg = _op(op_id='S-1', clave='S|1', lider=lider, **kw_seg)
        lider.copias = [seg]
        return lider, seg
    lider, seg = par()
    motor.calificar([lider, seg], _ev('L|1', R['cortafuegos']['perdida']['infraccion']), {'L-1': {'estrategia': X}},
                    set(), R)
    assert (lider.calificacion, seg.calificacion) == ('Trade Peligroso', 'Trade Perfecto')
    lider, seg = par(pos_max=2)                                          # CONTRATOS > PERMITIDOS propio de la seguidora
    motor.calificar([lider, seg], {}, {'L-1': {'estrategia': X}}, set(), R)
    assert (lider.calificacion, seg.calificacion) == ('Trade Perfecto', 'Trade Peligroso')
    lider, seg = par(ticks=9.75)                                         # su propio resultado (deslizamiento)
    motor.calificar([lider, seg], {}, {'L-1': {'estrategia': X}}, set(), R)
    assert (lider.calificacion, seg.calificacion) == ('Trade Perfecto', 'Neutro')
    lider, seg = par()                                                   # la estrategia es la de la decisión
    motor.calificar([lider, seg], {}, {'S-1': {'estrategia': X}}, set(), R)
    assert (lider.proceso_ok, seg.proceso_ok) == (False, False)
    lider, seg = par()                                                   # la tardía también
    motor.calificar([lider, seg], {}, {'L-1': {'estrategia': X}}, {'L-1'}, R)
    assert (lider.proceso_ok, seg.proceso_ok) == (False, False)


# ===========================================================================
# Calificación de punta a punta (sync con la hoja falsa)
# ===========================================================================

def test_sync_guarda_todas_y_el_sheet_muestra_la_de_la_lider(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA_HOY)
    assert lay['valores'][lay['ops_header']] == vista.OPS_COLS
    assert vista.OC[CAL] == vista.OC['Observaciones demonio'] + 1          # justo después (spec §4.3)
    ops = _ops_sqlite(con)
    assert set(ops) == {f.op_id for f in res.filas} and len(ops) == 16    # líderes y seguidoras
    assert all({'calificacion', 'proceso_ok', 'categoria_resultado'} <= set(d) for d in ops.values())
    # sin estrategia en ninguna: todas con el proceso incorrecto; E2T250-001 (+7 t) es neutra
    assert ops['E2T250-20260922-001']['calificacion'] == 'Neutro'
    assert ops['E2T250-20260922-001']['proceso_ok'] is False
    assert ops['E2T250-20260922-001']['categoria_resultado'] == 'neutro'
    assert {d['calificacion'] for k, d in ops.items() if k != 'E2T250-20260922-001'} == {'Trade Peligroso',
                                                                                        'Trade Basura'}
    # el operador pone la estrategia en dos decisiones (fila de la líder)
    _editar(s, 'PA16-20260922-004', 'Estrategia', X)
    _editar(s, 'PA16-20260922-007', 'Estrategia', X)
    res, lay = s.sync(AHORA_HOY)
    ops = _ops_sqlite(con)
    # PA16-004 +38 t lleva REENTRADA y SOBREOPERACIÓN 10 MIN / 1 H; su copia en PA-15 (+36 t) no las lleva
    assert (ops['PA16-20260922-004']['calificacion'], ops['PA15-20260922-004']['calificacion']) == \
        ('Trade Peligroso', 'Trade Perfecto')
    assert ops['PA15-20260922-004']['lider_id'] == 'PA16-20260922-004'
    # PA16-007 −25 t y su copia −23 t, LARGA SIN GESTIÓN y sin infracciones: estrategia de la decisión en las dos
    assert (ops['PA16-20260922-007']['calificacion'], ops['PA15-20260922-007']['calificacion']) == \
        ('Buen Trade', 'Buen Trade')
    assert _ver(s, 'PA16-20260922-004', CAL) == 'Trade Peligroso'          # la de la líder
    assert _ver(s, 'PA16-20260922-007', CAL) == 'Buen Trade'
    assert not any(r and str(r[0]).startswith('PA15-') for r in s.hoja.valores)    # la seguidora no tiene fila
    i, c = _celda(s, 'PA16-20260922-004', CAL)
    assert (i, c) not in lay['celdas_cortafuegos'] and (i, c) not in lay['celdas_manuales']   # texto, sin color


def test_recalculo_al_editar_el_mae_a_mano(entorno_hoy):
    """PA16-20260922-001 (+61 t, 776 s, SL 88): MAE del log −52 > 50 % del SL -> LARGA AGUANTANDO PÉRDIDA. Al poner el
    MAE a −20 a mano cambian en ese mismo sync la duración, la amplitud y la calificación, también en la seguidora."""
    ent, con, s = entorno_hoy
    s.sync(AHORA_HOY)
    _editar(s, 'PA16-20260922-001', 'Estrategia', X)
    res, _ = s.sync(AHORA_HOY)
    lider, seg = _fila(res, 'PA16-20260922-001'), _fila(res, 'PA15-20260922-001')
    assert (lider.etq_duracion, lider.max_amplitud, lider.calificacion) == ('LARGA AGUANTANDO PÉRDIDA', 133,
                                                                           'Trade Peligroso')
    assert (seg.etq_duracion, seg.max_amplitud, seg.calificacion) == ('LARGA AGUANTANDO PÉRDIDA', 133, 'Trade Peligroso')
    _editar(s, 'PA16-20260922-001', MAE_C, '-20')
    res, _ = s.sync(AHORA_HOY)
    lider, seg = _fila(res, 'PA16-20260922-001'), _fila(res, 'PA15-20260922-001')
    assert (lider.mae, lider.etq_duracion, lider.max_amplitud, lider.calificacion) == (-20, 'LARGA SIN GESTIÓN', 101,
                                                                                      'Trade Perfecto')
    assert (seg.mae, seg.etq_duracion, seg.max_amplitud, seg.calificacion) == (-20, 'LARGA SIN GESTIÓN', 94,
                                                                              'Trade Perfecto')
    assert (_ver(s, 'PA16-20260922-001', AMP), _ver(s, 'PA16-20260922-001', CAL)) == (101, 'Trade Perfecto')
    ops = _ops_sqlite(con)
    assert (ops['PA16-20260922-001']['calificacion'], ops['PA15-20260922-001']['calificacion']) == \
        ('Trade Perfecto', 'Trade Perfecto')
    _editar(s, 'PA16-20260922-001', MAE_C, '')                            # celda vaciada: vuelve al log
    res, _ = s.sync(AHORA_HOY)
    assert (_fila(res, 'PA16-20260922-001').calificacion, _ver(s, 'PA16-20260922-001', AMP)) == ('Trade Peligroso', 133)


def _decisiones(res):
    return sorted((f for f in res.filas if not f.es_seguidora), key=lambda f: f.t_apertura_utc)


def _ts(t, **d):
    return fuentes.fmt_ts(fuentes.parse_ts(t) + timedelta(**d))


@pytest.mark.parametrize('segundos, tardia, cuadrante', [(-60, False, 'Buen Trade'), (30, True, 'Trade Basura')])
def test_estrategia_de_la_preoperativa_cuenta_en_el_mismo_sync(entorno_hoy, segundos, tardia, cuadrante):
    """La decisión que se anota recibe la estrategia de la preoperativa en el sync en que se escribe por primera vez, y
    su calificación ya sale con ella (y con la marca de tardía si se eligió con la operación abierta)."""
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA_HOY)
    f = _decisiones(res)[-1]
    assert f.op_id == 'PA16-20260922-007' and f.duracion > 30 + ent.reglas['preoperativa']['tolerancia_tardia_s']
    con.execute('DELETE FROM escrito_ops WHERE op_id=?', (f.op_id,))       # como si acabara de cerrar
    s.hoja.valores[lay['preop_fila']][lay['preop_col']] = X
    con.execute("INSERT OR REPLACE INTO estado VALUES ('preop_t_lectura', ?)", (_ts(f.t_apertura_utc, seconds=segundos),))
    con.commit()
    res, _ = s.sync(AHORA_HOY)
    assert _ver(s, f.op_id, 'Estrategia') == X
    assert (ent.reglas['observaciones']['estrategia_tardia'] in _ver(s, f.op_id, 'Observaciones demonio')) == tardia
    ops = _ops_sqlite(con)
    assert (_ver(s, f.op_id, CAL), ops[f.op_id]['calificacion'], ops['PA15-20260922-007']['calificacion']) == \
        (cuadrante, cuadrante, cuadrante)


# ===========================================================================
# Primer sync con la pestaña escrita por la versión anterior (sin 'Calificación')
# ===========================================================================

def _a_layout_viejo(valores, ops_ini, precio):
    """Pestaña tal como la dejó el demonio anterior: sin la columna Calificación y con el precio en la amplitud."""
    i_cal, i_amp = vista.OC[CAL], vista.OC[AMP]
    out = []
    for k, r in enumerate(valores):
        r = list(r)
        if k >= ops_ini and r and r[0]:
            r[i_amp] = precio
        del r[i_cal]
        out.append(r)
    return out


def test_primer_sync_con_el_layout_viejo(entorno):
    ent, con, s = entorno
    _, lay = s.sync(AHORA)
    viejo = _a_layout_viejo(s.hoja.valores, lay['ops_ini'], 30651.25)
    h = lay['ops_header']
    assert viejo[h] == OPS_VIEJO and len(OPS_VIEJO) == 29 and OPS_VIEJO.index('OrderId') == 28
    ov = {c: i for i, c in enumerate(OPS_VIEJO)}
    fila = {r[0]: r for r in viejo[h + 1:] if r and r[0]}
    # el operador edita con el layout viejo (Análisis LLM en la AB, no en la AC de ahora) y corrige un MAE
    fila['PA16-20260921-001'][ov['Estrategia']] = X
    fila['PA16-20260921-001'][ov['Observaciones']] = 'entré tarde'
    fila['SIM-20260922-002'][ov[MAE_C]] = -100
    s.hoja.valores = viejo
    escrito_antes = {r['op_id']: (r['mae'], r['mfe']) for r in con.execute('SELECT * FROM escrito_ops')}
    res, lay = s.sync(AHORA)
    # manuales leídos por nombre de encabezado en el layout viejo y escritos en su sitio nuevo
    assert lay['valores'][lay['ops_header']] == vista.OPS_COLS
    assert (_ver(s, 'PA16-20260921-001', 'Estrategia'), _ver(s, 'PA16-20260921-001', 'Observaciones')) == (X, 'entré tarde')
    m = con.execute("SELECT * FROM manual_ops WHERE op_id='PA16-20260921-001'").fetchone()
    assert (m['estrategia'], m['observaciones']) == (X, 'entré tarde')      # 'Análisis LLM' salió de la hoja con la V5
    # el precio viejo de la amplitud no se toma por manual: solo el MAE corregido
    manuales = {r['op_id']: (r['mae_manual'], r['mfe_manual']) for r in con.execute(
        'SELECT op_id, mae_manual, mfe_manual FROM manual_ops WHERE mae_manual IS NOT NULL OR mfe_manual IS NOT NULL')}
    assert manuales == {'SIM-20260922-002': (-100, None)}
    assert [f.op_id for f in res.filas if f.mae_manual or f.mfe_manual] == ['SIM-20260922-002']
    # calificación y amplitud con los valores finales en este primer sync
    assert (_fila(res, 'SIM-20260922-002').max_amplitud, _ver(s, 'SIM-20260922-002', AMP)) == (142, 142)
    assert _ver(s, 'PA16-20260921-001', CAL) == 'Trade Perfecto'           # +102 t, sin infracciones, con estrategia
    assert _fila(res, 'PA15-20260921-001').calificacion == 'Trade Perfecto'
    assert _ver(s, 'PA16-20260921-001', AMP) == 177                       # ticks, no el precio viejo
    # escrito_ops sigue siendo lo que hay en las celdas MAE/MFE de la pestaña nueva
    escrito = {r['op_id']: (r['mae'], r['mfe']) for r in con.execute('SELECT * FROM escrito_ops')}
    en_hoja = {r[0]: (r[vista.OC[MAE_C]], r[vista.OC[MFE_C]]) for r in s.hoja.valores[lay['ops_ini']:lay['ops_fin']]}
    assert escrito == en_hoja
    assert {k: v for k, v in escrito.items() if k != 'SIM-20260922-002'} == \
        {k: v for k, v in escrito_antes.items() if k != 'SIM-20260922-002'}
    # la columna que el demonio ocultó con el layout viejo (OrderId en la AC) se vuelve a mostrar, una vez
    assert lay['mostrar_columnas'] == [28] and vista.OC['OrderId'] == 29
    _, lay = s.sync(AHORA)
    assert lay['mostrar_columnas'] == []
    assert {r['op_id']: (r['mae_manual'], r['mfe_manual']) for r in con.execute(
        'SELECT op_id, mae_manual, mfe_manual FROM manual_ops WHERE mae_manual IS NOT NULL OR mfe_manual IS NOT NULL')} \
        == manuales


# ===========================================================================
# Sheets: formatos y columnas (sin API)
# ===========================================================================

def _hoja(R, svc=None):
    h = sheets.HojaV4.__new__(sheets.HojaV4)
    h.R, h.tab_id, h.filas, h.columnas, h.n_cf, h.sid, h.tab, h.svc = R, 7, 500, vista.N_COLS, 0, 'x', 'V4', svc
    return h


def test_formatos_amplitud_en_ticks_calificacion_texto_y_columna_oculta(entorno):
    ent, con, s = entorno
    _, lay = s.sync(AHORA)
    lay = dict(lay, mostrar_columnas=[28])
    reqs = _hoja(ent.reglas)._formatos(lay)

    def formatos(col):
        return [r['repeatCell']['cell']['userEnteredFormat']['numberFormat']['pattern'] for r in reqs
                if 'repeatCell' in r and 'numberFormat' in r['repeatCell']['cell']['userEnteredFormat']
                and r['repeatCell']['range']['startColumnIndex'] == col
                and r['repeatCell']['range']['startRowIndex'] >= lay['ops_ini']]     # solo la zona de operaciones (el BAL comparte índices)
    assert formatos(vista.OC[AMP]) == formatos(vista.OC[MAE_C]) == ['0.##']
    assert formatos(vista.OC[CAL]) == []
    reglas_cf = [r['addConditionalFormatRule']['rule']['ranges'][0]['startColumnIndex'] for r in reqs
                 if 'addConditionalFormatRule' in r]
    assert vista.OC[CAL] not in reglas_cf
    dims = [(r['updateDimensionProperties']['range']['startIndex'], r['updateDimensionProperties']['properties'])
            for r in reqs if 'updateDimensionProperties' in r]
    assert dims == [(28, {'hiddenByUser': False}), (vista.OC['OrderId'], {'hiddenByUser': True})]


class _Svc:
    """Lo justo de la API de Sheets para asegurar_pestana: metadatos de la pestaña y batchUpdate."""

    def __init__(self, columnas):
        self.columnas, self.cuerpos = columnas, []

    def spreadsheets(self):
        return self

    def get(self, **_):
        return SimpleNamespace(execute=lambda: {'sheets': [{'properties': {
            'sheetId': 7, 'title': 'V4', 'gridProperties': {'rowCount': 500, 'columnCount': self.columnas}}}]})

    def batchUpdate(self, spreadsheetId, body):
        self.cuerpos.append(body)
        return SimpleNamespace(execute=lambda: {})


@pytest.mark.parametrize('columnas, peticiones', [
    (29, [{'requests': [{'appendDimension': {'sheetId': 7, 'dimension': 'COLUMNS', 'length': 1}}]}]),
    (30, []), (40, [])])
def test_la_pestana_vieja_recibe_la_columna_que_falta(R, columnas, peticiones):
    h = _hoja(R, _Svc(columnas))
    assert h.asegurar_pestana() is False and h.svc.cuerpos == peticiones
    assert h.columnas == max(columnas, vista.N_COLS) == max(columnas, 30)
