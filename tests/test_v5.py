"""V5 (grill del 11/11/2026, Q1–Q47; spec §16). Sintético y mínimo, como test_plan4: fills de MyTrade escritos en la SQLite.
Día de prueba: jueves 12/11/2026 (sesión CME >= v5.desde). En noviembre Madrid = UTC+1 y Nueva York = UTC−5: la ventana
09:30–12:00 de NY (15:30–18:00 de Madrid) es 14:30–17:00 UTC."""

import json
import os
from datetime import date

import pytest

import demonio_v4
from atasv4 import almacen, aviso, cortafuegos, motor, v5, vista
from test_apex_pruebas import AP76, D, NQ, PA15, _base, _entorno, _op, _t

V = date(2026, 11, 12)
AHORA = _t(V, '20:00:00')
X = 'Continuación de M204s confirmada por M17'
SEP = ' · '


def _d(con, t_in, t_out, ticks, n, cuenta=AP76, dia=V):
    """Decisión de 1 NQ que mueve `ticks` por contrato (compra a 20.000)."""
    _op(con, cuenta, NQ, 1, 20000, 20000 + ticks * 0.25, _t(dia, t_in), _t(dia, t_out), n)


def _ent(tmp_path, cuentas_=(AP76,)):
    ent, con, s = _entorno(tmp_path, list(cuentas_))
    for c in cuentas_:
        _base(con, c, 25000.0)
    return ent, con, s


def _con_estrategia(con, s, t=AHORA):
    """Da una estrategia a todas las decisiones (como si se hubiera elegido antes de entrar) y vuelve a sincronizar."""
    res, _ = s.sync(t)
    for f in res.filas:
        con.execute('INSERT INTO manual_ops (op_id, estrategia) VALUES (?, ?) ON CONFLICT(op_id) DO UPDATE SET '
                    'estrategia=excluded.estrategia', (f.op_id, X))
    con.commit()
    for i, r in enumerate(s.hoja.valores):          # la hoja también: el sync lee las manuales de ella
        if r and r[0] in {f.op_id for f in res.filas}:
            s.hoja.valores[i][vista.OC['Estrategia']] = X
    return s.sync(t)


def _obs(lay):
    return {r[0]: r[vista.OC['Observaciones demonio']] for r in lay['valores'][lay['ops_ini']:lay['ops_fin']]}


def _por_orden(res):
    return sorted((f for f in res.filas if not f.es_seguidora), key=lambda f: f.t_apertura_utc)


# ---------------------------------------------------------------------------
# Banda ±20 y horario
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('ticks, c', [(20, 'G'), (19.75, 'N'), (0, 'N'), (-19.75, 'N'), (-20, 'P'), (204, 'G'), (-136, 'P')])
def test_banda_de_20_ticks(entorno_hoy, ticks, c):
    assert v5.categoria(ticks, entorno_hoy[0].reglas) == c


@pytest.mark.parametrize('hora_utc, fuera', [('14:29:59', True), ('14:30:00', False), ('16:59:59', False),
                                             ('17:00:00', True), ('23:38:00', True)])
def test_fuera_de_horario_ventana_de_nueva_york(entorno_hoy, hora_utc, fuera):
    assert v5.fuera_de_horario(_t(V, hora_utc), entorno_hoy[0].reglas) is fuera


def test_fuera_de_horario_se_mueve_sola_con_el_cambio_de_hora(entorno_hoy):
    R = entorno_hoy[0].reglas
    # 28/10/2026: España ya está en invierno (UTC+1) y EE. UU. sigue en verano (UTC−4): la apertura son las 14:30 de Madrid
    assert v5.fuera_de_horario('2026-10-28 13:30:00.000', R) is False
    assert v5.fuera_de_horario('2026-10-28 16:30:00.000', R) is True       # 12:30 de NY: ya fuera


# ---------------------------------------------------------------------------
# Consejos del día
# ---------------------------------------------------------------------------

def test_cuatro_ganadoras_sin_perdedora_avisa_y_operar_despues_es_consejo_ignorado(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '14:31:00', '14:32:00', 20, 1)
    _d(con, '14:40:00', '14:41:00', 5, 2)          # neutra: ni cuenta ni lo anula
    _d(con, '14:50:00', '14:51:00', 30, 3)
    _d(con, '15:00:00', '15:01:00', 20, 4)
    _d(con, '15:10:00', '15:11:00', 40, 5)         # 4ª ganadora: aviso
    _d(con, '15:20:00', '15:21:00', 20, 6)         # abierta después: consejo ignorado
    res, lay = _con_estrategia(con, s)
    fs = _por_orden(res)
    assert '4 GANADORAS SIN PERDER: NO OPERAR MÁS HOY' in fs[4].obs and all('4 GANADORAS' not in f.obs for f in fs[:4])
    assert 'CONSEJO IGNORADO: OPERAR TRAS 4 GANADORAS' in fs[5].obs and fs[5].naranja and not fs[5].cortafuegos
    assert not fs[4].naranja and not fs[4].cortafuegos                       # el aviso va como texto, sin color
    assert [f.proceso_ok for f in fs] == [True] * 5 + [False]
    assert fs[5].calificacion == 'Trade Peligroso' and fs[4].calificacion == 'Trade Perfecto'
    fila = next(i for i, r in enumerate(lay['valores']) if r and r[0] == fs[5].op_id)
    assert lay['celdas_naranja'] == [(fila, vista.OC['Observaciones demonio'])] and lay['celdas_cortafuegos'] == []
    assert [tuple(r) for r in con.execute('SELECT decision_id, tipo FROM cortafuegos_saltados')] == \
        [(fs[5].op_id, 'CONSEJO IGNORADO: OPERAR TRAS 4 GANADORAS')]


