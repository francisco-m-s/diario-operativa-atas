"""Tanda del 11/11/2026 (orden del operador): avisos apagados, inactividad de la E2T real (spec §10.1), red en un hilo auxiliar
con el cortafuegos corriendo (Ciclo.corredor), escritura de la pestaña en una petición, ciclos archivados (spec §15), log sin
repeticiones, purga del cortafuegos y escrituras de SQLite que sobraban."""

import threading
import time
from datetime import date

import pytest

import demonio_v4
from atasv4 import almacen, aviso, cortafuegos, motor, sheets, sync, vista

AHORA = '2026-09-22 18:00:00.000'            # 20:00 Madrid: sesión del 22/09 ya terminada


# ---------------------------------------------------------------------------
# Avisos: ni sonido ni notificación
# ---------------------------------------------------------------------------

def test_reglas_sin_sonido_ni_notificacion_no_lanzan_nada(entorno_hoy, monkeypatch):
    R = entorno_hoy[0].reglas
    A = R['cortafuegos']['aviso']
    assert not A.get('sonido') and A.get('notificacion') is False
    lanzado = []
    monkeypatch.setattr(aviso, 'notificar', lambda *a, **k: lanzado.append(a))
    monkeypatch.setattr(aviso.subprocess, 'Popen', lambda *a, **k: lanzado.append(a))
    monkeypatch.setattr(aviso.winsound, 'PlaySound', lambda *a, **k: lanzado.append(a))
    aviso.avisador(R)(A['titulo'], 'prueba')
    assert lanzado == []


def test_notificar_sin_toast_no_abre_powershell(monkeypatch):
    lanzado = []
    monkeypatch.setattr(aviso.subprocess, 'Popen', lambda *a, **k: lanzado.append(a))
    aviso.notificar('t', 'x', None, toast=False)
    assert lanzado == []


# ---------------------------------------------------------------------------
# Inactividad de la E2T real (spec §10.1): 5 días hábiles seguidos sin operar; cualquier operación cuenta
# ---------------------------------------------------------------------------

REGLA = {'limite_dias': 5, 'aviso_desde': 3, 'estados': {'ok': 'OK', 'aviso': 'AVISO', 'ultimo': 'ÚLTIMO DÍA', 'vencido': 'VENCIDO'},
         'formato': 'día {n} de {max} sin operar · operar como tarde el {limite} · {estado}'}


@pytest.mark.parametrize('hoy, esperado', [
    (date(2026, 11, 5), 'día 0 de 5 sin operar · operar como tarde el 12/11 · OK'),          # operó hoy (jueves)
    (date(2026, 11, 6), 'día 1 de 5 sin operar · operar como tarde el 12/11 · OK'),
    (date(2026, 11, 7), 'día 1 de 5 sin operar · operar como tarde el 12/11 · OK'),          # sábado: no cuenta
    (date(2026, 11, 9), 'día 2 de 5 sin operar · operar como tarde el 12/11 · OK'),          # lunes
    (date(2026, 11, 10), 'día 3 de 5 sin operar · operar como tarde el 12/11 · AVISO'),
    (date(2026, 11, 11), 'día 4 de 5 sin operar · operar como tarde el 12/11 · AVISO'),
    (date(2026, 11, 12), 'día 5 de 5 sin operar · operar como tarde el 12/11 · ÚLTIMO DÍA'),
    (date(2026, 11, 13), 'día 6 de 5 sin operar · operar como tarde el 12/11 · VENCIDO'),
])
def test_inactividad_e2t_cuenta_dias_habiles_desde_la_ultima_operacion(hoy, esperado):
    assert motor.inactividad_firma(REGLA, {date(2026, 11, 5)}, date(2026, 11, 5), hoy) == esperado


def test_inactividad_e2t_sin_operaciones_cuenta_desde_el_alta():
    # alta el jueves 05/11 sin operar nunca: el 05 es el día 1 y el límite es el miércoles 11
    assert motor.inactividad_firma(REGLA, set(), date(2026, 11, 5), date(2026, 11, 5)) == \
        'día 1 de 5 sin operar · operar como tarde el 11/11 · OK'


def test_inactividad_e2t_solo_para_la_firma_con_norma_propia(entorno_hoy):
    R = entorno_hoy[0].reglas
    assert set(R['inactividad_firma']) == {'E2T'} and R['inactividad_firma']['E2T']['limite_dias'] == 5


# ---------------------------------------------------------------------------
# Red en un hilo auxiliar: el cortafuegos sigue corriendo mientras Sheets contesta
# ---------------------------------------------------------------------------

