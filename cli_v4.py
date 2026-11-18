"""Acciones manuales del operador sobre la V4.

  python cli_v4.py quemar <cuenta>
  python cli_v4.py retiro <importe> <cuenta>          (retiro de X $ de cuenta Y)
  python cli_v4.py sim activar|desactivar <cuenta_sim>
  python cli_v4.py reset <cuenta física> <desde>       (reset de Apex: cuenta lógica <ID>-resetN, spec §14)
  python cli_v4.py sync                                (forzar sincronización)
  python cli_v4.py archivar [--hasta YYYY-MM-DD]       (cierra el ciclo: copia la pestaña V4 a otra con sus fechas y empieza uno nuevo, spec §15)
  python cli_v4.py unificado                           (carga inicial o reparación del histórico unificado, spec §16.9; demonio parado)
  python cli_v4.py foto [--comprobar|--quitar]         (foto del cálculo de lo archivado, spec §16.10; demonio parado)
  python cli_v4.py cuentas                             (listar registro)

Toda acción que cambia datos fuerza una sincronización: la hace el demonio si está en marcha
o este mismo proceso si no lo está.

`reset` se usa con el demonio PARADO (como cualquier edición de cuentas.yaml; si está en marcha, se niega). <desde> es la
hora del log de ATAS (Madrid, reloj del PC), con la cuenta plana y sin operaciones alrededor del corte:
  python cli_v4.py reset APEX-91120-74 "2026-10-28 21:06:59.587"
Registra el reset en cuentas.yaml (N automático), reasigna lo ya ingerido desde esa hora y sincroniza.
"""

import argparse
import ctypes
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atasv4 import almacen, config, cuentas, sheets, sync  # noqa: E402

SYNCHRONIZE = 0x00100000


def demonio_activo(nombre):
    h = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, nombre)
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def forzar_sync(ent, con):
    if demonio_activo(ent.cfg['mutex']):
        almacen.set_estado(con, 'forzar_sync', '1')
        con.commit()
        print("Demonio V4 en marcha: sincronizará en el próximo ciclo.")
    else:
        sync.Sincronizador(ent, con, sheets.HojaV4(ent)).sync()