def test_con_una_perdedora_antes_las_cuatro_ganadoras_no_avisan(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '14:31:00', '14:32:00', -20, 1)
    for i in range(5):
        _d(con, f'15:{i}0:00', f'15:{i}1:00', 25, 2 + i)
    res, _ = s.sync(AHORA)
    assert not any('GANADORAS' in f.obs for f in res.filas)


def test_tres_perdedoras_seguidas_avisan_se_repite_y_no_es_infraccion(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '14:31:00', '14:32:00', -20, 1)
    _d(con, '14:33:00', '14:34:00', -25, 2)
    _d(con, '14:35:00', '14:36:00', -10, 3)        # neutra: ni suma ni rompe
    _d(con, '14:37:00', '14:38:00', -30, 4)        # 3ª perdedora: aviso
    _d(con, '14:39:00', '14:40:00', -20, 5)        # 4ª: se repite
    _d(con, '14:41:00', '14:42:00', 20, 6)         # ganadora: rompe
    _d(con, '14:43:00', '14:44:00', -20, 7)
    res, _ = _con_estrategia(con, s)
    fs = _por_orden(res)
    assert ['PERDEDORAS SEGUIDAS' in f.obs for f in fs] == [False, False, False, True, True, False, False]
    assert '3 PERDEDORAS SEGUIDAS: PARAR Y ANALIZAR EL MERCADO' in fs[3].obs and '4 PERDEDORAS SEGUIDAS' in fs[4].obs
    assert not any('CONSEJO IGNORADO' in f.obs for f in fs) and all(f.proceso_ok for f in fs)
    assert con.execute('SELECT COUNT(*) FROM cortafuegos_saltados').fetchone()[0] == 0


def test_407_ticks_perdidos_cuenta_todo_lo_cerrado_en_negativo_y_operar_despues_es_consejo_ignorado(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '14:31:00', '14:32:00', -200, 1)
    _d(con, '14:40:00', '14:41:00', 300, 2)        # el neto del día va en positivo: da igual, cuentan los ticks perdidos
    _d(con, '14:50:00', '14:51:00', -12, 3)        # una neutra en negativo también suma
    _d(con, '15:00:00', '15:01:00', -194, 4)       # 406: todavía no
    _d(con, '15:10:00', '15:11:00', -1, 5)         # 407: aviso
    _d(con, '15:20:00', '15:21:00', 20, 6)
    res, lay = _con_estrategia(con, s)
    fs = _por_orden(res)
    assert ['T PERDIDOS HOY' in f.obs for f in fs] == [False, False, False, False, True, False]
    assert '407 T PERDIDOS HOY: FIN DE SESIÓN' in fs[4].obs
    assert 'CONSEJO IGNORADO: OPERAR TRAS PERDER 407 T' in fs[5].obs and fs[5].naranja
    cols = {r[0]: (r[vista.OC['Nº en el día']], r[vista.OC['Acumulado del día (ticks)']], r[vista.OC['Ticks perdidos del día']])
            for r in lay['valores'][lay['ops_ini']:lay['ops_fin']]}
    assert [cols[f.op_id] for f in fs] == [(1, -200, 200), (2, 100, 200), (3, 88, 212), (4, -106, 406), (5, -107, 407),
                                           (6, -87, 407)]


def test_los_consejos_son_por_sesion_cme_y_no_cruzan_de_dia(tmp_path):
    ent, con, s = _ent(tmp_path)
    for i in range(4):
        _d(con, f'15:{i}0:00', f'15:{i}1:00', 25, 1 + i)
    _d(con, '15:00:00', '15:01:00', 25, 9, dia=date(2026, 11, 13))       # al día siguiente: empieza de cero
    res, _ = _con_estrategia(con, s, _t(date(2026, 11, 13), '20:00:00'))
    fs = _por_orden(res)
    assert 'CONSEJO IGNORADO' not in fs[4].obs and fs[4].n_dia == 1 and fs[4].proceso_ok


# ---------------------------------------------------------------------------
# Infracciones en rojo
# ---------------------------------------------------------------------------