class _S:
    def __init__(self):
        self.ingestas = 0

    def ingerir(self):
        self.ingestas += 1
        return 0

    def vigilar(self, avisar):
        class R:
            filas_abiertas = []
        return R()

    def pendiente(self):
        return False

    def vigilar_preoperativa(self):
        return None


def _ciclo(entorno_hoy, intervalo=0.05):
    ent, con, _ = entorno_hoy
    ent.cfg['intervalo_s'] = intervalo
    s, logs = _S(), []
    return demonio_v4.Ciclo(ent, con, s, logs.append, avisar=lambda *a: None), s, logs


def test_corredor_sigue_dando_ticks_mientras_la_peticion_no_vuelve(entorno_hoy):
    c, s, _ = _ciclo(entorno_hoy)
    hilos = []

    def lenta():
        hilos.append(threading.current_thread())
        time.sleep(0.4)
        return 'hecho'

    assert c.corredor(lenta, 5) == 'hecho'
    assert s.ingestas >= 3                                   # el cortafuegos no se ha parado durante la petición
    assert hilos[0] is not threading.current_thread()        # la petición fue en otro hilo; SQLite, en este


def test_corredor_abandona_al_vencer_el_plazo_de_reloj_de_pared(entorno_hoy):
    c, s, _ = _ciclo(entorno_hoy)
    suelta = threading.Event()
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        c.corredor(lambda: suelta.wait(10), 0.3)
    suelta.set()
    assert time.monotonic() - t0 < 2


def test_corredor_propaga_el_error_de_la_peticion(entorno_hoy):
    c, _, _ = _ciclo(entorno_hoy)

    def rota():
        raise ConnectionError('sin red')

    with pytest.raises(ConnectionError):
        c.corredor(rota, 5)


def test_un_fill_que_llega_durante_la_escritura_no_se_da_por_sincronizado(entorno_hoy):
    """La foto de las cuentas es la del cálculo que se escribe: si durante la petición a Sheets se ingiere algo más, queda
    pendiente para el sync siguiente."""
    ent, con, s = entorno_hoy
    s.sync(AHORA)
    assert not s.pendiente(AHORA)
    escribir = s.hoja.escribir

    def escribir_con_fill(lay):                 # el «tick» que corre mientras Sheets contesta ingiere un fill nuevo
        con.execute("INSERT INTO fills VALUES ('PA-APEX-91120-16', 'NQZ6@CME', '99999001', '1', 'Buy', 25000.0, 1.0, "
                    "'2026-09-22 17:30:00.000')")
        con.execute("INSERT INTO fills VALUES ('PA-APEX-91120-16', 'NQZ6@CME', '99999002', '2', 'Sell', 25001.0, 1.0, "
                    "'2026-09-22 17:30:20.000')")
        con.commit()
        escribir(lay)

    s.hoja.escribir = escribir_con_fill
    s.sync(AHORA)
    s.hoja.escribir = escribir
    assert s.pendiente(AHORA)                   # antes se marcaba como sincronizado sin haberlo escrito


def test_el_ej_de_la_hoja_usa_el_corredor_y_rehace_clientes_tras_un_plazo_vencido():
    h = sheets.HojaV4.__new__(sheets.HojaV4)
    usados, rehechos = [], []

    class Peticion:
        def execute(self):
            return 'ok'

    h.corredor = lambda fn, plazo: (usados.append(plazo), fn())[1]
    h._crear_clientes = lambda: rehechos.append(1)
    assert h._ej(Peticion()) == 'ok' and h._ej(Peticion(), rapido=True) == 'ok'
    assert usados == [90.0, 3.0]

    def vencido(fn, plazo):
        raise TimeoutError('plazo')

    h.corredor = vencido
    with pytest.raises(TimeoutError):
        h._ej(Peticion())
    assert rehechos == [1]


# ---------------------------------------------------------------------------
# Escritura en una petición
# ---------------------------------------------------------------------------

def test_celda_guarda_lo_mismo_que_la_escritura_raw():
    assert sheets._celda('') == {} and sheets._celda(None) == {}
    assert sheets._celda(0) == {'userEnteredValue': {'numberValue': 0}}
    assert sheets._celda(12.5) == {'userEnteredValue': {'numberValue': 12.5}}
    assert sheets._celda(True) == {'userEnteredValue': {'boolValue': True}}
    assert sheets._celda('=1+1') == {'userEnteredValue': {'stringValue': '=1+1'}}      # texto, nunca fórmula
    assert sheets._celda('123') == {'userEnteredValue': {'stringValue': '123'}}


