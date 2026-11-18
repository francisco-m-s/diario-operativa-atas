"""R17 y stop calibrado en la preoperativa (spec §13). INFORMATIVO: no es una regla del Plan 4 ni se guarda en SQLite.

Lee las M204s que escribe el indicador de ATAS (`<carpeta>\\YYYY-MM-DD.jsonl`, solo lectura: nunca se abre para escribir ni se
bloquea) y deja en la celda de la derecha de la de `Estrategia a usar` un texto con R17, el stop calibrado y la hora (Madrid) del
cierre de la última vela usada. Corre en un hilo propio, con su cliente de Sheets (timeout corto, pausa tras error): un fallo
de aquí no frena el ciclo del demonio, ni el cortafuegos, ni la lectura de la preoperativa. Todo número y texto sale de
reglas.yaml -> preoperativa.r17."""

import contextlib
import json
import os
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from . import fuentes, vista

Vela = namedtuple('Vela', 'ap_ms h l')      # apertura en ms desde 1970 (UTC), máximo y mínimo
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def a_ms(apertura):
    """'2026-10-09T11:33:00.000Z' -> ms enteros desde 1970 (UTC). ValueError/TypeError si no es una hora con zona."""
    d = datetime.fromisoformat(apertura.replace('Z', '+00:00')) - EPOCH
    return (d.days * 86400 + d.seconds) * 1000 + d.microseconds // 1000


def hora_madrid(ms):
    """ms desde 1970 (UTC) -> 'HH:MM:SS' de Europe/Madrid."""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(fuentes.MADRID).strftime('%H:%M:%S')


class LectorDia:
    """Un fichero diario de M204s leído de forma incremental: se recuerda la posición y solo se lee lo añadido. De cada
    apertura se queda con la última versión del fichero (la más reciente). Una línea a medio escribir (sin salto de línea)
    espera a la siguiente lectura. Si el fichero se encoge (sustituido), se vuelve a leer entero. Solo se abre en modo 'rb'."""

    def __init__(self, ruta):
        self.ruta = ruta
        self.pos = 0
        self.velas = {}                 # apertura ms -> Vela
        self.corruptas = 0

    def leer(self):
        try:
            tam = os.path.getsize(self.ruta)
        except OSError:                 # el fichero aún no existe (día sin velas): lo ya leído sigue valiendo
            return
        if tam < self.pos:
            self.pos, self.velas = 0, {}
        if tam == self.pos:
            return
        with open(self.ruta, 'rb') as f:
            f.seek(self.pos)
            datos = f.read(tam - self.pos)
        fin = datos.rfind(b'\n')
        if fin < 0:
            return
        self.pos += fin + 1
        for linea in datos[:fin].split(b'\n'):
            if not linea.strip():
                continue
            try:
                d = json.loads(linea)
                v = Vela(a_ms(d['apertura']), float(d['h']), float(d['l']))
            except (ValueError, TypeError, KeyError, AttributeError):
                self.corruptas += 1
                continue
            self.velas[v.ap_ms] = v


def analizar(velas, marco_ms, n):
    """(última, ventana, válida) de {apertura ms: Vela}.
    - última: la vela de apertura más reciente (None si no hay ninguna).
    - ventana: las `n` velas más recientes de SU rejilla (apertura - la más reciente, múltiplo exacto del marco) si son
      contiguas (separadas exactamente `marco_ms`); None si no (hueco o menos de `n`).
    - válida: la última vela de la rejilla que cierra una ventana de `n` contiguas (None si nunca la hubo)."""
    if not velas:
        return None, None, None
    ref = max(velas)
    rejilla = sorted(ap for ap in velas if (ref - ap) % marco_ms == 0)
    racha, previa, valida = 0, None, None
    for ap in rejilla:
        racha = racha + 1 if previa is not None and ap - previa == marco_ms else 1
        previa = ap
        if racha >= n:
            valida = ap
    ventana = [velas[ap] for ap in rejilla[-n:]] if racha >= n else None
    return velas[ref], ventana, None if valida is None else velas[valida]


