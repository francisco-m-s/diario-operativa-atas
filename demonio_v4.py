"""Demonio ATAS V5.1 -> pestaña V5 de Registro_bot_V5 (el fichero conserva el nombre `demonio_v4.py`, spec §16.11).

Ciclo: ingesta incremental (logs + cdb) cada `intervalo_s`; escribe el sheet cuando una cuenta
queda plana con operaciones nuevas, al cambiar de sesión CME, o a petición (`sync` / cli_v4.py sync).
Uso: python demonio_v4.py [--oneshot]
"""

import ctypes
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atasv4 import almacen, aviso, config, r17, sheets, sync, v5 as mv5  # noqa: E402

LOG_FILE = os.path.join(config.BASE_DIR, 'atas_v4.log')
REINTENTO_S = 60
OCULTAR = {}            # {valor sensible: marcador}; main() lo rellena con el ID de las hojas y la ruta de la credencial


def valores_a_ocultar(cfg):
    """Lo que nunca debe salir en la consola ni en el log, aunque una excepción de la API de Google lo arrastre en su texto (la URL
    de una petición fallida lleva el ID de la hoja; un fichero que falta, la ruta de la credencial). V5.1."""
    marcadores = (('sheet_id', '<id-de-hoja>'), ('sheet_id_antiguo', '<id-de-hoja>'), ('credencial', '<credencial>'))
    return {str(cfg[clave]): marcador for clave, marcador in marcadores if cfg.get(clave)}


def log(msg):
    msg = str(msg)
    for valor, marcador in OCULTAR.items():
        msg = msg.replace(valor, marcador)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode('ascii', 'replace').decode('ascii'), flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def instancia_unica(nombre):
    """Mutex con nombre de Windows: sin lockfiles huérfanos ni PIDs reciclados. El código de error se lee con el mecanismo
    de ctypes (`use_last_error`), no llamando a GetLastError a mano: entre las dos llamadas ctypes puede pisarlo (11/11/2026).
    None si ya existe (otro demonio) o si no se pudo crear (no se puede asegurar que sea el único)."""
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    ctypes.set_last_error(0)
    h = k32.CreateMutexW(None, False, nombre)
    if not h or ctypes.get_last_error() == 183:        # ERROR_ALREADY_EXISTS
        return None
    return h