def test_escribir_va_en_una_sola_peticion_con_los_valores_dentro(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    h = sheets.HojaV4.__new__(sheets.HojaV4)
    h.tab_id, h.filas, h.columnas, h.n_cf, h.R, h.sid = 7, 500, vista.N_COLS, 2, ent.reglas, 'id'
    h._meta = lambda: True
    enviadas = []

    class Svc:
        def spreadsheets(self):
            return self

        def values(self):
            raise AssertionError('la escritura ya no usa values().clear ni values().update')

        def batchUpdate(self, spreadsheetId, body):
            enviadas.append(body)

            class P:
                def execute(self):
                    return {}
            return P()

    h.svc = Svc()
    h.escribir(lay)
    assert len(enviadas) == 1
    valores = [r for r in enviadas[0]['requests'] if 'updateCells' in r and 'rows' in r['updateCells']]
    assert len(valores) == 1 and valores[0]['updateCells']['fields'] == 'userEnteredValue'
    rango = valores[0]['updateCells']['range']
    assert rango == {'sheetId': 7, 'startRowIndex': 0, 'startColumnIndex': 0, 'endColumnIndex': vista.N_COLS}
    assert len(valores[0]['updateCells']['rows']) == len(lay['valores'])


# ---------------------------------------------------------------------------
# Ciclos (spec §15)
# ---------------------------------------------------------------------------

def test_archivar_copia_la_pestana_y_la_v4_empieza_ciclo_nuevo(entorno_hoy):
    ent, con, s = entorno_hoy
    res, lay = s.sync(AHORA)
    n = lay['ops_fin'] - lay['ops_ini']
    assert n > 0
    bal_antes = [r[:6] for r in lay['valores'][1:1 + lay['n_bal']]]
    nombre, archivadas = s.archivar_ciclo(AHORA)
    assert archivadas == n and nombre == 'V5 2026-09-22 a 2026-09-22'
    assert s.hoja.duplicadas == [nombre]
    lay2 = s.hoja.escrito
    assert lay2['ops_fin'] == lay2['ops_ini']                         # la pestaña queda sin operaciones…
    assert [r[:6] for r in lay2['valores'][1:1 + lay2['n_bal']]] == bal_antes   # …y el BAL, igual: el cálculo es de toda la historia
    assert len(s.calcular(AHORA).filas) == len(res.filas)             # nada se borra de SQLite
    fila = con.execute('SELECT * FROM ciclos').fetchone()
    assert fila['pestana'] == nombre and fila['decisiones'] == n
    with pytest.raises(ValueError):                                   # sin operaciones nuevas no hay nada que archivar
        s.archivar_ciclo(AHORA)


def test_archivar_conserva_las_columnas_manuales_en_sqlite(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    op = lay['valores'][lay['ops_ini']][vista.OC['ID']]
    s.hoja.valores[lay['ops_ini']][vista.OC['Observaciones']] = 'nota del operador'
    s.archivar_ciclo(AHORA)
    s.sync(AHORA)                                                     # la fila ya no está en la pestaña: no se pierde
    assert vista.cargar_manual(con)[op]['observaciones'] == 'nota del operador'


def test_el_demonio_archiva_cuando_lo_pide_el_cli(entorno_hoy):
    ent, con, s = entorno_hoy
    hechos, logs = [], []

    class S(_S):
        def archivar_ciclo(self):
            hechos.append(1)

    c = demonio_v4.Ciclo(ent, con, S(), logs.append, avisar=lambda *a: None)
    almacen.set_estado(con, 'archivar_ciclo', '1')
    con.commit()
    c.paso()
    c.paso()
    assert hechos == [1] and almacen.get_estado(con, 'archivar_ciclo') == '0'


# ---------------------------------------------------------------------------
# Log sin repeticiones
# ---------------------------------------------------------------------------

def test_los_avisos_del_calculo_y_el_reloj_no_se_repiten_en_cada_sync(entorno_hoy, monkeypatch):
    ent, con, s = entorno_hoy
    logs = []
    s.log = logs.append
    calcular = s.calcular

    def con_aviso(t=None):
        r = calcular(t)
        r.avisos.append('[AVISO] operación incompleta de prueba')
        return r

    monkeypatch.setattr(s, 'calcular', con_aviso)
    for _ in range(3):
        s.sync(AHORA)
    assert sum(1 for m in logs if m.startswith('[AVISO]')) == 1
    assert sum(1 for m in logs if m.startswith('[RELOJ]')) == 1
    assert sum(1 for m in logs if m.startswith('[SYNC]')) == 3


def test_el_reloj_vuelve_al_log_si_el_desfase_se_mueve(entorno_hoy):
    ent, con, s = entorno_hoy
    logs = []
    s.log = logs.append
    res, _ = s.sync(AHORA)
    d = dict(res.reloj.detalle[-1])

    class Rel:
        detalle = [dict(d, desfase_s=d['desfase_s'] + 0.05)]

    s._registrar_reloj(Rel)                       # 0,05 s: por debajo de reloj_log_min_s
    assert sum(1 for m in logs if m.startswith('[RELOJ]')) == 1
    Rel.detalle = [dict(d, desfase_s=d['desfase_s'] + 0.5)]
    s._registrar_reloj(Rel)
    assert sum(1 for m in logs if m.startswith('[RELOJ]')) == 2


# ---------------------------------------------------------------------------
# Purga y escrituras que sobraban
# ---------------------------------------------------------------------------

def test_purga_borra_las_infracciones_que_el_calculo_ya_no_da(entorno_hoy):
    ent, con, s = entorno_hoy
    s.sync(AHORA)
    buenas = con.execute('SELECT COUNT(*) FROM cortafuegos_saltados').fetchone()[0]
    assert buenas > 0
    con.execute("INSERT INTO cortafuegos_saltados (decision_id, cuenta, tipo, hora_evento) VALUES "
                "('XX-20260101-001', 'PA-APEX-91120-16', 'SIN SL', '2026-01-01 10:00:00.000')")
    con.execute("INSERT INTO avisos_emitidos VALUES ('SIN SL|cuenta|fill-que-no-existe', '2026-01-01 10:00:00.000', 'x', 0)")
    con.commit()
    avisos = con.execute('SELECT COUNT(*) FROM avisos_emitidos').fetchone()[0]
    logs = []
    s.log = logs.append
    s.sync(AHORA)
    assert con.execute('SELECT COUNT(*) FROM cortafuegos_saltados').fetchone()[0] == buenas
    assert con.execute('SELECT COUNT(*) FROM avisos_emitidos').fetchone()[0] == avisos - 1
    assert any(m.startswith('[PURGA]') for m in logs)


def test_la_purga_no_borra_avisos_de_eventos_que_siguen_existiendo(entorno_hoy):
    ent, con, s = entorno_hoy
    s.vigilar(lambda *a: None, AHORA)
    antes = con.execute('SELECT COUNT(*) FROM avisos_emitidos').fetchone()[0]
    assert antes > 0
    res = s.calcular('2026-12-31 12:00:00.000')              # meses después: viejos, pero sus eventos siguen en el cálculo
    assert cortafuegos.purgar_avisos(con, res, '2026-12-31 12:00:00.000', ent.reglas, 7) == 0
    assert con.execute('SELECT COUNT(*) FROM avisos_emitidos').fetchone()[0] == antes


def test_un_ciclo_sin_novedades_no_escribe_en_sqlite(entorno_hoy):
    ent, con, s = entorno_hoy
    s.sync(AHORA)
    s.vigilar(lambda *a: None, AHORA)
    escrituras = []
    con.set_trace_callback(lambda q: escrituras.append(q) if q.split(' ')[0].upper() in
                           ('INSERT', 'UPDATE', 'DELETE', 'COMMIT') else None)
    s.ingerir()
    s.vigilar(lambda *a: None, AHORA)
    s.pendiente(AHORA)
    con.set_trace_callback(None)
    assert escrituras == []


def test_backup_se_lleva_los_shm_y_wal_huerfanos(entorno_hoy):
    import os
    ent, con, s = entorno_hoy
    os.makedirs(ent.backups_dir, exist_ok=True)
    huerfano = os.path.join(ent.backups_dir, 'atas_v4_20260101.sqlite-wal')
    con_copia = os.path.join(ent.backups_dir, 'atas_v4_20260102.sqlite')
    acompanado = con_copia + '-wal'
    for p in (huerfano, con_copia, acompanado):
        open(p, 'w').close()
    sync.backup_diario(ent, con, lambda m: None)
    assert not os.path.exists(huerfano) and os.path.exists(acompanado) and os.path.exists(con_copia)


def test_archivar_hasta_un_dia_deja_lo_posterior_en_el_ciclo_nuevo(entorno_hoy):
    ent, con, s = entorno_hoy
    _, lay = s.sync(AHORA)
    n = lay['ops_fin'] - lay['ops_ini']
    with pytest.raises(ValueError):                                   # nada hasta el 21/09: no se archiva…
        s.archivar_ciclo(AHORA, hasta=date(2026, 9, 21))
    assert con.execute('SELECT COUNT(*) FROM ciclos').fetchone()[0] == 0
    _, lay = s.sync(AHORA)
    assert lay['ops_fin'] - lay['ops_ini'] == n                       # …y la pestaña vuelve a enseñar el ciclo entero
    nombre, archivadas = s.archivar_ciclo(AHORA, hasta=date(2026, 9, 22))
    assert archivadas == n and nombre == 'V5 2026-09-22 a 2026-09-22'