def calcular(ventana, tick, coef, exponente):
    """(R17, stop) en ticks enteros: R17 = (máx de H − mín de L) / tick; stop = coef × R17^exponente al tick entero."""
    r17 = round(max(v.h for v in ventana) / tick) - round(min(v.l for v in ventana) / tick)
    return r17, int(coef * r17 ** exponente + 0.5)


class ServicioR17:
    """Calcula el texto de R17 y lo escribe en el sheet (solo cuando cambia). `hoja`: HojaV4 (o doble) con `leer_fila_r17` y
    `escribir_celda_r17`, que usa su cliente propio (un hilo no comparte cliente HTTP con el principal).

    Toda la configuración se valida al construirlo: si algo no vale, lanza y el demonio arranca sin R17 (`demonio_v4.crear_r17`).

    Concurrencia con el sync (spec §13): el sync reescribe la pestaña entera. `cerrojo` serializa esa reescritura con la
    comprobación + escritura de aquí, para que una fila movida por el sync no reciba el texto fuera de sitio. El cerrojo solo se
    mantiene mientras dura la E/S de red y con un PLAZO DE RELOJ DE PARED (`r17_plazo_s`): la E/S va en un hilo auxiliar al que se
    espera con `join(plazo)`; si no vuelve, se abandona y se suelta el cerrojo. (El timeout de httplib2 es por operación de
    socket: una respuesta que gotea un byte cada pocos segundos no lo dispara nunca.) El sync espera al cerrojo como mucho
    `r17_espera_sync_s` (`exclusivo`), siempre más que el plazo. El sync deja el último texto en la celda (`texto_escrito`)."""

    def __init__(self, ent, hoja, log=print):
        C = self.cfg = ent.reglas['preoperativa']['r17']
        self.etiqueta = ent.reglas['preoperativa']['estrategia']['etiqueta']
        self.hoja, self.log = hoja, log
        self.carpeta = str(C['carpeta'])
        self.marco_ms = int(C['marco_s']) * 1000
        self.n_velas = int(C['velas'])
        self.tick = float(C['tick'])
        self.coef, self.exponente = float(C['stop']['coef']), float(C['stop']['exponente'])
        self.antiguedad_ms = float(C['antiguedad_max_s']) * 1000
        self.prefijo = str(C['prefijo'])
        self.t_texto, self.t_sin_desde, self.t_sin = str(C['texto']), str(C['sin_datos_desde']), str(C['sin_datos'])
        self.t_texto.format(r17=0, stop=0, hora='00:00:00')                 # plantillas con campos que no existen: falla ya
        self.t_sin_desde.format(hora='00:00:00')
        self.intervalo_s = float(ent.cfg.get('r17_intervalo_s', 5))
        self.espera_sync_s = float(ent.cfg.get('r17_espera_sync_s', 10))
        self.plazo_s = min(float(ent.cfg.get('r17_plazo_s', 6)), self.espera_sync_s)    # el cerrojo se suelta antes de que el sync deje de esperar
        self.pausa_error_s = float(ent.cfg.get('preoperativa_pausa_error_s', 30))
        if min(self.marco_ms, self.n_velas, self.tick, self.antiguedad_ms, self.intervalo_s, self.espera_sync_s,
               self.plazo_s, self.pausa_error_s) <= 0 or not self.prefijo:
            raise ValueError('R17: marco, velas, tick, antigüedad, intervalo, plazos, pausa y prefijo han de ser positivos')
        self.cerrojo = threading.Lock()
        self.log_fallido = 0            # veces que el log del demonio lanzó al anotar algo de R17
        self._celda = None              # A1 de la celda de la estrategia (la fija el sync)
        self._escrito = None            # texto que se cree puesto en el sheet
        self._pausa_hasta = 0.0
        self._ultimo_error = 0.0
        self._avisadas = set()          # avisos de «no escribo» ya dados desde el último sync
        self._red = None                # hilo auxiliar de la última petición a Sheets (vivo si quedó colgada)
        self._lectores = {}             # fecha UTC -> LectorDia (hoy y ayer)

    # --- cálculo ---
    def texto(self, ahora_s):
        """Texto de la celda en el instante `ahora_s` (epoch, reloj del PC; la tolerancia de la antigüedad absorbe su desfase)."""
        hoy = datetime.fromtimestamp(ahora_s, timezone.utc).date()
        fechas = (hoy - timedelta(days=1), hoy)         # el de ayer, por el cruce de medianoche UTC
        self._lectores = {d: self._lectores.get(d) or LectorDia(os.path.join(self.carpeta, f'{d:%Y-%m-%d}.jsonl'))
                          for d in fechas}
        velas = {}
        for lector in self._lectores.values():
            lector.leer()
            velas.update(lector.velas)
        ultima, ventana, valida = analizar(velas, self.marco_ms, self.n_velas)
        if ventana is not None and ahora_s * 1000 - (ultima.ap_ms + self.marco_ms) <= self.antiguedad_ms:
            r17, stop = calcular(ventana, self.tick, self.coef, self.exponente)
            return self.t_texto.format(r17=r17, stop=stop, hora=hora_madrid(ultima.ap_ms + self.marco_ms))
        if valida is None:
            return self.t_sin
        return self.t_sin_desde.format(hora=hora_madrid(valida.ap_ms + self.marco_ms))

    # --- estado compartido con el sync ---
    def fijar_celda(self, a1):
        """Celda de la estrategia tal como la dejó el sync (o la guardada al arrancar). Tras un sync la fila está donde dice."""
        if a1:
            self._celda = a1
        self._pausa_hasta = 0.0
        self._avisadas = set()

    def sembrar(self, texto):
        """Texto de R17 que el sync ha leído en el sheet (empieza por el prefijo): hasta que R17 escriba otro, es el que hay, así
        que ni el sync lo borra ni R17 lo reescribe si no cambia. Sin efecto si R17 ya ha escrito en esta ejecución."""
        if texto and self._escrito is None:
            self._escrito = texto

    def texto_escrito(self):
        """Último texto puesto en el sheet ('' si ninguno): el sync lo vuelve a escribir con el resto de la pestaña."""
        return self._escrito or ''

    @contextlib.contextmanager
    def exclusivo(self):
        """Para el sync: no hay escritura de R17 a medias mientras reescribe la pestaña. Espera como mucho `espera_sync_s`."""
        tomado = self.cerrojo.acquire(timeout=self.espera_sync_s)
        try:
            yield
        finally:
            if tomado:
                self.cerrojo.release()

    # --- hilo ---
    def _anotar(self, msg):
        """Log que no puede tumbar el hilo: si el del demonio lanza, queda en la consola (stderr) y se cuenta."""
        try:
            self.log(msg)
        except Exception:
            self.log_fallido += 1
            with contextlib.suppress(Exception):
                sys.stderr.write(f'{msg}\n')

    def _fallo(self, ahora_s, detalle):
        """Pausa del hilo y un log como mucho por minuto."""
        self._pausa_hasta = ahora_s + self.pausa_error_s
        if ahora_s - self._ultimo_error > 60:
            self._ultimo_error = ahora_s
            self._anotar(f'[R17 ERROR] {detalle}')

    def _avisar_una_vez(self, clave, msg):
        if clave not in self._avisadas:
            self._avisadas.add(clave)
            self._anotar(msg)

    def paso(self, ahora_s=None):
        """Un paso del hilo. Nunca lanza: un fallo de ficheros, de cálculo o de red se registra (como mucho una vez por
        minuto) y deja al hilo en pausa `preoperativa_pausa_error_s`; nada de esto llega al ciclo del demonio."""
        ahora_s = time.time() if ahora_s is None else ahora_s
        if self._celda is None or ahora_s < self._pausa_hasta:
            return
        try:
            texto = self.texto(ahora_s)
            if texto != self._escrito:
                self._escribir(texto, ahora_s)
        except Exception as e:
            self._fallo(ahora_s, f'{type(e).__name__}: {e}')

    def _peticiones(self, a1, texto, cancelado):
        """E/S de red, en el hilo auxiliar: lee la fila de la estrategia, comprueba que sigue siendo ella y que la celda de R17
        está vacía o es de R17, y escribe SOLO esa celda. Devuelve qué pasó; no toca el estado ni el log del servicio. Si el
        dueño ya abandonó la espera (`cancelado`), no escribe."""
        fila, col = vista.celda_de_a1(a1)
        actual = self.hoja.leer_fila_r17(fila, col + 1)
        if cancelado.is_set():
            return ('cancelado',)
        if not actual or str(actual[0]).strip() != self.etiqueta:
            return ('etiqueta',)                 # filas movidas a mano (o pestaña sin el layout)
        destino = actual[col + 1] if len(actual) > col + 1 else ''
        destino = '' if destino is None else str(destino)
        if destino.strip() and not destino.startswith(self.prefijo):
            return ('destino', destino)          # p. ej. una columna insertada: ahí hay otra cosa
        if destino == texto:
            return ('igual',)
        if cancelado.is_set():
            return ('cancelado',)
        self.hoja.escribir_celda_r17(fila, col + 1, texto)
        return ('escrito',)

    def _escribir(self, texto, ahora_s):
        """Pone `texto` en la celda de R17. El cerrojo solo cubre la espera de la E/S, con plazo de reloj de pared; el estado y el
        log se tocan fuera de él."""
        if self._red is not None and self._red.is_alive():
            raise TimeoutError('la petición anterior a Sheets sigue sin volver')
        resultado, cancelado = {}, threading.Event()

        def peticiones():
            try:
                resultado['v'] = self._peticiones(a1, texto, cancelado)
            except Exception as e:
                resultado['e'] = e

        with self.cerrojo:
            a1 = self._celda
            self._red = threading.Thread(target=peticiones, daemon=True, name='R17-red')
            self._red.start()
            self._red.join(self.plazo_s)
            colgada = self._red.is_alive()
            if colgada:
                cancelado.set()
        if colgada:
            raise TimeoutError(f'Sheets no ha contestado en {self.plazo_s:g} s (plazo de reloj de pared); se abandona la petición')
        if 'e' in resultado:
            raise resultado['e']
        que, *detalle = resultado['v']
        if que in ('escrito', 'igual'):
            self._escrito = texto
        elif que == 'etiqueta':
            self._pausa_hasta = ahora_s + self.pausa_error_s     # no se escribe y se espera al siguiente sync
            self._avisar_una_vez(que, f"[R17] La fila '{self.etiqueta}' ya no está en {a1}: no se escribe hasta el siguiente sync.")
        elif que == 'destino':
            self._pausa_hasta = ahora_s + self.pausa_error_s
            self._avisar_una_vez(que, f"[R17] La celda de R17 junto a {a1} tiene otro contenido ({detalle[0][:40]!r}): "
                                      f"no se escribe hasta el siguiente sync (¿columna insertada?).")

    def bucle(self, parar):
        """Cuerpo del hilo: `paso` cada `r17_intervalo_s` hasta que `parar` (threading.Event) se active. El hilo no muere por
        una excepción (ni por un log que lanza): la anota como mucho una vez por minuto y sigue. Si acaba sin que se le pida,
        lo deja escrito."""
        try:
            while not parar.wait(self.intervalo_s):
                try:
                    self.paso()
                except Exception as e:        # paso ya recoge lo suyo: esto solo salva al hilo de un fallo imprevisto
                    ahora = time.time()
                    if ahora - self._ultimo_error > 60:
                        self._ultimo_error = ahora
                        self._anotar(f'[R17 ERROR] {type(e).__name__}: {e}')
        finally:
            if not parar.is_set():
                self._anotar('[R17 ERROR] El hilo de R17 ha terminado sin que se le pidiera.')
