import inspect
import os
import re
import shutil
import sys

import pytest

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
sys.path.insert(0, RAIZ)

FIXTURES = os.path.join(AQUI, 'fixtures')
# Lo que delata el uso de datos reales: una constante FIXT…, la carpeta 'fixtures' como cadena, o leer las semillas de la raíz
_DATOS_REALES = re.compile(r'''FIXT\w*|['"]fixtures['"]|RAIZ[^\n]*semillas\.yaml''')


def _de_aqui(f):
    return (inspect.getsourcefile(f) or '').startswith(AQUI)               # las fixtures y ayudantes de pytest no cuentan


def _con_ayudantes(funcion, limite=60):
    """La función y los ayudantes de este directorio que llama, de forma transitiva y por nombre."""
    vistas, pendientes = [], [funcion]
    while pendientes and len(vistas) < limite:
        f = pendientes.pop()
        if f in vistas:
            continue
        vistas.append(f)
        for nombre in f.__code__.co_names:
            g = f.__globals__.get(nombre)
            if inspect.isfunction(g) and _de_aqui(g) and g not in vistas:
                pendientes.append(g)
    return vistas


def _usa_datos_reales(item):
    """El test (o una fixture o ayudante suyo de este directorio) lee logs reales de ATAS de tests/fixtures o las semillas reales."""
    if {'entorno', 'entorno_hoy'} & set(item.fixturenames):
        return True
    funciones = [item.function]
    for nombre in item.fixturenames:
        funciones += [d.func for d in item._fixtureinfo.name2fixturedefs.get(nombre, ())[-1:]]
    for f in funciones:
        if not _de_aqui(f):
            continue
        for g in _con_ayudantes(f):
            try:
                if _DATOS_REALES.search(inspect.getsource(g)):
                    return True
            except (OSError, TypeError):
                continue
    return False


def pytest_collection_modifyitems(config, items):
    """En el repositorio PÚBLICO no hay logs reales (tests/fixtures no se publica): los tests que los necesitan se saltan, con el
    motivo a la vista. En el árbol local, donde sí están, esto no hace nada."""
    if os.path.isdir(FIXTURES):
        return
    saltar = pytest.mark.skip(reason='necesita logs reales de ATAS (tests/fixtures), que no se publican')
    for item in items:
        if _usa_datos_reales(item):
            item.add_marker(saltar)

from atasv4 import almacen, config, sync  # noqa: E402

FIXT_ATAS = os.path.join(AQUI, 'fixtures', 'ATAS')

CUENTAS_FIXT = {
    'cuentas': {
        'PA-APEX-91120-16': {'firma': 'Apex', 'tipo': 'PA', 'tamano': 25000, 'sin_regla': False,
                             'alta': '2026-09-16', 'retiros': 0, 'ancla_inicial': None, 'ancla_estimada': True},
        'PA-APEX-91120-15': {'firma': 'Apex', 'tipo': 'PA', 'tamano': 25000, 'sin_regla': False,
                             'alta': '2026-09-16', 'retiros': 0, 'ancla_inicial': None, 'ancla_estimada': True},
        'fsern2776tcp25d152249': {'firma': 'E2T', 'tipo': 'TCP', 'tamano': 25000, 'sin_regla': False,
                                  'alta': '2026-09-16', 'retiros': 0, 'ancla_inicial': None,
                                  'ancla_estimada': True},
        # quemada: sin fila BAL, pero sus operaciones del 16/09 sirven de fixture (salidas Limit y Market)
        'BX63983-50': {'firma': 'Bulenox', 'tipo': 'evaluacion', 'tamano': 50000, 'sin_regla': False,
                       'alta': '2026-09-16', 'retiros': 0, 'ancla_inicial': None, 'ancla_estimada': True},
    },
    'sim_activas': ['fsern2776_sim'],     # los fixtures de sim sirven para tests aunque en producción se ignore
}


