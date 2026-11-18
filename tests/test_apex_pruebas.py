"""Reglas del 30/09/2026: Apex PA (spec §8), pruebas (spec §9), sonido solo con cuentas reales (spec §4.5), tope del
threshold de las evaluaciones Apex (spec §6) y columnas nuevas del BAL (spec §7.1).

Los casos con datos reales usan fixtures/ATAS_hoy (22/09/2026). El resto son sintéticos mínimos: fills de MyTrade
escritos en la SQLite (sin log: sin telemetría, así que el consumo de margen es ticks × 0,2 salvo MAE manual) y un
balance base. Precios: MNQ 0,50 $/tick y NQ 5 $/tick; comisión por contrato y operación Apex MNQ 1,02, Apex NQ 3,98,
E2T NQ 4,04.
"""

import os
import shutil
from datetime import date, datetime

import pytest

from atasv4 import almacen, config, cortafuegos, cuentas, fuentes, motor, sheets, sync, vista
from conftest import RAIZ, HojaFalsa, filas_por

PA15, PA16 = 'PA-APEX-91120-15', 'PA-APEX-91120-16'
AP76, AP77 = 'APEX-91120-76', 'APEX-91120-77'
E250, BX = 'fsern2776tcp25d152250', 'BX78918-51'
MNQ, NQ = 'MNQZ6@CME', 'NQZ6@CME'
D, D1, D2 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)       # lunes, martes y miércoles


def _ficha(firma, tipo, tam, alta):
    return {'firma': firma, 'tipo': tipo, 'tamano': tam, 'sin_regla': False, 'alta': alta, 'retiros': 0,
            'ancla_inicial': None, 'ancla_estimada': True}


FICHAS = {PA15: _ficha('Apex', 'PA', 25000, '2026-10-05'), PA16: _ficha('Apex', 'PA', 25000, '2026-10-05'),
          AP76: _ficha('Apex', 'evaluacion', 25000, '2026-10-05'), AP77: _ficha('Apex', 'evaluacion', 25000, '2026-10-05'),
          E250: _ficha('E2T', 'TCP', 25000, '2026-10-05'), BX: _ficha('Bulenox', 'evaluacion', 50000, '2026-10-05')}


def _entorno(tmp_path, cuentas_, inicio='2026-10-05', semillas=None, pares=(), margen_inicial=None, comision=None):
    """Entorno sintético: ATAS vacío (sin logs ni cdb), cuentas dadas y, si se piden, semillas.yaml y pares de copia.
    comision: {(firma, símbolo): por_lado} para cuadrar importes exactos en los bordes."""
    base = str(tmp_path / 'base')
    atas = tmp_path / 'ATAS'
    (atas / 'Logs').mkdir(parents=True)
    (atas / 'Database').mkdir()
    os.makedirs(base, exist_ok=True)
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), os.path.join(base, 'reglas.yaml'))
    cfg = config.cargar_yaml(os.path.join(RAIZ, 'config.yaml'), {})
    cfg.update({'atas_dir': str(atas), 'sqlite': os.path.join(base, 't.sqlite'), 'backups_dir': os.path.join(base, 'b'),
                'inicio_sesion': inicio, 'quemadas_iniciales': [], 'logs_respaldo': None})
    config.guardar_yaml(os.path.join(base, 'config.yaml'), cfg)
    config.guardar_yaml(os.path.join(base, 'cuentas.yaml'), {'cuentas': {c: dict(FICHAS[c]) for c in cuentas_},
                                                             'sim_activas': []})
    if semillas is not None:
        config.guardar_yaml(os.path.join(base, 'semillas.yaml'), semillas)
    ent = config.cargar_entorno(base)
    if margen_inicial is not None:
        ent.reglas['margen']['inicial'] = margen_inicial
    for (firma, simbolo), por_lado in (comision or {}).items():
        next(c for c in ent.reglas['comisiones'] if c['firma'] == firma and c['simbolo'] == simbolo)['por_lado'] = por_lado
    con = almacen.conectar(ent.sqlite_path)
    s = sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)
    s.pares_copia = lambda: list(pares)
    return ent, con, s


def _t(d, hms):
    return f"{d.isoformat()} {hms}.000"


def _base(con, cuenta, bal, t='2026-10-04 23:30:00.000'):
    """Balance base (último 'Bal:' plano), antes del inicio de la sesión del 05/10 (00:00 de Madrid)."""
    con.execute('INSERT INTO cuentas_estado VALUES (?, ?, ?)', (cuenta, bal, t))
    con.commit()


def _op(con, cuenta, simbolo, qty, entrada, salida, t_in, t_out, n, lado='Buy'):
    """Operación completa en MyTrade: un fill de entrada y uno de salida (horas UTC)."""
    sal = 'Sell' if lado == 'Buy' else 'Buy'
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (cuenta, simbolo, f'{n}e', f'E{n}', lado, entrada, qty, t_in))
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (cuenta, simbolo, f'{n}s', f'S{n}', sal, salida, qty, t_out))
    con.commit()


def _bal(res, cuenta):
    return {b['cuenta']: b for b in res.bal}[cuenta]


def _mae_manual(con, op_id, mae):
    con.execute('INSERT INTO manual_ops (op_id, mae_manual) VALUES (?, ?)', (op_id, mae))
    con.commit()


class Avisos(list):
    def __call__(self, titulo, texto):
        self.append(texto)


PARADA = '+50 DEL DÍA ALCANZADO: NO OPERAR MÁS HOY'
SEP = ' · '


# ===========================================================================
# Reglas puras
# ===========================================================================