class Ciclo:
    """Un paso del bucle del demonio. Cualquier excepción se registra y el demonio sigue vivo."""

    def __init__(self, ent, con, s, log, avisar=None):
        self.ent, self.con, self.s, self.log = ent, con, s, log
        self.ultimo_fallo = 0.0
        self.avisar = avisar if avisar is not None else aviso.avisador(ent.reglas)   # `or` no vale: un doble de test vacío es falso
        self.hay_abiertas = True           # primera vuelta: vigilar siempre (registra lo viejo sin avisar)
        self.ultima_preop = 0.0
        self.ultimo_error_preop = 0.0
        self.intervalo_s = float(ent.cfg.get('intervalo_s', 2))
        self._t_tick = 0.0                 # último tick del cortafuegos (reloj monótono)
        self._en_tick = False
        self.turno_id = None               # turno de operativa en curso (spec §16.2); lo abre main(), no los tests ni --oneshot
        self._t_latido = 0.0

    def abrir_turno(self):
        """Empieza el turno de operativa (spec §16.2): de este arranque del demonio a su cierre."""
        inicio = sync.ahora_utc()
        self.turno_id = almacen.abrir_turno(self.con, inicio)
        self.s.turno_inicio = inicio
        self._t_latido = time.monotonic()
        return inicio

    def cerrar_turno(self):
        if self.turno_id is not None:
            almacen.cerrar_turno(self.con, self.turno_id, sync.ahora_utc())
            self.turno_id = None

    def turno(self):
        """Con un turno abierto: su latido (para que un cuelgue no lo deje abierto para siempre) y el aviso de fin de turno
        (spec §16.4: notificación sin sonido, una vez por día)."""
        if self.turno_id is None:
            return
        try:
            if time.monotonic() - self._t_latido >= float(self.ent.cfg.get('turno_latido_s', 60)):
                self._t_latido = time.monotonic()
                almacen.latido_turno(self.con, self.turno_id, sync.ahora_utc())
            fin = mv5.aviso_fin_turno(sync.ahora_utc(), self.ent.reglas)
            if fin and almacen.get_estado(self.con, 'fin_turno_avisado') != fin[0]:
                almacen.set_estado(self.con, 'fin_turno_avisado', fin[0])
                self.con.commit()
                self.log(f"[AVISO V5] {fin[1]}")
                self.avisar(self.ent.reglas['v5']['avisos']['titulo'], fin[1], sonido='')
        except Exception as e:
            self.log(f"[TURNO ERROR] {type(e).__name__}: {e}")

    def tick(self, recargar=True):
        """Ingesta + cortafuegos en vivo: lo que tiene que correr cada `intervalo_s` pase lo que pase. True si fue bien."""
        self._t_tick = time.monotonic()
        try:
            if recargar and self.ent.recargar_cuentas():
                self.log('[CUENTAS] cuentas.yaml recargado.')
            nuevas = self.s.ingerir()
            if nuevas or self.hay_abiertas:            # cortafuegos en vivo: en cuanto llega al log
                res = self.s.vigilar(self.avisar)
                self.hay_abiertas = bool(res.filas_abiertas)
            return True
        except Exception as e:
            self.log(f"[CICLO ERROR] {type(e).__name__}: {e}")
            return False

    def corredor(self, fn, plazo_s):
        """Ejecuta una petición de red (`HojaV4.corredor`) sin parar el cortafuegos (11/11/2026): `fn` va en un hilo auxiliar
        y, mientras no vuelve, este hilo sigue dando un `tick` cada `intervalo_s`. Antes, el sync (unas once peticiones
        seguidas, justo al cerrar una decisión) y la lectura de la preoperativa dejaban el ciclo parado mientras duraban.
        `plazo_s` es de RELOJ DE PARED: si vence, la petición se abandona (TimeoutError) aunque la respuesta siga goteando.
        SQLite solo se toca desde este hilo; el auxiliar solo hace la petición."""
        caja = {}

        def trabajo():
            try:
                caja['v'] = fn()
            except BaseException as e:
                caja['e'] = e

        hilo = threading.Thread(target=trabajo, daemon=True, name='red')
        hilo.start()
        fin = time.monotonic() + plazo_s
        while True:
            hilo.join(0.05)
            if not hilo.is_alive():
                break
            if time.monotonic() >= fin:
                raise TimeoutError(f'Sheets no ha contestado en {plazo_s:g} s (plazo de reloj de pared); se abandona la petición')
            if not self._en_tick and time.monotonic() - self._t_tick >= self.intervalo_s:
                self._en_tick = True
                try:
                    self.tick(recargar=False)       # cuentas.yaml no se cambia a mitad de un sync
                finally:
                    self._en_tick = False
        if 'e' in caja:
            raise caja['e']
        return caja['v']

    def archivar(self):
        """Archiva el ciclo en curso (spec §15) a petición: comando `archivar` de la consola o de cli_v4.py. True si la petición
        queda atendida (hecha o rechazada por una regla: cuentas con posición abierta, nada que archivar); False si falló por algo
        transitorio (red, cuota) y conviene reintentarla (V5.1)."""
        try:
            self.s.archivar_ciclo()
            return True
        except Exception as e:
            self.log(f"[ARCHIVO ERROR] No se ha archivado el ciclo: {type(e).__name__}: {e}")
            if isinstance(e, ValueError):
                return True
            self.ultimo_fallo = time.time()
            return False

    def vigilar_preoperativa(self):
        """Lee la celda de la preoperativa cada `preoperativa.lectura_s` (momento de elección, spec §7.2).
        La lectura tiene timeout corto (config preoperativa_timeout_s); tras un fallo (red, cuota) no se reintenta
        hasta pasados preoperativa_pausa_error_s, para no frenar el ciclo del cortafuegos. El error se registra
        como mucho una vez por minuto."""
        ahora = time.time()
        if ahora - self.ultima_preop < float(self.ent.reglas['preoperativa']['lectura_s']):
            return
        self.ultima_preop = ahora
        try:
            antes = almacen.get_estado(self.con, 'preop_estrategia', self.ent.reglas['estrategia_por_defecto'])
            valor = self.s.vigilar_preoperativa()
            if valor is not None and valor != antes:
                self.log(f"[PREOPERATIVA] Estrategia a usar: {valor}")
        except Exception as e:
            self.ultima_preop = ahora + float(self.ent.cfg.get('preoperativa_pausa_error_s', 30))
            if ahora - self.ultimo_error_preop > 60:
                self.ultimo_error_preop = ahora
                self.log(f"[PREOPERATIVA ERROR] {type(e).__name__}: {e}")

    def intentar_sync(self):
        """True si el sync se hizo."""
        try:
            self.s.sync()
            self.ultimo_fallo = 0.0
            return True
        except Exception as e:                      # red, cuota de la API, ATAS reescribiendo un cdb
            self.ultimo_fallo = time.time()
            self.log(f"[SYNC ERROR] {type(e).__name__}: {e}")
            return False

    def _atender(self, clave, accion):
        """Atiende una petición que cli_v4.py deja en `estado.<clave>` = '1'. La marca se borra ANTES de actuar (una petición que
        llegue mientras se actúa no se pierde) y se rearma si `accion()` devuelve False, con la misma espera que los reintentos
        automáticos para no martillear la API (V5.1: antes un fallo de red perdía la petición). True si se intentó."""
        if almacen.get_estado(self.con, clave) != '1' or time.time() - self.ultimo_fallo <= REINTENTO_S:
            return False
        almacen.set_estado(self.con, clave, '0')
        self.con.commit()
        if not accion():
            almacen.set_estado(self.con, clave, '1')
            self.con.commit()
        return True

    def paso(self):
        if self.tick():
            try:
                self._atender('archivar_ciclo', self.archivar)                 # lo pide cli_v4.py archivar
                forzado = self._atender('forzar_sync', self.intentar_sync)
                if not forzado and self.s.pendiente() and time.time() - self.ultimo_fallo > REINTENTO_S:
                    self.intentar_sync()
                sync.backup_diario(self.ent, self.con, self.log)
            except Exception as e:
                self.log(f"[CICLO ERROR] {type(e).__name__}: {e}")
        self.vigilar_preoperativa()                 # independiente: un fallo de la ingesta no la bloquea
        self.turno()


