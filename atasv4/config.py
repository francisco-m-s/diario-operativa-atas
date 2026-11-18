"""Carga de config.yaml, reglas.yaml y cuentas.yaml. Ningún valor de negocio vive en el código."""

import os
import re
from dataclasses import dataclass, field
from datetime import date

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cargar_yaml(path, defecto=None):
    if not os.path.exists(path):
        return defecto
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or defecto


def guardar_yaml(path, data, cabecera=''):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        if cabecera:
            f.write(cabecera)
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        f.flush()
        os.fsync(f.fileno())            # V5.1: un corte de luz justo tras el reemplazo no debe dejar un fichero vacío
    os.replace(tmp, path)


def cargar_cuentas(path):
    """cuentas.yaml normalizado. Sin fichero: arranque limpio. Con el fichero vacío, ilegible o sin la clave `cuentas`: ValueError.
    Leerlo como «cero cuentas» daría de alta todas de nuevo, sin retiros ni resets, y lo guardaría encima (V5.1)."""
    if not os.path.exists(path):
        return _normalizar(None)
    try:
        datos = cargar_yaml(path, None)
    except yaml.YAMLError as e:
        raise ValueError(f'cuentas.yaml no se puede leer ({type(e).__name__}); restaura una copia de backups\\.') from e
    if not isinstance(datos, dict) or 'cuentas' not in datos:
        raise ValueError('cuentas.yaml está vacío o dañado (falta la clave `cuentas`); restaura una copia de '
                         'backups\\ (cuentas_AAAAMMDD.yaml).')
    return _normalizar(datos)


@dataclass
class Entorno:
    """Todo lo que el motor necesita: rutas, reglas y registro de cuentas."""
    base_dir: str
    cfg: dict
    reglas: dict
    cuentas_path: str
    cuentas: dict = field(default_factory=dict)
    semillas: dict = field(default_factory=dict)       # semillas.yaml: solo lectura (spec §8.3, §9.5)

    @property
    def atas_dir(self):
        return os.path.expandvars(self.cfg['atas_dir'])       # `%APPDATA%\ATAS` por defecto: sin el nombre de usuario en el repo

    @property
    def db_dir(self):
        return os.path.join(self.atas_dir, 'Database')

    @property
    def logs_dir(self):
        return os.path.join(self.atas_dir, 'Logs')

    @property
    def logs_respaldo_dir(self):
        """Copia de logs de ATAS para la migración de 'Order trade' (los que ATAS ya haya borrado). None si no hay."""
        p = self.cfg.get('logs_respaldo')
        if not p:
            return None
        return p if os.path.isabs(p) else os.path.join(self.base_dir, p)

    @property
    def sqlite_path(self):
        p = self.cfg['sqlite']
        return p if os.path.isabs(p) else os.path.join(self.base_dir, p)

    @property
    def backups_dir(self):
        p = self.cfg['backups_dir']
        return p if os.path.isabs(p) else os.path.join(self.base_dir, p)

    @property
    def inicio_sesion(self):
        v = self.cfg['inicio_sesion']
        return v if isinstance(v, date) else date.fromisoformat(str(v))

    # --- registro de cuentas (cuentas.yaml) ---
    def guardar_cuentas(self):
        guardar_yaml(self.cuentas_path, self.cuentas,
                     '# Gestionado por el sistema V4 (demonio y cli_v4.py). Editar solo con el demonio parado.\n')
        self._mtime = os.path.getmtime(self.cuentas_path)

    def recargar_cuentas(self):
        """Relee cuentas.yaml si otro proceso (cli_v4.py) lo cambió."""
        try:
            mt = os.path.getmtime(self.cuentas_path)
        except OSError:
            return False
        if mt == getattr(self, '_mtime', None):
            return False
        self._mtime = mt                # antes de leer: un fichero dañado avisa UNA vez y se conserva lo que había en memoria
        self.cuentas = cargar_cuentas(self.cuentas_path)
        return True

    @property
    def resets(self):
        """Registro de resets (spec §14): {cuenta física: [{desde, nombre}]}. Ver resets.py."""
        return self.cuentas.get('resets') or {}

    def es_sim(self, cuenta):
        return any(re.search(p, cuenta) for p in self.reglas['patrones_sim'])

    def sim_activa(self, cuenta):
        return cuenta in (self.cuentas.get('sim_activas') or [])

    def ficha(self, cuenta):
        return (self.cuentas.get('cuentas') or {}).get(cuenta)

    def es_real(self, cuenta):
        """Cuenta real (spec §8.1): su firma y tipo están en reglas.yaml -> cuentas_reales (hoy, las Apex PA)."""
        f = self.ficha(cuenta) or {}
        return any(f.get('firma') == r['firma'] and f.get('tipo') == r['tipo']
                   for r in self.reglas.get('cuentas_reales') or [])

    def es_prueba(self, cuenta):
        """Prueba (spec §9.1): ni sim ni real. Que esté viva solo decide si tiene fila BAL: una prueba quemada conserva
        sus reglas en las operaciones que ya tiene (quemar no cambia etiquetas ni infracciones hacia atrás)."""
        return not self.es_sim(cuenta) and not self.es_real(cuenta)

    def semilla(self, cuenta):
        return (self.semillas.get('cuentas') or {}).get(cuenta) or {}

    def nombre_visible(self, cuenta):
        """Nombre actual (con todos los retiros registrados). Para una operación: nombre_con_retiros."""
        f = self.ficha(cuenta) or {}
        return self.nombre_con_retiros(cuenta, int(f.get('retiros') or 0))

    @staticmethod
    def nombre_con_retiros(cuenta, n):
        return f"{cuenta}_retiros_{n}" if n else cuenta

    def cuenta_por_nombre_visible(self, nombre):
        """Cuenta interna de un nombre del sheet, con o sin sufijo _retiros_N (actual o anterior)."""
        base = re.sub(r'_retiros_\d+$', '', str(nombre).strip())
        return base if base in (self.cuentas.get('cuentas') or {}) else None