def main(argv=None):
    ap = argparse.ArgumentParser(description='Acciones manuales V4')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('quemar')
    p.add_argument('cuenta')
    p = sub.add_parser('retiro')
    p.add_argument('importe', type=lambda s: float(s.replace(',', '.')))
    p.add_argument('cuenta')
    p = sub.add_parser('sim')
    p.add_argument('accion', choices=['activar', 'desactivar'])
    p.add_argument('cuenta')
    p = sub.add_parser('reset')
    p.add_argument('cuenta')
    p.add_argument('desde', nargs='+', help='YYYY-MM-DD HH:MM:SS[.mmm], hora del log (Madrid); con o sin comillas')
    sub.add_parser('sync')
    p = sub.add_parser('archivar')
    p.add_argument('--hasta', type=date.fromisoformat, default=None,
                   help='YYYY-MM-DD: último día (Madrid) que entra en el archivo; solo con el demonio parado')
    sub.add_parser('unificado')
    p = sub.add_parser('foto')
    p.add_argument('--comprobar', action='store_true', help='solo compara el cálculo con foto con el completo')
    p.add_argument('--quitar', action='store_true', help='borra la foto: el demonio vuelve a calcularlo todo')
    sub.add_parser('cuentas')
    a = ap.parse_args(argv)

    ent = config.cargar_entorno()
    con = almacen.conectar(ent.sqlite_path)
    almacen.sembrar_quemadas(con, ent.cfg.get('quemadas_iniciales'), almacen.ahora_madrid())
    cuenta = getattr(a, 'cuenta', None)
    if cuenta and a.cmd != 'sim':
        cuenta = ent.cuenta_por_nombre_visible(cuenta) or cuenta

    if a.cmd == 'quemar':
        cuentas.quemar(ent, con, cuenta)
        print(f"{cuenta} quemada: sale del BAL (sus operaciones se conservan).")
    elif a.cmd == 'retiro':
        nuevo = cuentas.retiro(ent, con, cuenta, a.importe)
        print(f"Retiro de {a.importe:.2f} $ registrado en {cuenta}. Nombre ahora: {nuevo}.")
    elif a.cmd == 'reset':
        if demonio_activo(ent.cfg['mutex']):
            sys.exit("El demonio V4 está en marcha: páralo antes de registrar un reset (spec §14.2).")
        try:
            nombre, movidas, cambian = cuentas.reset(ent, con, cuenta, ' '.join(a.desde))
        except ValueError as e:
            sys.exit(f"Error: {e}")
        print(f"Reset registrado: {nombre} (desde {' '.join(a.desde)}). Filas reasignadas: "
              + (', '.join(f"{t} {n}" for t, n in movidas.items() if n) or 'ninguna') + '.')
        if cambian:
            print(f"AVISO: {len(cambian)} operaciones ya anotadas tras el corte cambian de ID y sus columnas manuales del "
                  f"sheet no se trasladan: {', '.join(cambian)}.")
    elif a.cmd == 'archivar':
        if demonio_activo(ent.cfg['mutex']):
            if a.hasta:
                sys.exit("Con --hasta, el demonio tiene que estar parado.")
            almacen.set_estado(con, 'archivar_ciclo', '1')
            con.commit()
            print("Demonio V4 en marcha: archivará el ciclo en el próximo paso (mira su ventana o atas_v4.log).")
        else:
            try:
                nombre, n = sync.Sincronizador(ent, con, sheets.HojaV4(ent)).archivar_ciclo(hasta=a.hasta)
            except ValueError as e:
                sys.exit(f"No se archiva: {e}.")
            print(f"Ciclo archivado en la pestaña '{nombre}' ({n} decisiones). La pestaña '{ent.cfg['pestana']}' empieza ciclo nuevo.")
        return
    elif a.cmd == 'foto':
        if demonio_activo(ent.cfg['mutex']):
            sys.exit("El demonio está en marcha: páralo antes de tocar la foto del cálculo.")
        s = sync.Sincronizador(ent, con, None)
        s.ingerir()
        if a.quitar:
            almacen.guardar_foto(con, {}, {}, '')
            print('Foto borrada: el demonio vuelve a calcularlo todo.')
        elif a.comprobar:
            difs = s.comprobar_foto()
            print('La foto coincide con el cálculo completo.' if not difs else
                  'DIFERENCIAS entre la foto y el cálculo completo:\n  ' + '\n  '.join(difs))
        else:
            n, m, difs = s.hacer_foto()
            print(f'Foto hecha y comprobada: {n} operaciones, {m} decisiones.' if not difs else
                  'La foto NO coincide con el cálculo completo y se ha descartado:\n  ' + '\n  '.join(difs))
        return
    elif a.cmd == 'unificado':
        if demonio_activo(ent.cfg['mutex']):
            sys.exit("El demonio está en marcha: páralo antes de cargar el histórico unificado.")
        n, antiguas, archivadas = sync.Sincronizador(ent, con, sheets.HojaV4(ent)).cargar_unificado()
        print(f"Histórico unificado: {n} filas añadidas ({antiguas} decisiones antiguas leídas, {archivadas} de ciclos archivados).")
        return
    elif a.cmd == 'sim':
        cuentas.sim(ent, cuenta, a.accion == 'activar')
        print(f"Sim {cuenta}: {'activada' if a.accion == 'activar' else 'desactivada'}. Sims activas: {ent.cuentas['sim_activas']}")
    elif a.cmd == 'cuentas':
        q = almacen.quemadas(con)
        for c, f in sorted(ent.cuentas['cuentas'].items()):
            estado = 'QUEMADA' if c in q else ('sim' if ent.es_sim(c) else 'viva')
            print(f"{ent.nombre_visible(c):32s} {estado:8s} {f}")
        return
    forzar_sync(ent, con)


if __name__ == '__main__':
    main()