def test_dia_50_umbral_exacto_10_ticks_de_nq(entorno_hoy):
    R = entorno_hoy[0].reglas
    netos = {date(2026, 10, 1): 50.0, date(2026, 10, 2): 49.99, date(2026, 10, 5): 50.004, date(2026, 10, 6): -80.0}
    assert motor.dias_validos(netos, 10, R) == {date(2026, 10, 1), date(2026, 10, 5)}      # la igualdad cuenta
    assert motor.ticks_nq(50, R) == 10 and motor.usd_de_ticks(-75, R) == -375
    assert motor.dias_validos({D: 100.0, D1: 99.99}, 20, R) == {D}                        # Días +100 de la 25K


def test_contador_estado_y_bordes_15_20(entorno_hoy):
    R = entorno_hoy[0].reglas
    dias = [date(2026, 9, 10), date(2026, 9, 14), date(2026, 9, 20)]
    assert motor.base_contador(dias, date(2026, 10, 5), date(2026, 9, 1)) == date(2026, 9, 14)   # 2 últimos: 14 y 20
    assert motor.base_contador(dias, date(2026, 9, 20), date(2026, 9, 1)) == date(2026, 9, 10)   # anteriores a la sesión
    assert motor.base_contador(dias, date(2026, 9, 20), date(2026, 9, 1), incluir=True) == date(2026, 9, 14)
    assert motor.base_contador(dias[:1], date(2026, 10, 5), date(2026, 9, 1)) == date(2026, 9, 10)  # solo uno
    assert motor.base_contador([], date(2026, 10, 5), date(2026, 9, 1)) == date(2026, 9, 1)          # ninguno: alta
    e = lambda n, m, parada=False: motor.estado_inactividad(n, not parada and motor.es_levantado(n, m, R), R)
    # §10.1: desde plan4.desde el margen levantado llega hasta 141 incluido (antes del Plan 4, por debajo de 141)
    assert [e(14, 0), e(15, 0), e(19, 140.9), e(20, 140.9), e(20, 141), e(20, 141.01)] == \
        ['OK', 'DORMANT', 'DORMANT', 'MARGEN LEVANTADO', 'MARGEN LEVANTADO', 'DORMANT']
    assert [motor.es_levantado(20, m, R, nuevo=False) for m in (140.9, 141)] == [True, False]       # la regla de antes
    assert e(25, 0, parada=True) == 'DORMANT'


def test_contratos_por_simbolo_apex_pa(entorno_hoy):
    R = entorno_hoy[0].reglas
    lim = lambda s, m, lev=False: motor.limite_real(s, m, lev, R)
    # §10.1 (desde plan4.desde): MNQ con más de 141 -> 1 (141 y por debajo -> 0); NQ 0 siempre
    assert [lim(MNQ, m) for m in (750, 749.9, 500, 499.9, 141.01, 141, 140.9)] == [3, 2, 2, 1, 1, 0, 0]
    assert [lim(NQ, m) for m in (2000, 141.01, 141, -500)] == [0, 0, 0, 0]
    # y la tabla de antes del Plan 4, para lo que se abrió antes de esa fecha
    old = lambda s, m: motor.limite_real(s, m, False, R, nuevo=False)
    assert [old(MNQ, m) for m in (750, 749.9, 500, 499.9, 141, 140.9)] == [3, 2, 2, 1, 1, 0]
    assert [old(NQ, m) for m in (2000, 141, 140.9, -500)] == [1, 1, 0, 0]
    assert (lim(MNQ, -1500, True), lim(NQ, -1500, True)) == (1, 0)           # margen levantado: 1 MNQ, 0 NQ
    assert lim('ESZ6@CME', 750) == 3                                           # sin tabla propia: la general (§5)
    assert motor.texto_permitidos({'NQ': 1, 'MNQ': 3}, R) == '1 NQ / 3 MNQ'


def test_nq_equivalentes(entorno_hoy):
    R = entorno_hoy[0].reglas
    q = lambda **p: motor.nq_equivalentes(p, R)
    assert [q(NQ=2), q(MNQ=20), q(MNQ=21), q(NQ=1, MNQ=10), q(NQ=1, MNQ=11), q(NQ=3), q(ES=5)] == \
        [2, 2, 2.1, 2, 2.1, 3, 0]


def test_formato_es(entorno_hoy):
    assert [motor.fmt_es(x) for x in (26025, 25817.7, 26803.2, 1447.44, -75, 0)] == \
        ['26.025', '25.817,7', '26.803,2', '1.447,44', '-75', '0']
    assert motor.fmt_es(152.448, 1) == '152,4' and motor.fmt_es(20.000000000000004, 1) == '20'


# ===========================================================================
# Apex PA (spec §8)
# ===========================================================================

SEMILLA_25 = {'cuentas': {PA15: {'ultimos_dias_50': ['2026-09-10', '2026-09-14']}}}   # contador del 05/10: 25


def _levantada(tmp_path, margen=-100):
    """PA-15 con el contador en 25 (semillas del 10 y el 14/09) y el margen por debajo de 141: margen levantado."""
    ent, con, s = _entorno(tmp_path, [PA15], semillas=SEMILLA_25, margen_inicial=margen)
    _base(con, PA15, 25000.0)
    return ent, con, s


def test_parada_del_dia_y_vuelta_del_bloqueo(tmp_path):
    ent, con, s = _levantada(tmp_path)
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)      # +120 t: 58,98 $
    _op(con, PA15, MNQ, 1, 20030, 20027.5, _t(D, '08:10:00'), _t(D, '08:12:00'), 2)    # −10 t: −6,02 $
    res = s.calcular(_t(D, '12:00:00'))
    f1, f2 = filas_por(res, PA15)
    assert f1.levantado and f1.permitidos == 1 and 'CONTRATOS > PERMITIDOS' not in f1.obs    # 1 MNQ con margen −100
    assert f1.obs.endswith(PARADA) and f1.cortafuegos                  # preventiva: celda en rojo
    assert not f2.levantado and f2.permitidos == 0                     # tras la parada vuelve el bloqueo por margen
    assert 'CONTRATOS > PERMITIDOS' in f2.obs and PARADA not in f2.obs
    assert [t for t, _ in cortafuegos.infracciones_de_entrada(f2, ent.reglas)] == ['CONTRATOS > PERMITIDOS']
    b = _bal(res, PA15)
    assert b['inactividad'] == 'día 21 · límite 14/10 · DORMANT'       # recalculado con el día +50 de hoy
    assert b['permitidos'] == '0 NQ / 0 MNQ'


