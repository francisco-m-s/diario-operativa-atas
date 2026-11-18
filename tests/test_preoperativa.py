import pytest
"""Preoperativa (spec §7.2): celda 'Estrategia a usar' encima de las operaciones, asignación al anotar y control
de la estrategia elegida con la operación abierta. Sesión real del 22/09/2026 (fixture entorno_hoy)."""

from datetime import timedelta

import demonio_v4
from atasv4 import almacen, fuentes, reloj, sheets, vista

@pytest.fixture(autouse=True)
def _compuerta_visible(monkeypatch):
    """Tests de las filas SÍ/NO de la compuerta: la V5 ya no las muestra (`preoperativa.compuerta.mostrar: false`, spec §16.3),
    pero el código sigue ahí para las decisiones de la V4; aquí se prueban con el interruptor encendido."""
    from atasv4 import config as _config
    real = _config.cargar_entorno

    def cargar(*a, **k):
        ent = real(*a, **k)
        ent.reglas['preoperativa']['compuerta']['mostrar'] = True
        return ent

    monkeypatch.setattr(_config, 'cargar_entorno', cargar)


AHORA = '2026-09-22 18:00:00.000'          # 20:00 Madrid: la sesión del 22/09 ya terminó
X = 'Continuación de M204s confirmada por M17'
OTRA = 'Cazas de soporte/resistencia en 204s'


def _ts(s, **d):
    return fuentes.fmt_ts(fuentes.parse_ts(s) + timedelta(**d))


def _decisiones(res):
    return sorted((f for f in res.filas if not f.es_seguidora), key=lambda f: f.t_apertura_utc)


def _como_nuevas(con, *decisiones):
    """Como si aún no se hubieran anotado (acaban de cerrar)."""
    for f in decisiones:
        con.execute('DELETE FROM escrito_ops WHERE op_id=?', (f.op_id,))
    con.commit()


def _elegir(s, lay, valor, t_lectura_previa):
    """El operador elige en la celda; la última lectura del demonio con el valor anterior fue t_lectura_previa."""
    s.hoja.valores[lay['preop_fila']][lay['preop_col']] = valor
    almacen.set_estado(s.con, 'preop_t_lectura', t_lectura_previa)
    s.con.commit()


def _filas(lay):
    return {r[0]: (i, r) for i, r in enumerate(lay['valores']) if lay['ops_ini'] <= i < lay['ops_fin']}


def _asignadas(con):
    return {r['op_id']: r for r in con.execute('SELECT * FROM estrategia_asignada')}