def test_fuera_de_horario_y_sin_estrategia_van_en_rojo_y_son_proceso_incorrecto(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '13:00:00', '13:01:00', 30, 1)         # 14:00 de Madrid: fuera
    _d(con, '15:00:00', '15:01:00', 30, 2)         # dentro
    res, lay = s.sync(AHORA)                       # nadie eligió estrategia
    fs = _por_orden(res)
    assert 'FUERA DE HORARIO' in fs[0].obs and 'FUERA DE HORARIO' not in fs[1].obs
    assert all('SIN ESTRATEGIA' in f.obs and f.cortafuegos and f.proceso_ok is False for f in fs)
    assert len(lay['celdas_cortafuegos']) == 2 and lay['celdas_naranja'] == []
    res, lay = _con_estrategia(con, s)
    fs = _por_orden(res)
    assert not any('SIN ESTRATEGIA' in f.obs for f in fs)
    assert (fs[0].proceso_ok, fs[0].cortafuegos, fs[1].proceso_ok, fs[1].cortafuegos) == (False, True, True, False)
    assert fs[1].calificacion == 'Trade Perfecto' and fs[1].ratio == 1.5      # ratio planeado: +204 / −136


def test_en_la_v5_no_hay_reglas_por_tiempo_ni_sin_sl_ni_espejo_ni_compuerta(tmp_path):
    ent, con, s = _ent(tmp_path)
    for i in range(6):                              # seis decisiones en seis minutos, alternando pérdida y ganancia
        _d(con, f'15:0{i}:00', f'15:0{i}:30', -20 if i % 2 == 0 else 20, 1 + i)
    res, _ = s.sync(AHORA)
    todo = SEP.join(f.obs for f in res.filas)
    for prohibido in ('REENTRADA', 'SOBREOPERACIÓN', 'NO OPERAR HASTA', 'SIN SL', 'SL CANCELADO', 'ESPEJO', 'COMPUERTA',
                      'OPERACIÓN TRAS PÉRDIDA DIARIA'):
        assert prohibido not in todo
    assert all(e['preventivos'] == [] for e in res.cortafuegos.values())


def test_lo_anterior_a_la_v5_conserva_las_reglas_de_la_v4(tmp_path):
    ent, con, s = _ent(tmp_path)
    antes = date(2026, 11, 10)                      # martes, última sesión de la V4
    _d(con, '15:00:00', '15:00:30', -20, 1, dia=antes)
    _d(con, '15:01:00', '15:01:30', -20, 2, dia=antes)
    _d(con, '15:02:00', '15:02:30', -20, 3, dia=antes)
    res, _ = s.sync(_t(antes, '20:00:00'))
    fs = _por_orden(res)
    assert 'REENTRADA TRAS PÉRDIDA' in fs[1].obs and 'SOBREOPERACIÓN 10 MIN' in fs[2].obs
    assert 'NO OPERAR HASTA' in fs[0].obs and not any('PERDEDORAS SEGUIDAS' in f.obs for f in fs)
    assert not any(e.get('avisos') for e in res.cortafuegos.values())


def test_racha_de_la_v5_por_ticks_con_la_banda(tmp_path):
    ent, con, s = _ent(tmp_path)
    for i, t in enumerate((25, 15, 30, -19, -20)):
        _d(con, f'15:{i}0:00', f'15:{i}1:00', t, 1 + i)
    res, _ = s.sync(AHORA)
    assert [f.racha for f in _por_orden(res)] == [1, 1, 2, 2, -1]


# ---------------------------------------------------------------------------
# Avisos en vivo
# ---------------------------------------------------------------------------

class Avisos(list):
    def __call__(self, titulo, texto, sonido=None):
        self.append((titulo, texto, sonido))


def test_avisos_en_vivo_con_su_sonido_y_tambien_en_pruebas(tmp_path):
    ent, con, s = _ent(tmp_path)                    # AP76 es una prueba: en la V4 no notificaba
    for i in range(4):
        _d(con, f'15:{i}0:00', f'15:{i}1:00', 25, 1 + i)
    a = Avisos()
    s.vigilar(a, _t(V, '15:31:30'))                 # 30 s después del cierre de la 4ª
    assert ('ATAS V5', '4 GANADORAS SIN PERDER: NO OPERAR MÁS HOY', 'profit') in a
    assert all(son == 'stoploss' for _, texto, son in a if texto.startswith('INFRACCIÓN'))
    n = len(a)
    s.vigilar(a, _t(V, '15:31:32'))
    assert len(a) == n                              # una sola vez por evento
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (AP76, NQ, '9e', 'E9', 'Buy', 20000, 1, _t(V, '15:40:00')))
    almacen.set_estado(con, 'preop_t_lectura', _t(V, '15:40:03'))      # la casilla se leyó tras abrir: sigue sin estrategia
    con.commit()                                    # abre otra, sin estrategia y tras el aviso
    s.vigilar(a, _t(V, '15:40:05'))
    nuevos = {texto.split(' (')[0]: son for _, texto, son in a[n:]}
    assert nuevos == {'INFRACCIÓN: CONSEJO IGNORADO: OPERAR TRAS 4 GANADORAS': 'stoploss',
                      'INFRACCIÓN: SIN ESTRATEGIA': 'stoploss'}