def test_parada_sin_aviso_sonoro_y_el_bloqueo_si_suena(tmp_path):
    ent, con, s = _levantada(tmp_path)
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)
    _op(con, PA15, MNQ, 1, 20030, 20027.5, _t(D, '08:10:00'), _t(D, '08:12:00'), 2)
    a = Avisos()
    s.vigilar(a, _t(D, '08:12:05'))
    assert not [x for x in a if '+50 DEL DÍA' in x]
    assert 'INFRACCIÓN: CONTRATOS > PERMITIDOS (PA-APEX-91120-15 MNQZ6) 1 contratos con 0 permitidos' in a


def test_parada_solo_al_quedar_plana(tmp_path):
    """A (+58,98) cierra con B aún abierta: el día ya pasa de +10 t pero la cuenta no está plana, así que no hay parada
    hasta que cierra B; la etiqueta la lleva B."""
    ent, con, s = _levantada(tmp_path)
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)      # A
    _op(con, PA15, MNQ, 1, 20030, 20030, _t(D, '08:02:00'), _t(D, '08:10:00'), 2)      # B: 0 t, −1,02
    a, b = filas_por(s.calcular(_t(D, '12:00:00')), PA15)
    assert PARADA not in a.obs and b.obs.endswith(PARADA)


def test_suma_lo_maximo_por_operacion_y_140_en_la_sesion_siguiente(tmp_path):
    """Día +50 con el margen levantado: cada operación aporta máx(0, consumo) y la sesión siguiente recibe +140.
    Lectura por operación (instrucción del 30/09/2026 para la ambigüedad de SPEC §8.4)."""
    ent, con, s = _levantada(tmp_path)
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)      # 24 − 80 (MAE manual) = −56
    _op(con, PA15, MNQ, 1, 20030, 20027.5, _t(D, '08:10:00'), _t(D, '08:12:00'), 2)    # −10 × 0,2 = −2
    f1 = filas_por(s.calcular(_t(D, '12:00:00')), PA15)[0]
    _mae_manual(con, f1.op_id, -80)
    res = s.calcular(_t(D, '12:00:00'))
    f1, f2 = filas_por(res, PA15)
    assert (f1.consumo, f2.consumo) == (0.0, 0.0)                      # normal: −56 y −2
    assert f1.margen_tras == f2.margen_tras == -100 and _bal(res, PA15)['margen'] == -100     # normal: −158
    res = s.calcular(_t(D1, '12:00:00'))
    b = _bal(res, PA15)
    assert b['margen'] == 40                                           # −100 + 140 (normal: −158, sin +140)
    # martes: contador recalculado con el día +50 del lunes = 22 (desde el 14/09) y margen 40 -> se levanta otra vez
    assert b['inactividad'] == 'día 22 · límite 14/10 · MARGEN LEVANTADO' and b['permitidos'] == '0 NQ / 1 MNQ'
    _op(con, PA15, MNQ, 1, 20000, 20001, _t(D1, '08:00:00'), _t(D1, '08:01:00'), 3)
    f3 = filas_por(s.calcular(_t(D1, '12:00:00')), PA15)[2]
    assert f3.levantado and f3.permitidos == 1 and 'CONTRATOS > PERMITIDOS' not in f3.obs


def test_dia_que_no_llega_a_50_resta_normal_aunque_hubiera_parada(tmp_path):
    ent, con, s = _levantada(tmp_path)
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)      # +58,98: parada
    _op(con, PA15, MNQ, 1, 20030, 20005, _t(D, '08:10:00'), _t(D, '08:12:00'), 2)      # −100 t: −51,02 -> día 7,96
    f1 = filas_por(s.calcular(_t(D, '12:00:00')), PA15)[0]
    _mae_manual(con, f1.op_id, -80)
    res = s.calcular(_t(D, '12:00:00'))
    f1, f2 = filas_por(res, PA15)
    assert f1.obs.endswith(PARADA) and 'CONTRATOS > PERMITIDOS' in f2.obs
    assert (f1.consumo, f2.consumo) == pytest.approx((-56, -20)) and _bal(res, PA15)['margen'] == pytest.approx(-176)
    b = _bal(s.calcular(_t(D1, '12:00:00')), PA15)
    assert b['margen'] == pytest.approx(-176)                          # consumo < 0: sin +140
    assert b['inactividad'] == 'día 26 · límite 10/10 · MARGEN LEVANTADO'     # el lunes no fue +50


def test_con_margen_normal_un_dia_50_no_para_nada(tmp_path):
    ent, con, s = _entorno(tmp_path, [PA15], semillas=SEMILLA_25)       # margen 500
    _base(con, PA15, 25000.0)
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)
    _op(con, PA15, MNQ, 1, 20030, 20027.5, _t(D, '08:10:00'), _t(D, '08:12:00'), 2)
    res = s.calcular(_t(D, '12:00:00'))
    f1, f2 = filas_por(res, PA15)
    assert not f1.levantado and PARADA not in f1.obs and not f1.cortafuegos
    assert f2.permitidos == 2 and 'CONTRATOS > PERMITIDOS' not in f2.obs
    assert _bal(res, PA15)['margen'] == pytest.approx(500 + 24 - 2)   # §5 sin cambios