def test_fila_y_desplegable_estricto(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    fila = lay['valores'][lay['preop_fila']]
    # la fila de la estrategia, con las 4 de la compuerta (§10.6) entre ella y el encabezado de operaciones
    assert lay['preop_fila'] == lay['ops_header'] - 1 - lay['compuerta_n'] and fila[0] == 'Estrategia a usar'
    assert fila[vista.OC['Estrategia']] == 'Sin estrategia'
    assert almacen.get_estado(con, 'preop_celda') == vista.celda_a1(lay['preop_fila'], vista.OC['Estrategia'])
    h = sheets.HojaV4.__new__(sheets.HojaV4)               # solo los formatos, sin API
    h.R, h.tab_id, h.filas = ent.reglas, 1, 500
    validaciones = [r['setDataValidation'] for r in h._formatos(lay) if 'setDataValidation' in r]
    assert len(validaciones) == 3                          # estrategia, estado del mercado (§12) y compuerta (§10.6); la columna Estrategia de las operaciones no
    rango = validaciones[0]['range']
    assert (rango['startRowIndex'], rango['endRowIndex']) == (lay['preop_fila'], lay['preop_fila'] + 1)
    assert (rango['startColumnIndex'], rango['endColumnIndex']) == (vista.OC['Estrategia'], vista.OC['Estrategia'] + 1)
    assert [v['userEnteredValue'] for v in validaciones[0]['rule']['condition']['values']] == lay['estrategias']
    assert validaciones[0]['rule']['strict'] is True       # solo valores de la lista


def test_celda_a1_ida_y_vuelta():
    for a1, fc in (('A1', (0, 0)), ('I11', (10, 8)), ('AA1', (0, 26)), ('AC500', (499, 28))):
        assert vista.celda_a1(*fc) == a1 and vista.celda_de_a1(a1) == fc


def test_leer_preoperativa_y_pestana_antigua(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    v = [list(r) for r in lay['valores']]
    v[lay['preop_fila']][lay['preop_col']] = X
    assert vista.leer_preoperativa(v, ent.reglas) == X
    del v[lay['preop_fila']]                               # layout anterior: sin fila de preoperativa
    assert vista.leer_preoperativa(v, ent.reglas) is None
    manual, _, ok = vista.parsear_hoja(lay['valores'])     # la fila nueva no se confunde con BAL ni operaciones
    assert ok and 'Estrategia a usar' not in manual and len(manual) == lay['ops_fin'] - lay['ops_ini']


def test_momento_de_eleccion_es_la_lectura_anterior(entorno_hoy):
    ent, con, s = entorno_hoy
    s.observar_preoperativa('Sin estrategia', 'T1')
    s.observar_preoperativa(X, 'T2')
    assert almacen.get_estado(con, 'preop_estrategia') == X and almacen.get_estado(con, 'preop_t_elegida') == 'T1'
    s.observar_preoperativa(X, 'T3')                       # sigue igual: no cambia el momento
    assert almacen.get_estado(con, 'preop_t_elegida') == 'T1'
    s.observar_preoperativa(OTRA, 'T4')                    # cambia de idea: cuenta desde la lectura anterior
    assert almacen.get_estado(con, 'preop_t_elegida') == 'T3'
    s.observar_preoperativa('sin ESTRATEGIA', 'T5')        # el defecto, escrito como sea: desarmada
    assert almacen.get_estado(con, 'preop_estrategia') == 'Sin estrategia'
    assert almacen.get_estado(con, 'preop_t_elegida') == ''


def test_asigna_a_las_nuevas_marca_tardias_sin_rojo_y_resetea(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    ds = _decisiones(res)
    tol = ent.reglas['preoperativa']['tolerancia_tardia_s']
    # tardía: una decisión SIN cortafuegos que dure lo bastante para elegir dentro de ella, pasada la tolerancia
    larga = next(f for f in reversed(ds) if not f.cortafuegos and f.duracion >= tol + 5)
    ultima = ds[-1] if ds[-1] is not larga else ds[-2]
    elegida = _ts(larga.t_apertura_utc, seconds=tol + 1)
    assert larga.t_cierre_utc >= elegida and ultima.t_cierre_utc >= elegida
    _como_nuevas(con, larga, ultima)
    _elegir(s, lay, X, elegida)
    res, lay = s.sync(AHORA)
    filas, O, asig = _filas(lay), ent.reglas['observaciones'], _asignadas(con)
    for f in (larga, ultima):
        i, r = filas[f.op_id]
        tardia = fuentes.parse_ts(elegida) > fuentes.parse_ts(f.t_apertura_utc) + timedelta(seconds=tol)
        assert r[vista.OC['Estrategia']] == X
        assert (O['estrategia_tardia'] in r[vista.OC['Observaciones demonio']]) == tardia
        assert asig[f.op_id]['estrategia'] == X and asig[f.op_id]['tardia'] == int(tardia)
        assert asig[f.op_id]['t_elegida_utc'] == elegida
    i, r = filas[larga.op_id]
    assert O['estrategia_tardia'] in r[vista.OC['Observaciones demonio']]
    assert (i, vista.OC['Observaciones demonio']) not in lay['celdas_cortafuegos']   # la marca no pone rojo
    for f in ds:                                           # las ya anotadas no se tocan
        if f not in (larga, ultima):
            assert filas[f.op_id][1][vista.OC['Estrategia']] == 'Sin estrategia'
    assert lay['valores'][lay['preop_fila']][lay['preop_col']] == 'Sin estrategia'   # reset
    assert almacen.get_estado(con, 'preop_estrategia') == 'Sin estrategia'
    # la marca y la estrategia sobreviven a otra escritura; la columna sigue siendo editable a mano
    j = next(k for k, r in enumerate(s.hoja.valores) if r[0] == ultima.op_id)
    s.hoja.valores[j][vista.OC['Estrategia']] = OTRA
    _, lay = s.sync(AHORA)
    filas = _filas(lay)
    assert filas[ultima.op_id][1][vista.OC['Estrategia']] == OTRA
    assert O['estrategia_tardia'] in filas[larga.op_id][1][vista.OC['Observaciones demonio']]


def test_solo_las_nuevas_aunque_las_escritas_cerraran_despues(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    ds = _decisiones(res)
    _como_nuevas(con, ds[-1])
    _elegir(s, lay, X, _ts(ds[0].t_apertura_utc, hours=-1))    # antes de todo: todas cerraron después
    _, lay = s.sync(AHORA)
    assert set(_asignadas(con)) == {ds[-1].op_id}
    assert all(_filas(lay)[f.op_id][1][vista.OC['Estrategia']] == 'Sin estrategia' for f in ds[:-1])


def _pc(ent, con, t_exch_utc):
    """Lectura del reloj del PC que corresponde a una hora del exchange (reloj ATAS del fixture: log − exchange
    = −0,699 s el 22/09). Los bordes son en hora del exchange; la lectura de la celda, con el reloj del PC (28/09)."""
    rel = reloj.cargar(con, ent.reglas)
    return fuentes.fmt_ts(fuentes.madrid_a_utc(fuentes.parse_ts(rel.utc_a_log(t_exch_utc))))


def test_bordes_cierre_igual_a_eleccion_y_tolerancia(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    f = _decisiones(res)[-1]
    tol = ent.reglas['preoperativa']['tolerancia_tardia_s']
    _como_nuevas(con, f)
    _elegir(s, lay, X, _pc(ent, con, f.t_cierre_utc))      # cerró justo en el momento de elección: la recibe
    s.sync(AHORA)
    assert _asignadas(con)[f.op_id]['estrategia'] == X
    assert _asignadas(con)[f.op_id]['t_elegida_exch_utc'] == f.t_cierre_utc
    tardia_esperada = f.duracion > tol
    assert _asignadas(con)[f.op_id]['tardia'] == int(tardia_esperada)
    con.execute('DELETE FROM estrategia_asignada')
    _, lay = s.sync(AHORA)
    _como_nuevas(con, f)
    _elegir(s, lay, X, _pc(ent, con, _ts(f.t_apertura_utc, seconds=tol)))    # justo en la tolerancia: no es tardía
    s.sync(AHORA)
    assert _asignadas(con)[f.op_id]['tardia'] == 0


def test_sin_lectura_previa_no_reparte_a_lo_ya_cerrado(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    _como_nuevas(con, *_decisiones(res))                   # p. ej. base reconstruida: todo parece nuevo
    _elegir(s, lay, X, '')                                 # sin lectura previa: cuenta desde esta lectura
    _, lay = s.sync(AHORA)
    assert _asignadas(con) == {}                           # todo cerró antes: nada recibe X
    assert lay['valores'][lay['preop_fila']][lay['preop_col']] == X    # queda armada para la siguiente
    assert almacen.get_estado(con, 'preop_t_elegida') == AHORA
    assert almacen.get_estado(con, 'preop_eleccion_desconocida') == '1'


def test_sin_lectura_previa_la_siguiente_la_recibe_sin_marca_de_tardia(entorno_hoy):
    """Residuo de la verificación (D4): con el momento desconocido la estrategia no puede quedar armada para
    siempre; la recibe la siguiente decisión y, como no se sabe cuándo se eligió, no se marca tardía."""
    from types import SimpleNamespace
    ent, con, s = entorno_hoy
    s.observar_preoperativa(X, '2026-09-22 20:00:00.000')  # primera lectura de todas, ya con X
    f = SimpleNamespace(es_seguidora=False, op_id='PA16-20260922-099', t_apertura_utc='2026-09-22 19:00:00.000',
                        t_cierre_utc='2026-09-22 20:05:00.000')
    assert s._asignar_estrategia(SimpleNamespace(filas=[f]), '2026-09-22 20:06:00.000') == 'Sin estrategia'
    r = _asignadas(con)[f.op_id]
    assert r['estrategia'] == X and r['tardia'] == 0       # abrió antes de "elegir", pero no se sabe: sin marca


def test_vigilar_sin_hora_explicita_fecha_antes_de_la_peticion(entorno_hoy, monkeypatch):
    from atasv4 import sync as sync_mod
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    reloj = ['2026-09-22 18:10:00.000']
    monkeypatch.setattr(sync_mod, 'ahora_utc', lambda: reloj[0])
    lenta = s.hoja.leer_filas

    def leer_lenta(fila, col, n=1):                        # la respuesta llega 3 s después
        reloj[0] = '2026-09-22 18:10:03.000'
        return lenta(fila, col, n)

    s.hoja.leer_filas = leer_lenta
    s.vigilar_preoperativa()
    assert almacen.get_estado(con, 'preop_t_lectura') == '2026-09-22 18:10:00.000'


def test_escritura_fallida_mantiene_la_eleccion_y_el_reintento_asigna(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    f = _decisiones(res)[-1]
    elegida = _ts(f.t_apertura_utc, hours=-1)
    _como_nuevas(con, f)
    _elegir(s, lay, X, elegida)
    escribir = s.hoja.escribir

    def falla(lay):
        raise ConnectionError('Sheets caído')

    s.hoja.escribir = falla
    try:
        s.sync(AHORA)
    except ConnectionError:
        pass
    assert almacen.get_estado(con, 'preop_estrategia') == X              # sigue armada
    assert almacen.get_estado(con, 'preop_t_elegida') == elegida          # con el momento original
    s.hoja.escribir = escribir
    _, lay = s.sync(AHORA)
    assert _asignadas(con)[f.op_id]['estrategia'] == X and _asignadas(con)[f.op_id]['t_elegida_utc'] == elegida
    assert lay['valores'][lay['preop_fila']][lay['preop_col']] == 'Sin estrategia'
    assert almacen.get_estado(con, 'preop_estrategia') == 'Sin estrategia'


def test_la_lectura_periodica_usa_el_cliente_de_timeout_corto(entorno_hoy):
    ent, con, s = entorno_hoy

    class Cliente:
        def __init__(self, nombre, usados):
            self.nombre, self.usados = nombre, usados

        def spreadsheets(self):
            return self

        def values(self):
            return self

        def get(self, **kw):
            self.usados.append((self.nombre, kw['range']))
            return self

        def execute(self):
            return {'values': [['Estrategia a usar', '', X]]}

    usados = []
    h = sheets.HojaV4.__new__(sheets.HojaV4)
    h.svc, h.svc_rapido = Cliente('lento', usados), Cliente('rapido', usados)
    h.sid, h.q = 'id', "'V4'"
    assert h.leer_filas(8, 2) == [['Estrategia a usar', '', X]]
    assert usados == [('rapido', "'V4'!A9:C9")]
    # la estrategia y las 4 filas de la compuerta (§10.6): UNA petición, con el cliente rápido; siempre 5 filas
    usados.clear()
    assert len(h.leer_filas(8, 2, 5)) == 5
    assert usados == [('rapido', "'V4'!A9:C13")]


def test_sin_decisiones_nuevas_la_celda_se_conserva(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    _elegir(s, lay, X, AHORA)
    _, lay = s.sync(AHORA)
    assert lay['valores'][lay['preop_fila']][lay['preop_col']] == X
    assert _asignadas(con) == {}


def test_decision_cerrada_antes_de_elegir_no_la_recibe(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    ultima = _decisiones(res)[-1]
    _como_nuevas(con, ultima)
    _elegir(s, lay, X, _ts(ultima.t_cierre_utc, seconds=1))     # eligió después de que cerrara
    _, lay = s.sync(AHORA)
    assert _filas(lay)[ultima.op_id][1][vista.OC['Estrategia']] == 'Sin estrategia'
    assert lay['valores'][lay['preop_fila']][lay['preop_col']] == X    # queda armada para la siguiente


def test_las_seguidoras_no_reciben_estrategia_propia(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    lider = next(f for f in reversed(_decisiones(res)) if f.copias)
    _como_nuevas(con, lider, *lider.copias)
    _elegir(s, lay, X, _ts(lider.t_apertura_utc, hours=-1))
    s.sync(AHORA)
    assert lider.op_id in _asignadas(con)
    assert not any(c.op_id in _asignadas(con) for c in lider.copias)


def test_vigilar_lee_la_celda_y_comprueba_la_etiqueta(entorno_hoy):
    ent, con, s = entorno_hoy
    assert s.vigilar_preoperativa('T0') is None            # nada escrito aún
    _, lay = s.sync(AHORA)
    s.hoja.valores[lay['preop_fila']][lay['preop_col']] = X
    assert s.vigilar_preoperativa('T9') == X
    assert almacen.get_estado(con, 'preop_t_elegida') == AHORA   # la última lectura fue la del sync
    s.hoja.valores.insert(lay['preop_fila'], [''] * vista.N_COLS)   # alguien inserta una fila a mano
    assert s.vigilar_preoperativa('T10') is None           # no lee otra celda: espera al siguiente sync
    assert almacen.get_estado(con, 'preop_t_lectura') == 'T9'


class _Falso:
    def __init__(self, preop):
        self.preop, self.ingerir_falla = preop, False

    def ingerir(self):
        if self.ingerir_falla:
            raise OSError('ATAS reescribiendo')
        return 0

    def vigilar(self, avisar):
        class R:
            filas_abiertas = []
        return R()

    def pendiente(self):
        return False

    def vigilar_preoperativa(self):
        return self.preop()


def test_ciclo_intervalo_pausa_tras_error_y_un_log_por_minuto(entorno_hoy, monkeypatch):
    ent, con, s = entorno_hoy
    reloj, llamadas, logs = [1000.0], [], []
    monkeypatch.setattr(demonio_v4.time, 'time', lambda: reloj[0])

    def falla():
        llamadas.append(reloj[0])
        raise ConnectionError('sin red')

    c = demonio_v4.Ciclo(ent, con, _Falso(falla), logs.append, avisar=lambda *a: None)
    c.paso()
    reloj[0] += 2
    c.paso()                                               # dentro del intervalo: no vuelve a leer
    reloj[0] += 10
    c.paso()                                               # pasó el intervalo, pero sigue en pausa tras el error
    assert len(llamadas) == 1
    reloj[0] += 30
    c.paso()                                               # acabada la pausa: reintenta
    assert len(llamadas) == 2
    assert sum(1 for m in logs if m.startswith('[PREOPERATIVA ERROR]')) == 1   # un solo log por minuto


def test_sin_cambios_no_se_registra_en_cada_lectura(entorno_hoy, monkeypatch):
    """Con el estado aún sin guardar, leer 'Sin estrategia' no es un cambio: no debe llenar el log (28/09)."""
    ent, con, s = entorno_hoy
    reloj, logs = [1000.0], []
    monkeypatch.setattr(demonio_v4.time, 'time', lambda: reloj[0])
    c = demonio_v4.Ciclo(ent, con, _Falso(lambda: ent.reglas['estrategia_por_defecto']), logs.append,
                         avisar=lambda *a: None)
    for _ in range(5):
        c.paso()
        reloj[0] += 10
    assert not [m for m in logs if m.startswith('[PREOPERATIVA]')]


def test_la_lectura_no_depende_de_que_la_ingesta_funcione(entorno_hoy):
    ent, con, s = entorno_hoy
    llamadas, logs = [], []
    f = _Falso(lambda: llamadas.append(1))
    f.ingerir_falla = True
    demonio_v4.Ciclo(ent, con, f, logs.append, avisar=lambda *a: None).paso()
    assert logs[0].startswith('[CICLO ERROR] OSError') and llamadas == [1]


def test_demos_de_atas_son_sim(entorno_hoy):
    ent, con, s = entorno_hoy
    assert ent.es_sim('DEMO0BBE0') and ent.es_sim('CRYPTO007D1') and ent.es_sim('fsern2776_sim')
    assert not ent.es_sim('PA-APEX-91120-16') and not ent.es_sim('APEX-91120-75')