def test_un_evento_viejo_de_la_v5_se_registra_sin_avisar(tmp_path):
    ent, con, s = _ent(tmp_path)
    for i in range(4):
        _d(con, f'15:{i}0:00', f'15:{i}1:00', 25, 1 + i)
    a = Avisos()
    s.vigilar(a, AHORA)                             # horas después: arranque del demonio con el día ya hecho
    assert a == [] and con.execute('SELECT COUNT(*), SUM(notificado) FROM avisos_emitidos').fetchone()[1] == 0


def test_avisador_v5_notificacion_muda_y_wav_de_atas_dos_veces(entorno_hoy, monkeypatch):
    R = entorno_hoy[0].reglas
    llamadas, viejas = [], []
    monkeypatch.setattr(aviso, 'notificar_v5', lambda *a: llamadas.append(a))
    monkeypatch.setattr(aviso, 'notificar', lambda *a, **k: viejas.append(a))
    avisar = aviso.avisador(R)
    avisar('ATAS V5', 'x', sonido='profit')
    avisar('ATAS V5', 'y', sonido='')              # FIN DE TURNO INMINENTE: sin sonido
    avisar('CORTAFUEGOS ATAS', 'evento de la V4')   # apagado desde el 11/11
    carpeta = R['v5']['avisos']['carpeta']
    assert llamadas == [('ATAS V5', 'x', os.path.join(carpeta, 'profit.wav'), 2, True), ('ATAS V5', 'y', None, 2, True)]
    assert viejas == []


def test_notificar_v5_toast_sin_tono_y_el_wav_a_la_cola_de_sonidos(monkeypatch, tmp_path):
    import queue
    cmds, hilos = [], []
    monkeypatch.setattr(aviso.subprocess, 'Popen', lambda cmd, **k: cmds.append(cmd))

    class Hilo:
        def __init__(self, target, args, daemon, name=None):
            hilos.append((target, daemon))

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(aviso.threading, 'Thread', Hilo)
    monkeypatch.setattr(aviso, '_COLA', queue.Queue())
    monkeypatch.setattr(aviso, '_HILO', None)
    wav = tmp_path / 'stoploss.wav'
    wav.write_bytes(b'RIFF')
    aviso.notificar_v5("ATAS 'V5'", 'texto', str(wav), 2)
    aviso.notificar_v5('ATAS V5', 'otro', str(wav), 2)
    assert "SetAttribute('silent', 'true')" in cmds[0][-1] and "ATAS ''V5''" in cmds[0][-1]
    assert hilos == [(aviso._sonador, True)]                                   # un solo hilo para todos los sonidos
    assert [aviso._COLA.get_nowait(), aviso._COLA.get_nowait()] == [(str(wav), 2), (str(wav), 2)]
    aviso.notificar_v5('t', 'x', str(tmp_path / 'no_existe.wav'), 2)          # sin fichero: solo la notificación
    assert len(cmds) == 3 and aviso._COLA.empty()


def test_la_cola_de_sonidos_los_reproduce_de_uno_en_uno_y_en_orden(monkeypatch):
    """Revisión V5, hallazgo 8: con varios avisos a la vez, cada wav suena entero (sus 2 veces) y después el siguiente."""
    import queue
    import threading
    sonando, solapes, orden = [0], [], []

    def reproducir(ruta, veces):
        sonando[0] += 1
        solapes.append(sonando[0])
        orden.append((ruta, veces))
        sonando[0] -= 1

    monkeypatch.setattr(aviso, '_reproducir', reproducir)
    cola = queue.Queue()
    for item in (('stoploss.wav', 2), ('stoploss.wav', 2), ('profit.wav', 2), None):
        cola.put(item)
    hilo = threading.Thread(target=aviso._sonador, args=(cola,))
    hilo.start()
    hilo.join(5)
    assert not hilo.is_alive() and orden == [('stoploss.wav', 2), ('stoploss.wav', 2), ('profit.wav', 2)]
    assert max(solapes) == 1                                                   # nunca dos a la vez


@pytest.mark.parametrize('hora_utc, sale', [('16:54:59', False), ('16:55:00', True), ('16:59:59', True), ('17:00:00', False)])
def test_aviso_de_fin_de_turno_a_las_17_55_de_madrid(entorno_hoy, hora_utc, sale):
    r = v5.aviso_fin_turno(_t(V, hora_utc), entorno_hoy[0].reglas)
    assert (r == ('2026-11-12', 'FIN DE TURNO INMINENTE')) is sale and (r is None) is not sale


