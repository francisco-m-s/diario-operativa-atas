"""Configuración en capas (V5.1, 17/11/2026): `config.yaml` (va al repositorio, sin datos personales) + `config.local.yaml` (tuyo,
ignorado por git, manda) + variable de entorno para la credencial de Google, que nunca vive en el repositorio."""

import os
from types import SimpleNamespace

import pytest

from atasv4 import config, sheets


def _base(tmp_path, publico, local=None):
    config.guardar_yaml(str(tmp_path / 'config.yaml'), publico)
    if local is not None:
        config.guardar_yaml(str(tmp_path / 'config.local.yaml'), local)
    return str(tmp_path)


def test_la_configuracion_local_manda_sobre_la_del_repositorio(tmp_path):
    base = _base(tmp_path, {'a': 1, 'b': 2, 'lista': ['x']}, {'b': 3, 'c': 4, 'lista': []})
    assert config.cargar_entorno(base).cfg == {'a': 1, 'b': 3, 'c': 4, 'lista': []}


def test_sin_configuracion_local_se_usa_solo_la_del_repositorio(tmp_path):
    assert config.cargar_entorno(_base(tmp_path, {'a': 1})).cfg == {'a': 1}


def test_la_variable_de_entorno_manda_sobre_la_credencial_del_fichero(tmp_path, monkeypatch):
    base = _base(tmp_path, {'credencial': ''}, {'credencial': 'ruta_local.json'})
    monkeypatch.delenv('ATAS_SHEETS_CREDENCIAL', raising=False)
    assert config.cargar_entorno(base).cfg['credencial'] == 'ruta_local.json'
    monkeypatch.setenv('ATAS_SHEETS_CREDENCIAL', 'ruta_entorno.json')
    assert config.cargar_entorno(base).cfg['credencial'] == 'ruta_entorno.json'


def test_cfg_override_gana_a_todo(tmp_path, monkeypatch):
    monkeypatch.setenv('ATAS_SHEETS_CREDENCIAL', 'entorno.json')
    base = _base(tmp_path, {'a': 1}, {'a': 2})
    assert config.cargar_entorno(base, {'a': 9, 'credencial': 'prueba'}).cfg == {'a': 9, 'credencial': 'prueba'}


def test_atas_dir_expande_las_variables_de_entorno(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', r'C:\Datos\Roaming')
    ent = config.cargar_entorno(_base(tmp_path, {'atas_dir': r'%APPDATA%\ATAS'}))
    assert ent.atas_dir == r'C:\Datos\Roaming\ATAS'
    assert ent.db_dir == r'C:\Datos\Roaming\ATAS\Database'


def test_un_cuentas_yaml_vacio_o_sin_la_clave_cuentas_es_un_error_no_cero_cuentas(tmp_path):
    base = _base(tmp_path, {'a': 1})
    ruta = tmp_path / 'cuentas.yaml'
    assert config.cargar_entorno(base).cuentas == {'cuentas': {}, 'sim_activas': []}       # sin fichero: arranque limpio
    ruta.write_text('', encoding='utf-8')                                                  # vacío tras un corte de luz
    with pytest.raises(ValueError, match='cuentas.yaml'):
        config.cargar_entorno(base)
    ruta.write_text('sim_activas: []\n', encoding='utf-8')
    with pytest.raises(ValueError, match='cuentas.yaml'):
        config.cargar_entorno(base)
    ruta.write_text('cuentas: {}\n', encoding='utf-8')                                     # «ninguna cuenta» sí es legítimo
    assert config.cargar_entorno(base).cuentas['cuentas'] == {}


def test_recargar_cuentas_con_el_fichero_danado_conserva_lo_anterior_y_falla_una_sola_vez(tmp_path):
    base = _base(tmp_path, {'a': 1})
    ruta = tmp_path / 'cuentas.yaml'
    config.guardar_yaml(str(ruta), {'cuentas': {'X': {'firma': 'Apex'}}})
    ent = config.cargar_entorno(base)
    ruta.write_text('', encoding='utf-8')
    os.utime(ruta, (1, ent._mtime + 5))                                                    # el CLI «lo ha cambiado»
    with pytest.raises(ValueError):
        ent.recargar_cuentas()
    assert 'X' in ent.cuentas['cuentas']                                                   # lo anterior sigue en memoria
    assert ent.recargar_cuentas() is False                                                 # y no protesta en cada ciclo


def test_guardar_yaml_fuerza_el_volcado_a_disco_antes_de_reemplazar(tmp_path, monkeypatch):
    llamadas = []
    monkeypatch.setattr(config.os, 'fsync', lambda fd: llamadas.append(fd))
    config.guardar_yaml(str(tmp_path / 'x.yaml'), {'a': 1})
    assert llamadas and (tmp_path / 'x.yaml').read_text(encoding='utf-8').startswith('a: 1')


def test_sin_credencial_la_hoja_falla_con_un_mensaje_claro(monkeypatch):
    monkeypatch.delenv('ATAS_SHEETS_CREDENCIAL', raising=False)
    with pytest.raises(RuntimeError, match='ATAS_SHEETS_CREDENCIAL'):
        sheets.HojaV4(SimpleNamespace(cfg={'credencial': '', 'sheet_id': 'x', 'pestana': 'V5'}))