def test_nq_en_apex_pa_uno_desde_141(tmp_path):
    ent, con, s = _entorno(tmp_path, [PA15], semillas=SEMILLA_25)
    _base(con, PA15, 25000.0)
    _op(con, PA15, NQ, 2, 20000, 20001, _t(D, '08:00:00'), _t(D, '08:01:00'), 1)
    _op(con, PA15, NQ, 1, 20000, 20001, _t(D, '09:00:00'), _t(D, '09:01:00'), 2)
    f1, f2 = filas_por(s.calcular(_t(D, '12:00:00')), PA15)
    assert cortafuegos.detalle_exceso(f1, ent.reglas) == '2 contratos con 1 permitidos'
    assert cortafuegos.detalle_exceso(f2, ent.reglas) is None


def test_semillas_contador_limite_y_columnas_bal(tmp_path):
    """semillas.yaml real (PA-15: 17 y 21/09, 8 días +100; PA-16: 11 y 21/09, 2) y el estado a 30/09/2026 de
    NORMAS_APEX §3.4: PA-15 día 13, límite 17/10; PA-16 día 19, límite 11/10, dormant."""
    ent, con, s = _entorno(tmp_path, [PA15, PA16], inicio='2026-09-22')
    shutil.copy(os.path.join(RAIZ, 'semillas.yaml'), os.path.join(ent.base_dir, 'semillas.yaml'))
    ent.semillas = config.cargar_yaml(os.path.join(ent.base_dir, 'semillas.yaml'))
    for c in (PA15, PA16):
        ent.cuentas['cuentas'][c]['alta'] = '2026-09-22'
        _base(con, c, 25470.0, '2026-09-21 23:30:00.000')
    res = s.calcular(_t(date(2026, 9, 30), '12:00:00'))
    b15, b16 = _bal(res, PA15), _bal(res, PA16)
    assert (b15['inactividad'], b15['dias_50_30d'], b15['dias_100']) == ('día 13 · límite 17/10 · OK', 2, 8)
    assert (b16['inactividad'], b16['dias_50_30d'], b16['dias_100']) == ('día 19 · límite 11/10 · DORMANT', 2, 2)
    fila = vista.fila_bal(b15)
    assert fila[vista.BC['Últimos 2 días +50 $']] == '17/09/2026 · 21/09/2026'
    assert (fila[vista.BC['Días +50 (30 d)']], fila[vista.BC['Inactividad']], fila[vista.BC['Días +100']],
            fila[vista.BC['Prueba']]) == (2, 'día 13 · límite 17/10 · OK', 8, '')
    assert fila[vista.BC['Contratos permitidos']] == '0 NQ / 3 MNQ'                 # margen 500 + 140 × 6; la columna es el estado actual (§10.1)
    assert fila[vista.BC['Distancia al threshold']] == round((25470 - b15['threshold']) / 5, 2)   # ticks de NQ
    assert fila[vista.BC['Observaciones demonio']] == b15['obs']
    # un día +50 de la V4 (28/09) pasa por delante de las semillas
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(date(2026, 9, 28), '08:00:00'), _t(date(2026, 9, 28), '08:05:00'), 1)
    b15 = _bal(s.calcular(_t(date(2026, 9, 30), '12:00:00')), PA15)
    assert b15['ultimos_validos'] == [date(2026, 9, 21), date(2026, 9, 28)]
    assert (b15['inactividad'], b15['dias_50_30d']) == ('día 9 · límite 21/10 · OK', 3)


def test_semillas_solo_antes_del_inicio_y_ventana_de_30_dias(tmp_path):
    sem = {'cuentas': {PA15: {'ultimos_dias_50': ['2026-09-05', '2026-09-06', '2026-10-05']}}}
    ent, con, s = _entorno(tmp_path, [PA15], semillas=sem)
    _base(con, PA15, 25000.0)
    b = _bal(s.calcular(_t(D, '12:00:00')), PA15)
    assert b['ultimos_validos'] == [date(2026, 9, 5), date(2026, 9, 6)]        # la del 05/10 (>= inicio) no cuenta
    assert b['dias_50_30d'] == 1                                               # 06/09 = hoy − 29; 05/09 = hoy − 30
    assert b['inactividad'] == 'día 30 · límite 05/10 · DORMANT'


def test_semillas_yaml_contenido_y_el_demonio_no_lo_escribe(tmp_path):
    sem = config.cargar_yaml(os.path.join(RAIZ, 'semillas.yaml'))
    assert sem['cuentas'][PA15] == {'ultimos_dias_50': [date(2026, 9, 17), date(2026, 9, 21)], 'dias_100_previos': 8}
    assert sem['cuentas'][PA16] == {'ultimos_dias_50': [date(2026, 9, 11), date(2026, 9, 21)], 'dias_100_previos': 2}
    assert sem['cuentas'][E250] == {'inicio_prueba': date(2026, 9, 21), 'balance_inicial': 25000,
                                    'mejor_dia_previo': 280.76}
    ent, con, s = _entorno(tmp_path, [PA15], semillas=SEMILLA_25)
    p = os.path.join(ent.base_dir, 'semillas.yaml')
    antes = open(p, 'rb').read()
    _base(con, PA15, 25000.0)
    _op(con, PA15, MNQ, 1, 20000, 20030, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)
    s.sync(_t(D, '12:00:00'))
    cuentas.retiro(ent, con, PA15, 10)                                       # reescribe cuentas.yaml
    assert open(p, 'rb').read() == antes


