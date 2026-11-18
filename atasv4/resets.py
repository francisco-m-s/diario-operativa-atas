"""Reset de una cuenta (spec §14, 03/11/2026): cuenta lógica `<ID físico>-resetN`.

Apex resetea una evaluación conservando el ID de Rithmic. A partir de `desde` (hora del log de ATAS, Madrid, reloj del PC)
todo evento de la cuenta física pertenece a la cuenta lógica nueva; lo anterior, a la anterior. `cuenta_logica` es el ÚNICO
sitio donde se traduce (cuenta física, hora) -> cuenta lógica: la ingesta (almacen.py) lo aplica a cada evento antes de
guardarlo, la reasignación retroactiva (almacen.reasignar_resets) usa los mismos cortes, y de ahí en adelante todo el código
trabaja con el ID lógico (la columna `cuenta` de SQLite ya es la lógica).

Registro (cuentas.yaml, lo mantiene el sistema): por cuenta física, la lista de resets con su `desde` y su `nombre`:

    resets:
      APEX-91120-74:
        - {desde: '2026-10-28 21:06:59.587', nombre: APEX-91120-74-reset1}

Este módulo es puro (sin SQLite): `registro` es ese diccionario (`ent.resets`). El formato del sufijo vive en
reglas.yaml -> reset.sufijo."""

import json
from datetime import datetime
from functools import lru_cache

from . import fuentes

CLAVE_ESTADO = 'resets_aplicados'          # clave de `estado`: huella del registro con el que se reasignó SQLite


def normalizar_desde(texto):
    """`YYYY-MM-DD HH:MM:SS[.mmm]` (o el datetime que YAML lee de una fecha sin comillas) -> `YYYY-MM-DD HH:MM:SS.mmm`.
    ValueError si no es una hora en ese formato."""
    if isinstance(texto, datetime):
        return fuentes.fmt_ts(texto.replace(tzinfo=None))
    for formato in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return fuentes.fmt_ts(datetime.strptime(str(texto).strip(), formato))
        except ValueError:
            pass
    raise ValueError(f"Hora no válida: {texto!r} (formato YYYY-MM-DD HH:MM:SS[.mmm], hora del log, Madrid)")


@lru_cache(maxsize=256)
def desde_utc(desde):
    """El corte (Madrid) pasado a UTC, para compararlo con las horas de los .cdb (spec §14.1)."""
    return fuentes.fmt_ts(fuentes.madrid_a_utc(fuentes.parse_ts(desde)))


def cortes(registro, fisica):
    """[(desde, nombre)] de los resets de una cuenta física, por `desde` ascendente."""
    return sorted((normalizar_desde(r['desde']), r['nombre']) for r in (registro or {}).get(fisica) or [])


def cuenta_logica(registro, fisica, t, utc=False):
    """Cuenta lógica de un evento de la cuenta física `fisica` a la hora `t`: la del último reset con `desde` <= t; sin reset
    anterior a t (o sin resets), la propia cuenta física. `t`: hora del log (Madrid) o, con `utc=True`, UTC (.cdb), que se
    compara con `desde` pasado a UTC. Es el único punto de verdad de la traducción física -> lógica (spec §14.1)."""
    if not registro or fisica not in registro:
        return fisica
    logica = fisica
    for desde, nombre in cortes(registro, fisica):
        if t >= (desde_utc(desde) if utc else desde):
            logica = nombre
        else:
            break
    return logica


def logicas(registro, fisica):
    """Todas las cuentas lógicas de una cuenta física, de la más antigua a la más reciente: ella misma y sus resets."""
    return [fisica] + [nombre for _, nombre in cortes(registro, fisica)]


def logica_actual(registro, fisica):
    """La lógica más reciente de una cuenta física: a ella pertenece lo que no tiene hora (Portfolio.cdb, spec §14.1)."""
    return logicas(registro, fisica)[-1]


def fisica_de(registro, cuenta):
    """Cuenta física de una cuenta lógica (la propia cuenta si no es un reset)."""
    for fisica, lista in (registro or {}).items():
        if any(r['nombre'] == cuenta for r in lista or []):
            return fisica
    return cuenta


def es_reset(registro, cuenta):
    return fisica_de(registro, cuenta) != cuenta


def corte_de(registro, cuenta):
    """`desde` de una cuenta lógica de reset; None si no lo es."""
    fisica = fisica_de(registro, cuenta)
    return next((d for d, n in cortes(registro, fisica) if n == cuenta), None) if fisica != cuenta else None


def nombre_siguiente(registro, fisica, sufijo):
    """Nombre del próximo reset de `fisica` (reglas.yaml -> reset.sufijo, p. ej. '-reset{n}')."""
    return fisica + sufijo.format(n=len(cortes(registro, fisica)) + 1)


def anotar(registro, fisica, desde, nombre):
    """Añade el reset al registro (una entrada por cuenta física, en orden)."""
    registro.setdefault(fisica, []).append({'desde': desde, 'nombre': nombre})
    registro[fisica].sort(key=lambda r: normalizar_desde(r['desde']))


def expandir_pares(registro, pares):
    """[(líder, seguidora)] de CopyTrading.cnf (IDs físicos) -> con cada ID físico en todas sus cuentas lógicas. La
    configuración actual figura para la cuenta física: la original y sus resets heredan el mismo papel (lo anterior al reset
    se juzga como antes, spec §4.1)."""
    if not registro:
        return pares
    return [(lg, sg) for lider, seg in pares
            for lg in logicas(registro, lider) for sg in logicas(registro, seg)]


def huella(registro):
    """Texto estable del registro (cortes y nombres): `estado.resets_aplicados` lo compara para saber si SQLite está
    reasignado con el registro vigente. Sin resets, ''."""
    plano = {f: cortes(registro, f) for f in sorted(registro or {}) if registro[f]}
    return json.dumps(plano, ensure_ascii=False) if plano else ''
