"""V5.1 (17/11/2026, revisión de código). Sintético y mínimo, como test_v5: fills de MyTrade escritos en la SQLite.

Dos arreglos de los BAJA que dejó documentados la revisión del 11/11/2026 (INFORME_V4.md):
- los turnos del demonio se guardan con el reloj del PC y las aperturas están en hora del exchange: se comparan ya en la misma;
- `almacen._FOTO` guarda la foto por `id(con)`: `conectar` descarta lo que haya dejado otra conexión con el mismo `id`."""

import os
import sqlite3
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

import demonio_v4
from atasv4 import almacen, cuentas, reloj, sync
from test_apex_pruebas import AP76, NQ, _op, _t
from test_tanda_20261111 import _S
from test_v5 import AHORA, V, _ent

DESFASE = -0.8          # el reloj del PC va 0,8 s por detrás del exchange (medido el 12/11/2026: −0,829 s)


def _reloj_con_desfase():
    return reloj.Reloj(cortes=('',), desfases=(DESFASE,))


def test_los_turnos_se_pasan_del_reloj_del_pc_al_del_exchange(tmp_path):
    ent, con, s = _ent(tmp_path)
    t = almacen.abrir_turno(con, _t(V, '14:25:00'))
    almacen.cerrar_turno(con, t, _t(V, '15:00:00'))
    almacen.abrir_turno(con, _t(V, '16:00:00'))                      # sigue en marcha: sin fin
    assert s._turnos_exchange(_reloj_con_desfase()) == [
        ('2026-11-12 14:25:00.800', '2026-11-12 15:00:00.800'), ('2026-11-12 16:00:00.800', None)]


def test_el_recordatorio_compara_con_el_arranque_en_la_hora_del_exchange(tmp_path):
    ent, con, s = _ent(tmp_path)
    s.turno_inicio = _t(V, '14:25:00')                               # reloj del PC; en el del exchange: 14:25:00.800
    antes = SimpleNamespace(es_seguidora=False, t_apertura_utc='2026-11-12 14:25:00.500')    # abierta antes de arrancar
    despues = SimpleNamespace(es_seguidora=False, t_apertura_utc='2026-11-12 14:25:01.000')
    recordatorio = ent.reglas['v5']['recordatorio']
    assert s._recordatorio(SimpleNamespace(filas=[antes], reloj=_reloj_con_desfase())) == recordatorio
    assert s._recordatorio(SimpleNamespace(filas=[antes, despues], reloj=_reloj_con_desfase())) is None


def test_la_decision_abierta_antes_del_arranque_en_hora_del_exchange_es_turno_fantasma(tmp_path, monkeypatch):
    ent, con, s = _ent(tmp_path)
    t = almacen.abrir_turno(con, _t(V, '14:25:00'))
    almacen.cerrar_turno(con, t, _t(V, '15:00:00'))
    rel = _reloj_con_desfase()
    monkeypatch.setattr(reloj, 'cargar', lambda con, R: rel)
    salida = 20000 + 5 * 0.25
    _op(con, AP76, NQ, 1, 20000, salida, '2026-11-12 14:25:00.500', _t(V, '14:26:00'), 1)   # antes del arranque (14:25:00.800)
    _op(con, AP76, NQ, 1, 20000, salida, _t(V, '14:30:00'), _t(V, '14:31:00'), 2)
    res, _ = s.sync(AHORA)
    por_orden = sorted((f for f in res.filas if not f.es_seguidora), key=lambda f: f.t_apertura_utc)
    assert [f.turno for f in por_orden] == ['turno fantasma', 'T1']


def test_conectar_descarta_la_foto_en_cache_de_otra_conexion_con_el_mismo_id(tmp_path, monkeypatch):
    ruta = str(tmp_path / 'foto.sqlite')
    real = sqlite3.connect(ruta)
    almacen._FOTO[id(real)] = ('v1', {'filas': {'vieja|1': {}}, 'evaluar': {}})     # resto de una conexión que tuvo este id
    monkeypatch.setattr(almacen.sqlite3, 'connect', lambda *a, **k: real)
    con = almacen.conectar(ruta)
    try:
        con.execute("INSERT INTO foto_filas VALUES ('nueva|1', '{\"a\": 1}')")
        almacen.set_estado(con, 'foto_version', 'v1')                # misma versión que la del resto en caché
        con.commit()
        assert almacen.foto(con)['filas'] == {'nueva|1': {'a': 1}}
    finally:
        almacen._FOTO.pop(id(con), None)
        con.close()


