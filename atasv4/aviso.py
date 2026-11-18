"""Aviso en vivo en Windows: notificación (toast) si `cortafuegos.aviso.notificacion` y sonido del sistema si
`cortafuegos.aviso.sonido`. Sin servicios externos. Desde el 11/11/2026 (orden del operador) los dos van apagados en
reglas.yaml: el evento solo queda en el log del demonio (`[CORTAFUEGOS]`) y en el sheet."""

import os
import queue
import subprocess
import threading
import time
import winsound

# AppUserModelID de Windows PowerShell: permite mostrar toasts sin registrar una aplicación propia.
_AUMID = r'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode('{titulo}')) > $null
$t.Item(1).AppendChild($x.CreateTextNode('{texto}')) > $null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{aumid}').Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def _ps(s):
    return str(s).replace("'", "''")


def _sonar(sonido):
    try:
        winsound.PlaySound(sonido, winsound.SND_ALIAS | winsound.SND_ASYNC)
    except RuntimeError:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


def _insistir(sonido, duracion_s, cada_s, dormir=time.sleep):
    """Repite el sonido hasta cubrir `duracion_s`. Un pitido suelto se ignora (decisión del operador)."""
    fin = time.monotonic() + max(0.0, duracion_s)
    while True:
        _sonar(sonido)
        if time.monotonic() + cada_s >= fin:
            return
        dormir(cada_s)


def notificar(titulo, texto, sonido, duracion_s=0.0, cada_s=1.2, toast=True):
    """No bloquea: el sonido insiste en un hilo aparte y el toast lo lanza un PowerShell oculto. Sin `sonido` (vacío en
    reglas.yaml) no suena nada: solo el toast."""
    if not sonido:
        pass
    elif duracion_s and cada_s > 0:
        threading.Thread(target=_insistir, args=(sonido, duracion_s, cada_s), daemon=True).start()
    else:
        _sonar(sonido)
    if not toast:
        return
    script = _TOAST.format(titulo=_ps(titulo), texto=_ps(texto), aumid=_AUMID)
    subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                      '-Command', script], creationflags=subprocess.CREATE_NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_TOAST_MUDO = _TOAST.replace(
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier",
    "$a = $x.CreateElement('audio'); $a.SetAttribute('silent', 'true'); $x.DocumentElement.AppendChild($a) > $null\n"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier")


def _reproducir(ruta, veces):
    """El wav, `veces` veces seguidas (cada una entera). Lo llama el hilo de la cola de sonidos: no para el ciclo del demonio."""
    for _ in range(max(1, int(veces))):
        try:
            winsound.PlaySound(ruta, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        except RuntimeError:
            return


# Cola única de sonidos (11/11/2026, revisión V5, hallazgo 8): un solo hilo los reproduce de uno en uno, cada uno entero y sus
# `veces`. Con varios avisos a la vez (al abrir: FUERA DE HORARIO + SIN ESTRATEGIA + CONSEJO IGNORADO), suenan seguidos en vez de
# cortarse entre sí.
_COLA = queue.Queue()
_HILO = None
_CERROJO = threading.Lock()


def _sonador(cola):
    """Cuerpo del hilo de sonidos: reproduce lo que llega a la cola, en orden. `None` lo para (solo lo usan los tests)."""
    while True:
        item = cola.get()
        if item is None:
            return
        _reproducir(*item)


def _encolar(ruta, veces):
    global _HILO
    with _CERROJO:
        if _HILO is None or not _HILO.is_alive():
            _HILO = threading.Thread(target=_sonador, args=(_COLA,), daemon=True, name='sonidos')
            _HILO.start()
    _COLA.put((ruta, veces))


def notificar_v5(titulo, texto, wav=None, veces=1, toast=True):
    """Aviso de la V5 (spec §16.4): notificación de Windows SIN su tono (Windows no admite un wav propio en la notificación
    de un programa sin empaquetar) y, aparte, el wav de ATAS que toque. `wav` None o inexistente: sin sonido."""
    if toast:
        script = _TOAST_MUDO.format(titulo=_ps(titulo), texto=_ps(texto), aumid=_AUMID)
        subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                          '-Command', script], creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wav and os.path.isfile(wav):
        _encolar(wav, veces)            # en la cola única: no corta al que esté sonando


def avisador(R):
    """Función avisar(titulo, texto) con la configuración de reglas.yaml."""
    a = R['cortafuegos']['aviso']
    duracion = float(a.get('duracion_s', 0) or 0)
    cada = float(a.get('repetir_cada_s', 1.2) or 1.2)
    toast = bool(a.get('notificacion', True))
    A5 = (R.get('v5') or {}).get('avisos') or {}

    def avisar(titulo, texto, sonido=None):
        """`sonido` None: evento de la V4 (hoy, sin sonido ni notificación). Con valor ('' incluido): aviso de la V5, con la
        notificación muda y el wav de esa clave de `v5.avisos.sonidos` ('' = sin sonido)."""
        if sonido is not None:
            nombre = (A5.get('sonidos') or {}).get(sonido)
            wav = os.path.join(str(A5.get('carpeta') or ''), nombre) if nombre else None
            notificar_v5(titulo, texto, wav, A5.get('veces', 1), bool(A5.get('notificacion', True)))
        elif a.get('sonido') or toast:
            notificar(titulo, texto, a.get('sonido') or None, duracion, cada, toast)

    return avisar