def test_el_demonio_avisa_del_fin_de_turno_una_vez_y_solo_con_turno_abierto(entorno_hoy, monkeypatch):
    ent, con, s = entorno_hoy
    a, logs = Avisos(), []
    monkeypatch.setattr(demonio_v4.sync, 'ahora_utc', lambda: _t(V, '16:56:00'))
    c = demonio_v4.Ciclo(ent, con, s, logs.append, avisar=a)
    c.turno()
    assert a == []                                  # sin turno abierto (tests, --oneshot, CLI) no avisa
    c.abrir_turno()
    c.turno()
    c.turno()
    assert a == [('ATAS V5', 'FIN DE TURNO INMINENTE', '')]
    c.cerrar_turno()
    assert almacen.turnos(con)[-1][1] is not None


# ---------------------------------------------------------------------------
# Turnos y recordatorio
# ---------------------------------------------------------------------------

def test_turnos_numerados_por_dia_con_su_primera_operacion_y_turno_fantasma(tmp_path):
    ent, con, s = _ent(tmp_path)
    for ini, fin in (('14:00:00', '14:20:00'),      # turno sin operaciones: no consume número
                     ('14:25:00', '15:15:00'), ('15:30:00', '16:00:00')):
        t = almacen.abrir_turno(con, _t(V, ini))
        almacen.cerrar_turno(con, t, _t(V, fin))
    _d(con, '14:31:00', '14:32:00', 20, 1)          # T1
    _d(con, '15:00:00', '15:01:00', 20, 2)          # T1
    _d(con, '15:20:00', '15:21:00', 20, 3)          # demonio apagado: fantasma
    _d(con, '15:40:00', '15:41:00', 20, 4)          # T2
    res, lay = s.sync(AHORA)
    assert [f.turno for f in _por_orden(res)] == ['T1', 'T1', 'turno fantasma', 'T2']
    guardado = [json.loads(r['datos'])['turno'] for r in con.execute('SELECT datos FROM operaciones ORDER BY t_apertura_utc')]
    assert guardado == ['T1', 'T1', 'turno fantasma', 'T2']
    assert not any('T1' == c or 'turno fantasma' == c for r in lay['valores'] for c in r)      # no se muestra en la hoja


def test_un_turno_colgado_acaba_en_su_ultimo_latido(tmp_path):
    ent, con, s = _ent(tmp_path)
    t = almacen.abrir_turno(con, _t(V, '14:00:00'))
    almacen.latido_turno(con, t, _t(V, '14:10:00'))
    almacen.abrir_turno(con, _t(V, '15:00:00'))     # arranque nuevo sin haber cerrado el anterior
    assert almacen.turnos(con) == [(_t(V, '14:00:00'), _t(V, '14:10:00')), (_t(V, '15:00:00'), None)]


def test_recordatorio_de_inicio_de_turno_hasta_la_primera_operacion_anotada(tmp_path):
    ent, con, s = _ent(tmp_path)
    _, lay = s.sync(_t(V, '14:00:00'))
    assert lay['recordatorio_fila'] is None         # sin turno (CLI): no hay recordatorio
    s.turno_inicio = _t(V, '14:25:00')
    _, lay = s.sync(_t(V, '14:26:00'))
    fila = lay['valores'][lay['recordatorio_fila']]
    assert fila[1] == 'SUEÑO ≥ 6 H  ·  PLAN LEÍDO HOY  ·  NI ENFERMO NI CON CONFLICTO PERSONAL ABIERTO' and fila[0] == ''
    assert lay['recordatorio_fila'] == lay['mercado_fila'] - 1 and lay['compuerta_n'] == 0
    assert vista.parsear_hoja(lay['valores'])[2] is True                     # la hoja se sigue leyendo igual
    assert vista.leer_preoperativa(lay['valores'], ent.reglas) == 'Sin estrategia'
    _d(con, '14:31:00', '14:32:00', 20, 1)
    _, lay = s.sync(_t(V, '14:33:00'))
    assert lay['recordatorio_fila'] is None


# ---------------------------------------------------------------------------
# Objetivos semanales e histórico unificado
# ---------------------------------------------------------------------------

def test_objetivos_semanales_de_la_v5_tres_recuentos_y_sin_columna_manual(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '13:00:00', '13:01:00', 30, 1)          # fuera de horario
    _d(con, '15:00:00', '15:01:00', 30, 2)
    _d(con, '15:10:00', '15:11:00', 30, 3)
    res, lay = s.sync(AHORA)                        # las tres sin estrategia
    semana = next(r for r in lay['valores'] if r and str(r[0]).startswith('SEMANA'))
    assert semana[1] == '3 decisiones · 1) sin estrategia 3 ✘ · 2) infracciones 1 ✘ · 3) proceso ok 0 % ✘ · cumplidos 0/3'
    res, lay = _con_estrategia(con, s)
    semana = next(r for r in lay['valores'] if r and str(r[0]).startswith('SEMANA'))
    assert semana[1] == '3 decisiones · 1) sin estrategia 0 ✔ · 2) infracciones 1 ✘ · 3) proceso ok 66 % ✘ · cumplidos 1/3'
    assert vista.cabecera_objetivos(ent.reglas) == ['Semana', 'Decisiones', '1 Sin estrategia', '2 Infracciones',
                                                    '3 % proceso correcto', 'Cumplidos']