def test_dias_100_desde_el_ultimo_retiro(tmp_path):
    sem = {'cuentas': {PA15: {'dias_100_previos': 8}}}
    ent, con, s = _entorno(tmp_path, [PA15], semillas=sem)
    _base(con, PA15, 25000.0)
    _op(con, PA15, MNQ, 1, 20000, 20025, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)      # +100 t: 48,98
    _op(con, PA15, NQ, 1, 20000, 20002.75, _t(D, '08:10:00'), _t(D, '08:15:00'), 2)    # +11 t: 51,02 -> día 100,00
    assert _bal(s.calcular(_t(D, '12:00:00')), PA15)['dias_100'] == 9                   # la igualdad cuenta
    # retiro en la sesión del martes (00:30 de Madrid): solo cuentan las sesiones posteriores; la semilla ya no
    con.execute("INSERT INTO movimientos (cuenta, t, importe, tipo, nota) VALUES (?, '2026-10-06 00:30:00.000', "
                "-100, 'retiro', '')", (PA15,))
    con.commit()
    assert _bal(s.calcular(_t(D1, '12:00:00')), PA15)['dias_100'] == 0
    _op(con, PA15, MNQ, 1, 20000, 20025, _t(D1, '08:00:00'), _t(D1, '08:05:00'), 3)
    _op(con, PA15, NQ, 1, 20000, 20002.75, _t(D1, '08:10:00'), _t(D1, '08:15:00'), 4)
    _op(con, PA15, MNQ, 1, 20000, 20025, _t(D2, '08:00:00'), _t(D2, '08:05:00'), 5)
    _op(con, PA15, NQ, 1, 20000, 20002.75, _t(D2, '08:10:00'), _t(D2, '08:15:00'), 6)
    assert _bal(s.calcular(_t(D2, '12:00:00')), PA15)['dias_100'] == 1                  # el martes no, el miércoles sí


def test_en_pa_no_se_aplica_la_regla_de_pruebas(tmp_path):
    ent, con, s = _entorno(tmp_path, [PA15])
    _base(con, PA15, 26400.0)
    _op(con, PA15, NQ, 1, 20000, 20005.25, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)    # 26.501,02: sin objetivo
    _op(con, PA15, NQ, 1, 20000, 19981.25, _t(D1, '08:00:00'), _t(D1, '08:05:00'), 2)  # −378,98: sin pérdida diaria
    res = s.calcular(_t(D1, '12:00:00'))
    assert all(not f.etiquetas_cuenta and not f.infracciones_cuenta for f in filas_por(res, PA15))
    assert _bal(res, PA15)['prueba'] is None


# ===========================================================================
# Pruebas (spec §9)
# ===========================================================================

def test_dos_nq_equivalentes_con_mezcla_y_el_margen_no_bloquea(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76], margen_inicial=100)
    _base(con, AP76, 25000.0)
    _op(con, AP76, NQ, 1, 20000, 20001, _t(D, '08:00:00'), _t(D, '08:30:00'), 1)       # A: 1 NQ
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (AP76, MNQ, 'B', 'EB', 'Buy', 20000, 10, _t(D, '08:05:00')))
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (AP76, MNQ, 'C', 'EC', 'Buy', 20000, 1, _t(D, '08:10:00')))
    con.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)', (AP76, MNQ, 'X', 'SX', 'Sell', 20001, 11, _t(D, '08:20:00')))
    con.commit()
    res = s.calcular(_t(D, '12:00:00'))
    a, b, c = filas_por(res, AP76)
    assert (a.neq, b.neq, c.neq) == (1, 2, 2.1)                        # NQ 1 + MNQ 10 = 2; + 1 MNQ = 2,1
    assert a.margen_apertura == 100 and a.permitidos is None           # margen < 141 y 1 NQ: sin infracción
    assert cortafuegos.detalle_exceso(a, ent.reglas) is None and cortafuegos.detalle_exceso(b, ent.reglas) is None
    assert cortafuegos.detalle_exceso(c, ent.reglas) == '2,1 NQ-eq con 2 permitidos'
    assert 'CONTRATOS > PERMITIDOS' in c.obs and 'CONTRATOS > PERMITIDOS' not in a.obs + b.obs
    b76 = _bal(res, AP76)
    # margen informativo: 100 + 3 × (4 t × 0,2), sin multiplicar por contratos
    # la columna del BAL es el estado actual: una prueba de 25K da 1 NQ / 1 MNQ con la distancia lejos (§10.3, sin tener en cuenta el
    # margen); las operaciones de arriba son anteriores a plan4.desde y siguen con los NQ-equivalentes
    assert (b76['margen'], b76['permitidos'], b76['inactividad']) == (pytest.approx(102.4), '1 NQ / 1 MNQ', None)


def test_en_pruebas_no_hay_margen_levantado_ni_parada(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76], margen_inicial=100)
    _base(con, AP76, 25000.0)
    _op(con, AP76, NQ, 1, 20000, 20005, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)       # +20 t: 96,02
    _op(con, AP76, NQ, 1, 20000, 19999.5, _t(D, '08:10:00'), _t(D, '08:12:00'), 2)     # −2 t
    res = s.calcular(_t(D, '12:00:00'))
    f1, f2 = filas_por(res, AP76)
    assert not f1.levantado and PARADA not in f1.obs and f2.consumo == pytest.approx(-0.4)   # resta normal


def test_tope_del_threshold_de_la_evaluacion_apex_es_el_objetivo(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76, PA15, E250, BX])
    for c in (AP76, PA15, E250, BX):
        _base(con, c, 25000.0 if c != BX else 50000.0)
        con.execute('INSERT INTO equity (cuenta, t_log, bal, pnl_abierto, pnl_cerrado) VALUES (?, ?, ?, 0, 0)',
                    (c, '2026-10-05 10:00:00.000', 28000.0 if c != BX else 53000.0))
    con.commit()
    res = s.calcular(_t(D, '12:00:00'))
    thr = {c: _bal(res, c)['threshold'] for c in (AP76, PA15, E250, BX)}
    assert thr == {AP76: 26500, PA15: 25100, E250: 25100, BX: 50100}   # Bulenox sin objetivo: tope de siempre
    assert _bal(res, AP76)['distancia'] == -300                        # (25.000 − 26.500) ÷ 5
    assert _bal(res, BX)['prueba'] == 'SIN REGLA' and _bal(res, BX)['permitidos'] == '2 NQ / 20 MNQ'