# --- copias de seguridad (hallazgos de la segunda pasada, 17/11/2026) ---

def _copias(ent, prefijo, sufijo):
    return sorted(n for n in os.listdir(ent.backups_dir) if n.startswith(prefijo) and n.endswith(sufijo))


def test_una_copia_diaria_fallida_no_deja_un_fichero_que_se_da_por_bueno(entorno_hoy):
    ent, con, s = entorno_hoy

    class ConRoto:
        def backup(self, destino):
            raise sqlite3.OperationalError('disco lleno')

    with pytest.raises(sqlite3.OperationalError):
        sync.backup_diario(ent, ConRoto(), lambda m: None)
    assert _copias(ent, 'atas_v4_', '') == []                                  # ni la copia a medias ni su .tmp
    assert sync.backup_diario(ent, con, lambda m: None)                        # y al ciclo siguiente se reintenta, y esta vez sí
    assert _copias(ent, 'atas_v4_', '.sqlite') == [f"atas_v4_{datetime.now():%Y%m%d}.sqlite"]


def test_un_fichero_de_copia_vacio_de_hoy_no_cuenta_como_copia(entorno_hoy):
    ent, con, s = entorno_hoy
    os.makedirs(ent.backups_dir, exist_ok=True)
    hoy = os.path.join(ent.backups_dir, f"atas_v4_{datetime.now():%Y%m%d}.sqlite")
    open(hoy, 'w').close()                                                     # resto de un fallo de una versión anterior
    assert sync.backup_diario(ent, con, lambda m: None) == hoy and os.path.getsize(hoy) > 0


def test_la_copia_diaria_incluye_cuentas_yaml_y_solo_rota_las_diarias(entorno_hoy):
    ent, con, s = entorno_hoy
    ent.cfg['backups_retener'] = 2
    os.makedirs(ent.backups_dir, exist_ok=True)
    for n in ('cuentas_20200101.yaml', 'cuentas_20200102.yaml', 'cuentas_20261103_antes_reset.yaml'):
        open(os.path.join(ent.backups_dir, n), 'w').close()
    sync.backup_diario(ent, con, lambda m: None)
    hoy = f"cuentas_{datetime.now():%Y%m%d}.yaml"
    assert _copias(ent, 'cuentas_', '.yaml') == ['cuentas_20200102.yaml', 'cuentas_20261103_antes_reset.yaml', hoy]
    with open(ent.cuentas_path, 'rb') as origen, open(os.path.join(ent.backups_dir, hoy), 'rb') as copia:
        assert copia.read() == origen.read()                                   # copia exacta; la manual (_antes_) no se toca


# --- peticiones del CLI al demonio (hallazgo de la segunda pasada) ---

def _pedir(con, clave):
    almacen.set_estado(con, clave, '1')
    con.commit()


def test_una_peticion_de_sync_del_cli_no_se_pierde_si_el_sync_falla(entorno_hoy):
    ent, con, _ = entorno_hoy

    class SFalla(_S):
        intentos = 0

        def sync(self):
            self.intentos += 1
            if self.intentos == 1:
                raise TimeoutError('Sheets no ha contestado')

    s, logs = SFalla(), []
    c = demonio_v4.Ciclo(ent, con, s, logs.append, avisar=lambda *a: None)
    _pedir(con, 'forzar_sync')
    c.paso()
    assert s.intentos == 1 and almacen.get_estado(con, 'forzar_sync') == '1'         # falló: la petición sigue en pie
    assert any(m.startswith('[SYNC ERROR] TimeoutError') for m in logs)
    c.paso()
    assert s.intentos == 1                                                           # y no martillea la API: espera el reintento
    c.ultimo_fallo = 0.0                                                             # «pasa el tiempo»
    c.paso()
    assert s.intentos == 2 and almacen.get_estado(con, 'forzar_sync') == '0'