def test_las_semanas_de_la_v4_no_entran_en_el_recuento_de_la_v5(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '15:00:00', '15:01:00', 30, 1, dia=date(2026, 11, 10))
    res, _ = s.sync(_t(date(2026, 11, 10), '20:00:00'))
    assert motor.recuento_semanal(res.filas, {}, set(), ent.reglas) == {}


def test_filas_antiguas_del_unificado_con_las_columnas_de_la_v5(entorno_hoy):
    R = entorno_hoy[0].reglas
    cab = ['ID', 'Sesión', 'Apertura (Madrid)', 'Cuentas', 'Símbolo', 'Ticks', 'Duración (s)', 'Contratos', 'Dirección',
           'Tipo de cierre']
    valores = [cab,
               ['D-20260316-001', '2026-03-16', '2026-03-16 11:44:25', 'c1', 'NQ', 105, 359, 1, 'largo', 'TP'],
               ['D-20260316-002', '2026-03-16', '2026-03-16 12:00:00', 'c1, c2', 'NQ', '-34,5', 81, 2, 'corto', 'SL'],
               ['D-20260317-001', '2026-03-17', '2026-03-17 15:31:00', 'c1', 'NQ', -10, 60, 1, 'largo', 'Mercado']]
    filas = vista.filas_unificado_antiguas(valores, R)
    u = {c: i for i, c in enumerate(vista.UNIFICADO_COLS)}
    assert [r[u['ID']] for r in filas] == ['D-20260317-001', 'D-20260316-002', 'D-20260316-001']      # lo nuevo arriba
    segunda = filas[1]
    assert (segunda[u['Cuenta']], segunda[u['Largo/Corto']], segunda[u['Resultado (ticks)']], segunda[u['Tipo cierre']],
            segunda[u['Origen']]) == ('c1, c2', 'Corto', -34.5, 'SL', 'histórico 16/03–21/09')
    assert (segunda[u['Nº en el día']], segunda[u['Acumulado del día (ticks)']], segunda[u['Ticks perdidos del día']]) == \
        (2, 70.5, 34.5)
    assert segunda[u['Calificación']] == '' and segunda[u['Estrategia']] == ''                       # lo que no había, vacío
    assert all(len(r) == len(vista.UNIFICADO_COLS) for r in filas) and vista.UNIFICADO_COLS[-2:] == ['Origen', 'OrderId']
    assert isinstance(segunda[u['Fecha y hora apertura']], float)


def test_archivar_pasa_el_ciclo_al_unificado_sin_repetir_y_sin_reescribir(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '15:00:00', '15:01:00', 30, 1)
    _d(con, '15:10:00', '15:11:00', -30, 2)
    nombre, n = s.archivar_ciclo(AHORA)
    tab = s.hoja.pestanas['Histórico unificado']
    assert tab[0] == vista.UNIFICADO_COLS and len(tab) == 3 and n == 2 and nombre.startswith('V5 2026-11-12')
    u = {c: i for i, c in enumerate(vista.UNIFICADO_COLS)}
    assert [r[u['Origen']] for r in tab[1:]] == ['V5', 'V5'] and tab[1][u['Resultado (ticks)']] == -30     # lo nuevo arriba
    tab[2][u['Observaciones']] = 'nota mía en el unificado'
    _d(con, '15:20:00', '15:21:00', 30, 3)
    s.archivar_ciclo(AHORA)
    tab = s.hoja.pestanas['Histórico unificado']
    assert len(tab) == 4 and tab[3][u['Observaciones']] == 'nota mía en el unificado'                 # solo añade arriba
    assert s._insertar_unificado([list(tab[1])]) == 0                                               # por ID: no repite