@pytest.mark.parametrize('base, alcanza_la_primera', [(26400.0, True), (26398.98, True), (26398.97, False)])
def test_objetivo_alcanzado_e_infraccion_posterior(tmp_path, base, alcanza_la_primera):
    ent, con, s = _entorno(tmp_path, [AP76])
    _base(con, AP76, base)
    _op(con, AP76, NQ, 1, 20000, 20005.25, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)    # +21 t: 101,02
    _op(con, AP76, NQ, 1, 20000, 20000.25, _t(D, '08:10:00'), _t(D, '08:12:00'), 2)    # +1 t: 1,02
    res = s.calcular(_t(D, '12:00:00'))
    f1, f2 = filas_por(res, AP76)
    assert _bal(res, AP76)['prueba'] == 'OBJETIVO ALCANZADO'
    if alcanza_la_primera:                                              # 26.501,02 / 26.500,00 justos
        assert f1.obs.endswith('OBJETIVO ALCANZADO: NO OPERAR MÁS') and f1.cortafuegos
        assert f2.infracciones_cuenta == [('OPERACIÓN TRAS OBJETIVO', 'objetivo alcanzado a las 10:05:00')]
        assert 'OPERACIÓN TRAS OBJETIVO' in f2.obs.split(SEP) and f2.cortafuegos
        cortafuegos.registrar(con, res.filas, res.cortafuegos, ent.reglas)
        assert [tuple(r) for r in con.execute('SELECT decision_id, cuenta, tipo, detalle FROM cortafuegos_saltados')] == \
            [(f2.op_id, AP76, 'OPERACIÓN TRAS OBJETIVO', 'objetivo alcanzado a las 10:05:00')]
        a = Avisos()                                                    # prueba: se registra sin sonar
        s.vigilar(a, _t(D, '08:12:05'))
        assert a == []
        assert con.execute("SELECT notificado FROM avisos_emitidos WHERE texto=?",
                           ('INFRACCIÓN: OPERACIÓN TRAS OBJETIVO (APEX-91120-76 NQZ6) objetivo alcanzado a las '
                            '10:05:00',)).fetchone()[0] == 0
    else:                                                               # 26.499,99: la alcanza la segunda
        assert not f1.etiquetas_cuenta and not f2.infracciones_cuenta
        assert f2.obs.endswith('OBJETIVO ALCANZADO: NO OPERAR MÁS')