ENV_CREDENCIAL = 'ATAS_SHEETS_CREDENCIAL'        # ruta al .json de la cuenta de servicio de Google (siempre FUERA del repositorio)


def cargar_entorno(base_dir=BASE_DIR, cfg_override=None):
    """Capas, de menor a mayor prioridad: config.yaml (va al repositorio, sin datos personales), config.local.yaml (lo tuyo; git lo
    ignora; sustituye clave a clave), variable de entorno ATAS_SHEETS_CREDENCIAL (solo la credencial) y `cfg_override` (tests)."""
    cfg = cargar_yaml(os.path.join(base_dir, 'config.yaml'), {})
    cfg.update(cargar_yaml(os.path.join(base_dir, 'config.local.yaml'), {}))
    if os.environ.get(ENV_CREDENCIAL):
        cfg['credencial'] = os.environ[ENV_CREDENCIAL]
    if cfg_override:
        cfg.update(cfg_override)
    reglas = cargar_yaml(os.path.join(base_dir, 'reglas.yaml'), {})
    cuentas_path = os.path.join(base_dir, 'cuentas.yaml')
    semillas = cfg.get('semillas') or 'semillas.yaml'
    semillas = semillas if os.path.isabs(semillas) else os.path.join(base_dir, semillas)
    ent = Entorno(base_dir=base_dir, cfg=cfg, reglas=reglas, cuentas_path=cuentas_path,
                  cuentas=cargar_cuentas(cuentas_path), semillas=cargar_yaml(semillas, {}) or {})
    if os.path.exists(cuentas_path):
        ent._mtime = os.path.getmtime(cuentas_path)
    return ent


def _normalizar(cuentas):
    cuentas = cuentas or {}
    if not cuentas.get('cuentas'):
        cuentas['cuentas'] = {}
    if not cuentas.get('sim_activas'):
        cuentas['sim_activas'] = []
    return cuentas