def test_carga_inicial_del_unificado_ciclos_archivados_y_decisiones_antiguas(tmp_path):
    ent, con, s = _ent(tmp_path)
    ent.cfg['sheet_id_antiguo'] = 'HOJA_ANTIGUA_DE_PRUEBA'                   # el real vive en config.local.yaml (V5.1): el test pone el suyo
    _d(con, '15:00:00', '15:01:00', 30, 1, dia=date(2026, 11, 10))           # época V4
    s.archivar_ciclo(_t(date(2026, 11, 10), '20:00:00'))
    s.hoja.pestanas.clear()                                                   # como si el unificado aún no existiera
    _d(con, '15:00:00', '15:01:00', 30, 2)                                    # ciclo en curso: no entra hasta archivarlo
    s.hoja.otra = [['ID', 'Sesión', 'Apertura (Madrid)', 'Ticks'], ['D-20260316-001', '2026-03-16', '2026-03-16 11:44:25', 105]]
    n, antiguas, archivadas = s.cargar_unificado(AHORA)
    tab = s.hoja.pestanas['Histórico unificado']
    u = {c: i for i, c in enumerate(vista.UNIFICADO_COLS)}
    assert (n, antiguas, archivadas) == (2, 1, 1)
    assert [r[u['Origen']] for r in tab[1:]] == ['V4', 'histórico 16/03–21/09']
    assert s.cargar_unificado(AHORA)[0] == 0                                  # repetirla no duplica


# ---------------------------------------------------------------------------
# Foto del cálculo (spec §16.10)
# ---------------------------------------------------------------------------

def test_la_foto_da_exactamente_lo_mismo_que_el_calculo_completo_y_ahorra_consultas(entorno_hoy):
    """Sesión real del 22/09 (con log: SL/TP, MAE/MFE, cortafuegos de la V4): se archiva, se hace la foto y el cálculo con
    foto tiene que ser idéntico al completo, con muchas menos consultas."""
    ent, con, s = entorno_hoy
    T = '2026-09-22 18:00:00.000'
    s.archivar_ciclo(T)                             # archivar ya hace la foto y la comprueba
    assert almacen.get_estado(con, 'foto_version')
    n_filas = con.execute('SELECT COUNT(*) FROM foto_filas').fetchone()[0]
    assert n_filas == len(s.calcular(T).filas) > 0
    assert s.comprobar_foto(T) == []
    consultas = []
    con.set_trace_callback(consultas.append)
    s.calcular(T)
    con_foto = len(consultas)
    consultas.clear()
    s.calcular(T, usar_foto=False)
    con.set_trace_callback(None)
    assert con_foto < len(consultas) * 0.6


def test_una_foto_que_no_coincide_se_descarta_sola(entorno_hoy):
    ent, con, s = entorno_hoy
    T = '2026-09-22 18:00:00.000'
    s.archivar_ciclo(T)
    clave, datos = con.execute('SELECT clave, datos FROM foto_filas LIMIT 1').fetchone()
    malo = json.loads(datos)
    malo['mae'] = -9999.0
    con.execute('UPDATE foto_filas SET datos=? WHERE clave=?', (json.dumps(malo), clave))
    almacen.set_estado(con, 'foto_version', 'tocada a mano')
    con.commit()
    assert s.comprobar_foto(T) != []                # la comprobación lo ve…
    logs = []
    s.log = logs.append
    n, m, difs = s.hacer_foto(T)                    # …y rehacerla la deja bien otra vez
    assert difs == [] and s.comprobar_foto(T) == [] and any(x.startswith('[FOTO]') for x in logs)


def test_lo_nuevo_tras_la_foto_se_calcula_y_cambia_solo_lo_suyo(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '15:00:00', '15:01:00', -30, 1)
    s.archivar_ciclo(AHORA)
    antes = motor.canon(s.calcular(AHORA))
    _d(con, '15:10:00', '15:11:00', 30, 2)          # una operación nueva, después del corte
    despues = motor.canon(s.calcular(AHORA))
    assert s.comprobar_foto(AHORA) == []
    vieja = next(k for k in antes['filas'])
    assert antes['filas'][vieja] == despues['filas'][vieja] and len(despues['filas']) == 2


def test_sin_ciclos_archivados_no_hay_foto(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '15:00:00', '15:01:00', 30, 1)
    assert s.hacer_foto(AHORA) == (0, 0, []) and almacen.foto(con) == {'filas': {}, 'evaluar': {}}


# ---------------------------------------------------------------------------
# Correcciones de la revisión independiente (11/11/2026)
# ---------------------------------------------------------------------------

def _abrir(con, hora, n=9, cuenta=AP76):
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (cuenta, NQ, f'{n}e', f'E{n}', 'Buy', 20000, 1, _t(V, hora)))
    con.commit()


def test_operacion_tras_objetivo_avisa_al_abrir_y_no_repite_al_cerrar(tmp_path):
    ent, con, s = _ent(tmp_path)
    _d(con, '14:31:00', '14:32:00', 310, 1)        # 25.000 + 1.550 − 3,98 de comisión >= 26.500: objetivo alcanzado
    _abrir(con, '14:40:00')
    a = Avisos()
    s.vigilar(a, _t(V, '14:40:05'))
    assert [(t.split(' (')[0], son) for _, t, son in a if 'TRAS OBJETIVO' in t] == \
        [('INFRACCIÓN: OPERACIÓN TRAS OBJETIVO', 'stoploss')]
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (AP76, NQ, '9s', 'S9', 'Sell', 20001, 1, _t(V, '14:41:00')))
    con.commit()
    n = len(a)
    s.vigilar(a, _t(V, '14:41:05'))
    assert not any('TRAS OBJETIVO' in t for _, t, _ in a[n:])           # misma clave al cerrar: no vuelve a sonar
    res, _ = s.sync(AHORA)
    assert 'OPERACIÓN TRAS OBJETIVO' in _por_orden(res)[1].obs and _por_orden(res)[1].cortafuegos