def test_columna_prueba_apex_y_bal(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    _base(con, AP76, 25000.0)
    res = s.calcular(_t(D, '12:00:00'))
    fila = vista.fila_bal(_bal(res, AP76))
    assert fila[vista.BC['Prueba']] == 'objetivo 26.500 · faltan 300 t'
    assert fila[vista.BC['Contratos permitidos']] == '1 NQ / 1 MNQ'          # 25K con distancia 200 t >= 75 (§10.3, estado actual)
    assert [fila[vista.BC[c]] for c in ('Días +50 (30 d)', 'Inactividad', 'Días +100')] == ['', '', '']
    assert fila[vista.BC['Distancia al threshold']] == 200               # (25.000 − 24.000) ÷ 5
    lay = vista.construir(res, ent, {}, [])
    hoja = sheets.HojaV4.__new__(sheets.HojaV4)
    hoja.tab_id, hoja.filas, hoja.R = 0, len(lay['valores']) + 50, ent.reglas
    formatos = [r['repeatCell'] for r in hoja._formatos(lay) if 'repeatCell' in r
                and r['repeatCell']['range']['startColumnIndex'] == vista.BC['Distancia al threshold']
                and r['repeatCell']['range']['startRowIndex'] == 1]
    assert formatos[0]['cell']['userEnteredFormat']['numberFormat']['pattern'] == '#,##0.0'


def test_maximo_diario_e2t_y_objetivo_efectivo(tmp_path):
    ent, con, s = _entorno(tmp_path, [E250])
    _base(con, E250, 25500.0)
    _op(con, E250, NQ, 1, 20000, 20027.25, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)    # +109 t: 545 − 4,08 = 540,92 -> 26.040,92
    res = s.calcular(_t(D, '12:00:00'))
    (f1,) = filas_por(res, E250)
    assert f1.obs.endswith('MÁXIMO BAL DE HOY: 26.025 · REDUCIR BAL PARA CUMPLIR LA REGLA DEL 30 %') and f1.cortafuegos
    assert not f1.infracciones_cuenta                                   # aviso: no sanciona
    # mejor día 540,92 -> objetivo efectivo 25.000 + 540,92 ÷ 0,3 = 26.803,07; faltan (26.803,07 − 26.040,92) ÷ 5
    assert _bal(res, E250)['prueba'] == 'objetivo 26.803,07 · faltan 152,4 t · mejor día 108,2 t (máx 105)'


@pytest.mark.parametrize('ticks, etiqueta', [(106, False), (107, True)])
def test_maximo_diario_e2t_estrictamente_por_encima(tmp_path, ticks, etiqueta):
    ent, con, s = _entorno(tmp_path, [E250], comision={('E2T', 'NQ'): 2.5})    # 5 $ por operación: importes exactos
    _base(con, E250, 25500.0)
    _op(con, E250, NQ, 1, 20000, 20000 + ticks * 0.25, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)
    (f1,) = filas_por(s.calcular(_t(D, '12:00:00')), E250)
    assert f1.balance_tras == 25500 + ticks * 5 - 5                   # 26.025,00 justos no es "por encima"
    assert ('MÁXIMO BAL DE HOY: 26.025' in f1.obs) is etiqueta


def test_objetivo_efectivo_e2t_sube_con_el_mejor_dia(tmp_path):
    ent, con, s = _entorno(tmp_path, [E250])
    _base(con, E250, 26250.0)
    _op(con, E250, NQ, 1, 20000, 20027.5, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)     # +110 t: 550 − 4,08 = 545,92 -> 26.795,92
    (f1,) = filas_por(s.calcular(_t(D, '12:00:00')), E250)
    assert 'OBJETIVO ALCANZADO' not in f1.obs                          # ≥ 26.750, pero el efectivo es 26.819,87
    _op(con, E250, NQ, 1, 20000, 20007.5, _t(D1, '08:00:00'), _t(D1, '08:05:00'), 2)   # +30 t: 150 − 4,08 = 145,92 -> 26.941,84
    f1, f2 = filas_por(s.calcular(_t(D1, '12:00:00')), E250)
    assert f2.obs.endswith('OBJETIVO ALCANZADO: NO OPERAR MÁS')


def test_semilla_e2t_mejor_dia_previo(tmp_path):
    sem = {'cuentas': {E250: {'inicio_prueba': '2026-09-21', 'balance_inicial': 25000, 'mejor_dia_previo': 600}}}
    ent, con, s = _entorno(tmp_path, [E250], semillas=sem)
    _base(con, E250, 25500.0)
    assert _bal(s.calcular(_t(D, '12:00:00')), E250)['prueba'] == \
        'objetivo 27.000 · faltan 300 t · mejor día 120 t (máx 105)'
    ent.semillas['cuentas'][E250]['mejor_dia_previo'] = 280.76         # la real: 56 t, no mueve el objetivo
    assert _bal(s.calcular(_t(D, '12:00:00')), E250)['prueba'] == \
        'objetivo 26.750 · faltan 250 t · mejor día 56,2 t (máx 105)'


def test_perdida_diaria_y_operacion_posterior(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP77])
    _base(con, AP77, 25000.0)
    _op(con, AP77, NQ, 1, 20000, 19981.25, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)    # −75 t: −378,98
    _op(con, AP77, NQ, 1, 20000, 20000.25, _t(D, '08:10:00'), _t(D, '08:12:00'), 2)
    _op(con, AP77, NQ, 1, 20000, 20000.25, _t(D1, '08:10:00'), _t(D1, '08:12:00'), 3)  # otro día: nada
    res = s.calcular(_t(D1, '12:00:00'))
    f1, f2, f3 = filas_por(res, AP77)
    assert 'PÉRDIDA DIARIA −75 T: NO OPERAR MÁS HOY' in f1.obs.split(SEP) and f1.cortafuegos   # + la de 612 s
    assert f2.infracciones_cuenta == [('OPERACIÓN TRAS PÉRDIDA DIARIA', 'pérdida diaria a las 10:05:00')]
    assert 'OPERACIÓN TRAS PÉRDIDA DIARIA' in f2.obs and not f3.infracciones_cuenta and not f3.etiquetas_cuenta
    cortafuegos.registrar(con, res.filas, res.cortafuegos, ent.reglas)
    assert sorted(tuple(r) for r in con.execute('SELECT decision_id, tipo FROM cortafuegos_saltados')) == [
        (f2.op_id, 'OPERACIÓN TRAS PÉRDIDA DIARIA'), (f2.op_id, 'REENTRADA TRAS PÉRDIDA')]   # la de siempre, aparte


@pytest.mark.parametrize('ticks, etiqueta', [(-74, True), (-73, False)])
def test_perdida_diaria_umbral_exacto(tmp_path, ticks, etiqueta):
    ent, con, s = _entorno(tmp_path, [AP77], comision={('Apex', 'NQ'): 2.5})   # 5 $ por operación
    _base(con, AP77, 25000.0)
    _op(con, AP77, NQ, 1, 20000, 20000 + ticks * 0.25, _t(D, '08:00:00'), _t(D, '08:05:00'), 1)
    (f1,) = filas_por(s.calcular(_t(D, '12:00:00')), AP77)
    assert f1.neto == ticks * 5 - 5                                    # −375,00 justos: ≤ −75 t
    assert ('PÉRDIDA DIARIA' in f1.obs) is etiqueta


# ===========================================================================
# Sonido solo con cuentas reales (spec §4.5, §9.4)
# ===========================================================================

def test_decision_solo_de_prueba_no_suena_datos_reales(entorno_hoy):
    """22/09 18:41:55: E2T250-002 (prueba) entra tras la pérdida de PA16-006 -> REENTRADA muda; el preventivo de
    PA16-006 (real, cerró a las 18:38:09) sí suena."""
    ent, con, s = entorno_hoy
    a = Avisos()
    t = fuentes.fmt_ts(fuentes.madrid_a_utc(datetime(2026, 9, 22, 18, 41, 55)))
    s.vigilar(a, t)
    assert a == ['NO OPERAR HASTA 18:48:21 (PÉRDIDA: NO OPERAR HASTA 18:48:21)']
    emitidos = {r['texto']: r['notificado'] for r in con.execute('SELECT texto, notificado FROM avisos_emitidos')}
    texto = 'INFRACCIÓN: REENTRADA TRAS PÉRDIDA (fsern2776tcp25d152250 NQZ6). No operar hasta 18:48:21'
    assert emitidos[texto] == 0
    s.vigilar(a, t)                                                    # registrado: no suena más tarde
    assert len(a) == 1


