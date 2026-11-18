"""Integración con el generador de datos ficticios («generador-logs-pruebas»).

El generador escribe un día de mercado REAL con operaciones inventadas y su verdad conocida; el demonio las lee y tiene que
reconstruir exactamente esa verdad: decisiones, ticks, tipo de cierre y copias. Cubre lo que los fixtures reales (que no se publican)
cubrían en parte: las dos versiones de ATAS, días con descuadre horario EE. UU.–España, cierres anticipados, desfase del reloj del PC,
varios contratos y varias seguidoras.

El generador es otro proyecto. Se busca instalado (`pip install generador-logs-pruebas`), en la variable TESTEO_HISTORICO o en una carpeta
hermana `testeo_historico`; si no está, estos tests se saltan."""

import os
import shutil
import sys
from datetime import date

import pytest

from conftest import RAIZ, HojaFalsa
from atasv4 import almacen, config, fuentes, sync


def _importar_generador():
    candidatos = [os.environ.get('TESTEO_HISTORICO'), os.path.join(os.path.dirname(RAIZ), 'testeo_historico')]
    for ruta in [None] + [c for c in candidatos if c and os.path.isdir(c)]:
        if ruta:
            sys.path.insert(0, ruta)
        try:
            import testeo_historico
            return testeo_historico
        except ImportError:
            continue
    return None


th = _importar_generador()
if th is None:
    pytest.skip('necesita el generador testeo_historico (pip install generador-logs-pruebas, TESTEO_HISTORICO o carpeta hermana)',
                allow_module_level=True)


def _demonio_sobre(tmp_path, esc):
    """Escribe el escenario en una carpeta de ATAS ficticia y deja al demonio listo para calcular sobre ella."""
    carpeta, base = str(tmp_path / 'ATAS'), str(tmp_path / 'base')
    os.makedirs(base)
    th.escribir(esc, carpeta)
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), base)
    cfg = config.cargar_yaml(os.path.join(RAIZ, 'config.yaml'), {})
    cfg.update({'atas_dir': carpeta, 'sqlite': os.path.join(base, 't.sqlite'), 'backups_dir': os.path.join(base, 'backups'),
                'inicio_sesion': '2026-01-02', 'quemadas_iniciales': [], 'logs_respaldo': os.path.join(base, 'no_hay')})
    config.guardar_yaml(os.path.join(base, 'config.yaml'), cfg)
    fichas = {c.id: {'firma': 'Apex', 'tipo': 'evaluacion', 'tamano': 25000, 'sin_regla': False, 'alta': '2026-01-02',
                     'retiros': 0, 'ancla_inicial': 25000.0, 'ancla_estimada': False} for c in esc.cuentas}
    config.guardar_yaml(os.path.join(base, 'cuentas.yaml'), {'cuentas': fichas, 'sim_activas': []})
    ent = config.cargar_entorno(base)
    con = almacen.conectar(ent.sqlite_path)
    s = sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)
    s.ingerir()
    return con, s


def _comprobar(tmp_path, esc):
    con, s = _demonio_sobre(tmp_path, esc)
    try:
        res = s.calcular(f'{esc.fecha.isoformat()} 23:00:00.000')
        verdad = th.decisiones(esc)
        lideres = [f for f in res.filas if not f.es_seguidora]
        copias = [f for f in res.filas if f.es_seguidora]
        assert len(lideres) == len(verdad) >= 1
        assert len(copias) == len(verdad) * (len(esc.cuentas) - 1)
        for d in verdad:
            t_verdad = d['t_apertura_utc'].replace(tzinfo=None)
            casan = [f for f in lideres if abs((fuentes.parse_ts(f.t_apertura_utc) - t_verdad).total_seconds()) < 3]
            assert len(casan) == 1, f"decisión {d['numero']}: {len(casan)} candidatas"
            fila = casan[0]
            assert round(fila.ticks, 2) == d['ticks'], f"decisión {d['numero']}"
            assert fila.tipo_cierre == d['tipo_cierre'] and fila.cuenta == d['cuenta']
    finally:
        con.close()


DIAS = [
    date(2026, 3, 10),      # EE. UU. ya en horario de verano y la UE aún no: Madrid−NY = 5 h
    date(2026, 3, 31),      # los dos en horario de verano
    date(2026, 7, 15),
    date(2026, 10, 28),     # la UE ya salió del horario de verano y EE. UU. aún no: 5 h
    date(2026, 11, 2),      # los dos en horario de invierno
    date(2026, 11, 27),     # día después de Thanksgiving: cierre anticipado
]


@pytest.mark.parametrize('version', ['8.0.14', '8.0.15'])
@pytest.mark.parametrize('dia', DIAS, ids=[d.isoformat() for d in DIAS])
def test_el_demonio_reconstruye_la_verdad_de_un_dia_real(tmp_path, dia, version):
    _comprobar(tmp_path, th.generar(dia, semilla=dia.toordinal() % 97, operaciones=5, version_atas=version))


@pytest.mark.parametrize('desfase', [3.2, -0.4])
def test_un_reloj_del_pc_adelantado_o_atrasado_no_cambia_el_resultado(tmp_path, desfase):
    _comprobar(tmp_path, th.generar(date(2026, 5, 12), semilla=7, operaciones=5, desfase_pc_s=desfase))


@pytest.mark.parametrize('contratos, seguidoras', [(2, 1), (1, 3), (3, 2)])
def test_varios_contratos_y_varias_seguidoras(tmp_path, contratos, seguidoras):
    _comprobar(tmp_path, th.generar(date(2026, 12, 21), semilla=9, operaciones=4, contratos=contratos, seguidoras=seguidoras))


@pytest.mark.parametrize('raiz', ['NQ', 'MNQ'])
def test_nq_y_mnq(tmp_path, raiz):
    _comprobar(tmp_path, th.generar(date(2026, 9, 16), semilla=4, operaciones=4, raiz=raiz))