def crear_r17(ent, hoja, log):
    """ServicioR17 (spec §13), o None si R17 está apagado (`preoperativa.r17.activo`) o su configuración no vale. R17 es
    informativo: con la configuración mal (reglas.yaml o config.yaml) se registra `[R17 ERROR]` y el demonio arranca sin él."""
    try:
        if not ent.reglas['preoperativa']['r17']['activo']:
            return None
        return r17.ServicioR17(ent, hoja, log)
    except Exception as e:
        log(f"[R17 ERROR] Configuración de R17 no válida; el demonio arranca sin R17: {type(e).__name__}: {e}")
        return None


def iniciar_r17(servicio, con, parar):
    """Arranca el hilo de R17 (spec §13): hilo propio y cliente de Sheets propio, así que nada de lo que le pase llega al ciclo
    del demonio. Parte de la celda de la estrategia guardada por el último sync; el sync de este arranque la actualiza."""
    servicio.fijar_celda(almacen.get_estado(con, 'preop_celda'))
    hilo = threading.Thread(target=servicio.bucle, args=(parar,), daemon=True, name='R17')
    hilo.start()
    return hilo


def main():
    ent = config.cargar_entorno()
    OCULTAR.update(valores_a_ocultar(ent.cfg))
    if not os.path.isdir(ent.logs_dir):             # V5.1: sin esto, una ruta mal puesta deja el demonio «vivo» sin ingerir nada
        print(f"No encuentro la carpeta de ATAS ({ent.atas_dir}). Revisa `atas_dir` en config.local.yaml.")
        time.sleep(5)
        return
    oneshot = '--oneshot' in sys.argv
    if not oneshot and instancia_unica(ent.cfg['mutex']) is None:
        print("Ya hay un Demonio ATAS (V4/V5) en marcha. Cierra esa ventana primero.")
        time.sleep(5)
        return
    con = almacen.conectar(ent.sqlite_path)
    hoja = sheets.HojaV4(ent)
    servicio_r17 = crear_r17(ent, hoja, log)
    s = sync.Sincronizador(ent, con, hoja, log=log, r17=servicio_r17)
    log('=' * 60)
    log(f"Demonio ATAS V5.1 -> pestaña '{ent.cfg['pestana']}'")
    ciclo = Ciclo(ent, con, s, log)
    if not oneshot:
        hoja.corredor = ciclo.corredor      # la red, en un hilo auxiliar: el cortafuegos sigue durante el sync
        log(f"[TURNO] Turno de operativa abierto ({ciclo.abrir_turno()} UTC).")
    try:                    # spec §16.10: la foto del cálculo se comprueba antes del primer sync y se rehace si no coincide
        s.ingerir()
        s.verificar_foto()
    except Exception as e:
        almacen.guardar_foto(con, {}, {}, '')
        log(f"[FOTO ERROR] {type(e).__name__}: {e}. Sin foto: el demonio calcula todo.")
    ciclo.intentar_sync()
    try:
        sync.backup_diario(ent, con, log)
    except Exception as e:
        log(f"[BACKUP ERROR] {type(e).__name__}: {e}")
    if oneshot:
        return

    ordenes = []
    parar = threading.Event()

    def consola():
        try:
            while not parar.is_set():
                ordenes.append(input().strip().lower())
        except (EOFError, KeyboardInterrupt):
            ordenes.append('q')

    threading.Thread(target=consola, daemon=True).start()
    if servicio_r17:
        try:
            iniciar_r17(servicio_r17, con, parar)
            log(f"[R17] Hilo en marcha (cada {servicio_r17.intervalo_s:g} s, solo lectura de {servicio_r17.carpeta}).")
        except Exception as e:
            log(f"[R17 ERROR] No se pudo arrancar el hilo de R17; el demonio sigue sin él: {type(e).__name__}: {e}")
    print('\n' + '=' * 62)
    print(' DEMONIO ATAS V5.1 (-> Registro_bot_V5, pestaña V5) ACTIVO')
    print(' Comandos: sync | archivar (cierra el ciclo) | q (salir).  Acciones manuales: cli_v4.py')
    print('=' * 62 + '\n')
    try:
        while True:
            while ordenes:
                cmd = ordenes.pop(0)
                if cmd in ('q', 'salir', 'exit', 'quit'):
                    return
                if cmd == 'sync':
                    ciclo.intentar_sync()
                if cmd == 'archivar':
                    ciclo.archivar()
            ciclo.paso()
            time.sleep(float(ent.cfg['intervalo_s']))
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()
        try:
            ciclo.cerrar_turno()
        except Exception as e:
            log(f"[TURNO ERROR] {type(e).__name__}: {e}")
        log('[SALIDA] Demonio V5 cerrado.')


if __name__ == '__main__':
    main()