def _dos_decisiones(con, cuentas_, desfase_s=0):
    """Pérdida de 43,98 $ que cierra a las 10:02 de Madrid y reentrada a las 10:05, en cada cuenta a la vez. `desfase_s`:
    segundos de más en cada cuenta sucesiva. Desde el 01/10/2026 (spec §4.1) dos cuentas que abren a la vez, con la misma
    dirección e instrumento, son copia aunque no figuren en CopyTrading.cnf; con más de 1 s de diferencia, no."""
    for i, c in enumerate(cuentas_):
        d = f"{i * desfase_s:02d}"
        _op(con, c, NQ, 1, 20000, 19998, _t(D, f'08:00:{d}'), _t(D, f'08:02:{d}'), 10 + i)
        _op(con, c, NQ, 1, 20000, 20000.25, _t(D, f'08:05:{d}'), _t(D, f'08:06:{d}'), 20 + i)


def test_prueba_lider_con_seguidora_real_suena(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76, PA15], pares=[(AP76, PA15)])
    _dos_decisiones(con, [AP76, PA15])
    a = Avisos()
    res = s.vigilar(a, _t(D, '08:06:30'))
    assert all(f.lider is not None for f in filas_por(res, PA15))      # copias de la prueba
    assert 'INFRACCIÓN: REENTRADA TRAS PÉRDIDA (APEX-91120-76 NQZ6). No operar hasta 10:12:12' in a
    assert 'NO OPERAR HASTA 10:12:12 (PÉRDIDA: NO OPERAR HASTA 10:12:12)' in a


def test_copia_por_simultaneidad_sin_configuracion_suena_si_hay_una_real(tmp_path):
    """Spec §4.1 y §4.5 (01/10/2026): sin ningún par en CopyTrading.cnf, dos cuentas que abren a la vez son una sola
    decisión (la líder es la primera por nombre al empatar) y suena porque una de ellas es real; la copia no avisa aparte."""
    ent, con, s = _entorno(tmp_path, [AP76, PA15])                      # sin pares
    _dos_decisiones(con, [AP76, PA15])
    a = Avisos()
    res = s.vigilar(a, _t(D, '08:06:30'))
    assert all(f.lider is not None for f in filas_por(res, PA15))
    assert 'INFRACCIÓN: REENTRADA TRAS PÉRDIDA (APEX-91120-76 NQZ6). No operar hasta 10:12:12' in a
    assert 'NO OPERAR HASTA 10:12:12 (PÉRDIDA: NO OPERAR HASTA 10:12:12)' in a
    assert not [x for x in a if f'({PA15} ' in x]


def test_sin_cuenta_real_en_la_decision_no_suena(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76, PA15])                      # sin copia: 5 s de diferencia, cada una es su decisión
    _dos_decisiones(con, [AP76, PA15], desfase_s=5)
    a = Avisos()
    s.vigilar(a, _t(D, '08:06:30'))
    emitidos = {r['clave']: r['notificado'] for r in con.execute('SELECT clave, notificado FROM avisos_emitidos')}
    de_76 = {k: v for k, v in emitidos.items() if f'|{AP76}|' in k}
    de_15 = {k: v for k, v in emitidos.items() if f'|{PA15}|' in k}
    assert de_76 and set(de_76.values()) == {0}                       # decisiones de la prueba: mudas
    assert de_15 and set(de_15.values()) == {1} and len(a) == len(de_15)    # las de la real suenan todas
    assert not [x for x in a if f'({AP76} ' in x]


def test_la_prueba_sola_nunca_suena(tmp_path):
    ent, con, s = _entorno(tmp_path, [AP76])
    _dos_decisiones(con, [AP76])
    a = Avisos()
    s.vigilar(a, _t(D, '08:06:30'))
    assert a == []
    # preventivo de la pérdida, reentrada y preventivo de 2 ops en 10 min: registrados, ninguno notificado
    assert tuple(con.execute('SELECT COUNT(*), SUM(notificado) FROM avisos_emitidos').fetchone()) == (3, 0)


def test_seguidora_prueba_con_lider_real_infraccion_propia_y_suena(tmp_path):
    """PA-15 (real) lidera y APEX-76 (prueba) copia. La prueba llega a su objetivo; la siguiente decisión es infracción
    de la seguidora (propia de su cuenta, cuelga de la decisión del líder) y, como la decisión tiene una real, suena."""
    ent, con, s = _entorno(tmp_path, [PA15, AP76], pares=[(PA15, AP76)])
    _base(con, PA15, 25000.0)
    _base(con, AP76, 26400.0)
    for c in (PA15, AP76):
        _op(con, c, NQ, 1, 20000, 20005.25, _t(D, '08:00:00'), _t(D, '08:05:00'), f'1{c}')     # +101,02
        _op(con, c, NQ, 1, 20000, 20000.25, _t(D, '08:10:00'), _t(D, '08:12:00'), f'2{c}')
    res = s.calcular(_t(D, '12:00:00'))
    l1, l2 = filas_por(res, PA15)
    c1, c2 = filas_por(res, AP76)
    assert c1.lider is l1 and c2.lider is l2
    assert c1.etiquetas_cuenta == ['OBJETIVO ALCANZADO: NO OPERAR MÁS'] and c1.cortafuegos and not l1.etiquetas_cuenta
    assert [t for t, _ in c2.infracciones_cuenta] == ['OPERACIÓN TRAS OBJETIVO'] and not l2.infracciones_cuenta
    cortafuegos.registrar(con, res.filas, res.cortafuegos, ent.reglas)
    assert [tuple(r) for r in con.execute('SELECT decision_id, cuenta, tipo FROM cortafuegos_saltados')] == \
        [(l2.op_id, AP76, 'OPERACIÓN TRAS OBJETIVO')]
    a = Avisos()
    s.vigilar(a, _t(D, '08:12:05'))
    assert 'INFRACCIÓN: OPERACIÓN TRAS OBJETIVO (APEX-91120-76 NQZ6) objetivo alcanzado a las 10:05:00' in a