def test_sin_estrategia_al_abrir_espera_a_una_lectura_de_la_casilla_posterior(tmp_path):
    ent, con, s = _ent(tmp_path)
    _abrir(con, '15:00:00')
    almacen.set_estado(con, 'preop_t_lectura', _t(V, '14:59:58'))      # última lectura, 2 s ANTES de abrir
    con.commit()
    a = Avisos()
    s.vigilar(a, _t(V, '15:00:02'))
    assert not any('SIN ESTRATEGIA' in t for _, t, _ in a)              # pudo elegirla en esos 2 s: aún no se sabe
    almacen.set_estado(con, 'preop_t_lectura', _t(V, '15:00:04'))      # lectura posterior: sigue sin estrategia
    con.commit()
    s.vigilar(a, _t(V, '15:00:05'))
    assert [(t.split(' (')[0], son) for _, t, son in a if 'SIN ESTRATEGIA' in t] == [('INFRACCIÓN: SIN ESTRATEGIA', 'stoploss')]


def test_con_la_estrategia_elegida_al_abrir_no_hay_aviso(tmp_path):
    ent, con, s = _ent(tmp_path)
    _abrir(con, '15:00:00')
    almacen.set_estado(con, 'preop_estrategia', X)
    almacen.set_estado(con, 'preop_t_lectura', _t(V, '15:00:04'))
    con.commit()
    a = Avisos()
    s.vigilar(a, _t(V, '15:00:05'))
    assert not any('SIN ESTRATEGIA' in t for _, t, _ in a)


def test_al_arrancar_la_foto_se_comprueba_y_se_rehace_si_las_reglas_cambiaron(tmp_path):
    ent, con, s = _ent(tmp_path)
    for i in range(4):
        _d(con, f'15:{i}0:00', f'15:{i}1:00', 25, 1 + i)
    s.archivar_ciclo(AHORA)
    assert s.verificar_foto(AHORA) == []                          # recién hecha: coincide
    ent.reglas['v5']['banda_ticks'] = 30                          # con la banda nueva, +25 t ya no es ganadora: la foto
    difs = s.verificar_foto(AHORA)                                # guardaba el aviso de las 4 ganadoras
    assert difs and s.comprobar_foto(AHORA) == []                 # vista y rehecha
    res = s.calcular(AHORA)
    assert not any('GANADORAS' in f.obs for f in res.filas)


def test_sin_foto_verificar_no_hace_nada(tmp_path):
    ent, con, s = _ent(tmp_path)
    assert s.verificar_foto(AHORA) == [] and not almacen.get_estado(con, 'foto_version', '')


def test_unificado_antiguo_numera_el_dia_por_apertura_aunque_las_filas_vengan_desordenadas(entorno_hoy):
    R = entorno_hoy[0].reglas
    cab = ['ID', 'Sesión', 'Apertura (Madrid)', 'Ticks']
    valores = [cab, ['B', '2026-03-16', '2026-03-16 12:00:00', -10], ['A', '2026-03-16', '2026-03-16 11:00:00', 30]]
    u = {c: i for i, c in enumerate(vista.UNIFICADO_COLS)}
    filas = {r[u['ID']]: r for r in vista.filas_unificado_antiguas(valores, R)}
    assert (filas['A'][u['Nº en el día']], filas['B'][u['Nº en el día']]) == (1, 2)
    assert filas['B'][u['Acumulado del día (ticks)']] == 20 and filas['B'][u['Ticks perdidos del día']] == 10


def test_n_en_el_dia_cuenta_las_abiertas_y_no_cambia_entre_syncs(tmp_path):
    """Revisión V5, hallazgo 2: A abre antes que B, B cierra antes. B tiene que ser la 2ª ya con A abierta, y seguir siéndolo."""
    ent, con, s = _ent(tmp_path, (AP76, PA15))
    _abrir(con, '15:00:00', n=1, cuenta=AP76)                          # A, abierta
    _d(con, '15:05:00', '15:06:00', 30, 2, cuenta=PA15)               # B, cerrada antes que A
    res = s.calcular(_t(V, '15:07:00'))
    (b,) = res.filas
    assert b.n_dia == 2
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (AP76, NQ, '1s', 'S1', 'Sell', 20005, 1, _t(V, '15:10:00')))
    con.commit()
    res = s.calcular(_t(V, '15:11:00'))
    assert {f.cuenta: f.n_dia for f in res.filas} == {AP76: 1, PA15: 2}