class HojaFalsa:
    """Sustituto del cliente de Sheets: guarda lo escrito y lo devuelve al leer."""

    def __init__(self):
        self.valores = []
        self.escrito = None

    def asegurar_pestana(self):
        return False

    def leer(self):
        return [list(r) for r in self.valores]

    def escribir(self, lay):
        self.escrito = lay
        self.valores = [list(r) for r in lay['valores']]

    # histórico unificado (spec §16.9): pestañas a las que solo se añade por arriba
    def leer_columna_a(self, pestana):
        return [r[0] if r else '' for r in self.__dict__.setdefault('pestanas', {}).get(pestana, [])]

    def insertar_arriba(self, pestana, cabecera, filas, formatos=None):
        tab = self.__dict__.setdefault('pestanas', {}).setdefault(pestana, [list(cabecera)])
        tab[1:1] = [list(f) for f in filas]
        return len(filas)

    def leer_otra_hoja(self, sheet_id, pestana, rango='A1:AZ'):
        return [list(r) for r in getattr(self, 'otra', [])]

    def duplicar(self, nombre):                  # archivo de un ciclo (spec §15): la copia queda anotada
        if not hasattr(self, 'duplicadas'):
            self.duplicadas = []
        self.duplicadas.append(nombre)
        return nombre

    def leer_filas(self, fila, col, n=1):
        return [list(self.valores[k][:col + 1]) if k < len(self.valores) else [] for k in range(fila, fila + n)]

    # R17 (spec §13): lectura de la fila de la estrategia y escritura de UNA celda; cada escritura queda anotada
    def leer_fila_r17(self, fila, col):
        return list(self.valores[fila][:col + 1]) if fila < len(self.valores) else []

    def escribir_celda_r17(self, fila, col, texto):
        if not hasattr(self, 'escrituras_r17'):
            self.escrituras_r17 = []
        self.escrituras_r17.append((fila, col, texto))
        self.valores[fila][col] = texto

    # pestaña del recuento semanal (spec §11.3)
    def asegurar_pestana_objetivos(self):
        return False

    def leer_objetivos(self):
        return [list(r) for r in getattr(self, 'objetivos', [])]

    def escribir_objetivos(self, lay):
        self.objetivos = [list(r) for r in lay['valores']]
        self.objetivos_lay = lay


@pytest.fixture
def entorno(tmp_path):
    base = str(tmp_path)
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), os.path.join(base, 'reglas.yaml'))
    cfg = config.cargar_yaml(os.path.join(RAIZ, 'config.yaml'), {})
    cfg.update({'atas_dir': FIXT_ATAS, 'sqlite': os.path.join(base, 't.sqlite'),
                'backups_dir': os.path.join(base, 'backups'), 'inicio_sesion': '2026-09-16',
                'quemadas_iniciales': ['BX63983-50']})
    config.guardar_yaml(os.path.join(base, 'config.yaml'), cfg)
    config.guardar_yaml(os.path.join(base, 'cuentas.yaml'), CUENTAS_FIXT)
    ent = config.cargar_entorno(base)
    con = almacen.conectar(ent.sqlite_path)
    s = sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)
    s.ingerir()
    yield ent, con, s
    con.close()


def filas_por(res, cuenta):
    return sorted((f for f in res.filas if f.cuenta == cuenta), key=lambda f: f.t_apertura_utc)


FIXT_ATAS_HOY = os.path.join(AQUI, 'fixtures', 'ATAS_hoy')
CUENTAS_HOY = {
    'cuentas': {
        'PA-APEX-91120-15': {'firma': 'Apex', 'tipo': 'PA', 'tamano': 25000, 'sin_regla': False, 'alta': '2026-09-22',
                             'retiros': 0, 'ancla_inicial': 25093.06, 'ancla_estimada': False},
        'PA-APEX-91120-16': {'firma': 'Apex', 'tipo': 'PA', 'tamano': 25000, 'sin_regla': False, 'alta': '2026-09-22',
                             'retiros': 0, 'ancla_inicial': 24453.03, 'ancla_estimada': False},
        'fsern2776tcp25d152250': {'firma': 'E2T', 'tipo': 'TCP', 'tamano': 25000, 'sin_regla': False,
                                  'alta': '2026-09-22', 'retiros': 0, 'ancla_inicial': None, 'ancla_estimada': True},
    },
    'sim_activas': [],
}


@pytest.fixture
def entorno_hoy(tmp_path):
    """Sesión real del 22/09/2026 16:58–18:46 Madrid: 8 decisiones de PA-16 (copiada en PA-15) y E2T-250."""
    base = str(tmp_path)
    shutil.copy(os.path.join(RAIZ, 'reglas.yaml'), os.path.join(base, 'reglas.yaml'))
    cfg = config.cargar_yaml(os.path.join(RAIZ, 'config.yaml'), {})
    cfg.update({'atas_dir': FIXT_ATAS_HOY, 'sqlite': os.path.join(base, 't.sqlite'),
                'backups_dir': os.path.join(base, 'backups'), 'inicio_sesion': '2026-09-22',
                'quemadas_iniciales': []})
    config.guardar_yaml(os.path.join(base, 'config.yaml'), cfg)
    config.guardar_yaml(os.path.join(base, 'cuentas.yaml'), CUENTAS_HOY)
    ent = config.cargar_entorno(base)
    con = almacen.conectar(ent.sqlite_path)
    s = sync.Sincronizador(ent, con, HojaFalsa(), log=lambda m: None)
    s.ingerir()
    yield ent, con, s
    con.close()