def test_una_peticion_de_archivar_se_rearma_si_falla_la_red_pero_no_si_el_archivo_se_rechaza(entorno_hoy):
    ent, con, _ = entorno_hoy

    class SArchiva(_S):
        llamadas, error = 0, TimeoutError('red')

        def archivar_ciclo(self):
            self.llamadas += 1
            raise self.error

    s = SArchiva()
    c = demonio_v4.Ciclo(ent, con, s, lambda m: None, avisar=lambda *a: None)
    _pedir(con, 'archivar_ciclo')
    c.paso()
    assert s.llamadas == 1 and almacen.get_estado(con, 'archivar_ciclo') == '1'     # fallo de red: se reintentará
    s.error = ValueError('hay cuentas con la posición abierta')
    c.ultimo_fallo = 0.0
    c.paso()
    assert s.llamadas == 2 and almacen.get_estado(con, 'archivar_ciclo') == '0'     # rechazo de negocio: no se repite


def test_el_demonio_no_arranca_si_no_existe_la_carpeta_de_atas(entorno_hoy, monkeypatch, capsys):
    ent, con, _ = entorno_hoy
    ent.cfg['atas_dir'] = os.path.join(os.path.dirname(ent.sqlite_path), 'no_existe')
    monkeypatch.setattr(demonio_v4.config, 'cargar_entorno', lambda *a, **k: ent)
    monkeypatch.setattr(demonio_v4.time, 'sleep', lambda s: None)
    monkeypatch.setattr(demonio_v4.sheets, 'HojaV4', lambda e: pytest.fail('no debe llegar a tocar Sheets'))
    monkeypatch.setattr(sys, 'argv', ['demonio_v4.py'])
    demonio_v4.main()
    assert 'carpeta de ATAS' in capsys.readouterr().out


# --- el log y la consola no muestran el ID de la hoja ni la ruta de la credencial ---

def test_los_valores_a_ocultar_salen_de_la_configuracion():
    cfg = {'sheet_id': 'HOJA_ACTUAL_12345', 'sheet_id_antiguo': 'HOJA_ANTIGUA_67890', 'credencial': 'ruta/mi-clave.json', 'pestana': 'V5'}
    assert demonio_v4.valores_a_ocultar(cfg) == {'HOJA_ACTUAL_12345': '<id-de-hoja>', 'HOJA_ANTIGUA_67890': '<id-de-hoja>',
                                                 'ruta/mi-clave.json': '<credencial>'}
    assert demonio_v4.valores_a_ocultar({'sheet_id': '', 'credencial': None}) == {}


def test_el_log_oculta_el_id_de_la_hoja_y_la_ruta_de_la_credencial(tmp_path, monkeypatch, capsys):
    hoja, ruta = 'HOJA_ACTUAL_12345', 'ruta/mi-clave.json'
    monkeypatch.setattr(demonio_v4, 'LOG_FILE', str(tmp_path / 'x.log'))
    monkeypatch.setattr(demonio_v4, 'OCULTAR', {hoja: '<id-de-hoja>', ruta: '<credencial>'})
    demonio_v4.log(f'[SYNC ERROR] HttpError 502 spreadsheets/{hoja}/values; FileNotFoundError {ruta}')
    for texto in (capsys.readouterr().out, (tmp_path / 'x.log').read_text(encoding='utf-8')):
        assert hoja not in texto and ruta not in texto and '<id-de-hoja>' in texto and '<credencial>' in texto


# --- alias circulares en reglas.yaml ---

def test_los_alias_circulares_de_firmas_no_cuelgan_el_demonio():
    ciclo = {'firmas': {'A': {'como': 'B'}, 'B': {'como': 'A'}}}
    assert cuentas.tamano_por_balance('A', 25000, ciclo) is None
    assert cuentas.regla_firma('A', 25000, ciclo) is None
    alias = {'firmas': {'A': {'como': 'B'}, 'B': {25000: {'drawdown': 1000}, 50000: {'drawdown': 2000}}}}
    assert cuentas.tamano_por_balance('A', 26000, alias) == 25000 and cuentas.regla_firma('A', 50000, alias) == {'drawdown': 2000}
