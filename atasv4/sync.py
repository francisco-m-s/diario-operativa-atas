"""Ingesta + cálculo + escritura. Orden de escritura (spec §1): leer manuales del sheet -> SQLite,
después reescribir la pestaña entera desde SQLite."""

import contextlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

from . import almacen, cortafuegos, cuentas, fuentes, motor, reloj, resets, v5 as mv5, vista


def ahora_utc():
    """Reloj del PC (UTC). Donde se cruza con horas del exchange pasa por el reloj ATAS (reloj.py)."""
    return fuentes.fmt_ts(datetime.now(timezone.utc).replace(tzinfo=None))


class Sincronizador:
    def __init__(self, ent, con, hoja=None, log=print, r17=None):
        self.ent = ent
        self.con = con
        self.hoja = hoja
        self.log = log
        self.r17 = r17          # ServicioR17 (spec §13) cuando corre su hilo; None en el CLI y en los tests
        self._avisos_dichos = set()     # avisos del cálculo ya escritos en el log en esta ejecución (no se repiten)
        self._hasta_vista = None        # solo mientras se archiva hasta un día (archivar_ciclo): tope de la pestaña
        self.turno_inicio = None        # UTC del arranque del turno en curso (lo pone el demonio; None en el CLI): spec §16.2
        self.desde_utc = almacen.inicio_utc(ent.inicio_sesion)
        self.desde_madrid = almacen.inicio_madrid(ent.inicio_sesion)
        almacen.sembrar_quemadas(con, ent.cfg.get('quemadas_iniciales'), almacen.ahora_madrid())
        almacen.migrar_estrategia_asignada(con)
        almacen.migrar_orden_linea(con)
        if almacen.get_estado(con, almacen.MIGRACION_RELECTURA) != '1':
            n, faltan = almacen.migrar_relectura(con, ent.logs_dir, ent.logs_respaldo_dir, self.desde_madrid,
                                                 ent.resets)
            log(f"[MIGRACIÓN] Se releen los logs de ATAS: 'Order trade', arranques, saltos del reloj y orden de línea "
                f"({n} filas de la copia de logs borrados).")
            if faltan:
                log(f"[MIGRACIÓN AVISO] Faltan {len(faltan)} logs ya leídos, ni en ATAS ni en la copia "
                    f"({ent.logs_respaldo_dir}): {', '.join(faltan)}. Sus filas se quedan sin orden de línea. "
                    f"La migración NO se da por hecha: se repite en cada arranque hasta que estén en la copia.")
        m = almacen.migrar_atas_8015(con)
        if m is not None:
            log(f"[MIGRACIÓN] ATAS 8.0.15 (Id del fill <OrderId>_<número>): {m[0]} Id de MyTrade pasados al número; "
                f"logs que se releen: {', '.join(m[1]) or 'ninguno'}.")

    # --- ingesta ---
    def ingerir(self, forzar_cdb=False):
        # spec §14: lo ingerido se parte en cuentas lógicas con el registro vigente (cuentas.yaml); si ese registro cambió desde
        # la última reasignación (el CLI lo hace él mismo; una edición a mano no), se reasigna antes de ingerir
        if almacen.aplicar_resets(self.con, self.ent.resets) is not None:
            self.log("[RESET] SQLite reasignado con los resets de cuentas.yaml.")
        n = almacen.ingerir_logs(self.con, self.ent.logs_dir, self.desde_madrid, self.ent.resets)
        n += almacen.ingerir_cdb(self.con, self.ent.db_dir, self.desde_utc, forzar=forzar_cdb, resets=self.ent.resets)
        avisos = []
        altas = cuentas.registrar_nuevas(self.ent, self.con, self.sesion(ahora_utc()), avisos)
        for c in altas:
            self.log(f"[CUENTA] Alta automática: {c} -> {self.ent.ficha(c)}")
        for a in avisos:
            self.log(a)
        self._completar_elegidas()
        return n

    def sesion(self, t_utc):
        """Sesión CME de una hora del reloj del PC, pasada a la del exchange (cambia a las 17:00 CT del exchange)."""
        return motor.sesion_cme(reloj.cargar(self.con, self.ent.reglas).pc_a_utc(t_utc))

    def _completar_elegidas(self):
        """Asignaciones de la preoperativa anteriores a la columna t_elegida_exch_utc (28/09/2026): se rellena con el
        reloj ATAS, cuando ya está la relectura de los logs. La marca de tardía se conserva tal como se decidió."""
        filas = self.con.execute("SELECT op_id, t_elegida_utc FROM estrategia_asignada WHERE t_elegida_exch_utc IS NULL "
                                 "AND t_elegida_utc IS NOT NULL AND t_elegida_utc<>''").fetchall()
        if not filas:
            return
        rel = reloj.cargar(self.con, self.ent.reglas)
        for r in filas:
            self.con.execute('UPDATE estrategia_asignada SET t_elegida_exch_utc=? WHERE op_id=?',
                             (rel.pc_a_utc(r['t_elegida_utc']), r['op_id']))
        self.con.commit()
        self.log(f"[PREOPERATIVA] Hora del exchange de la elección rellenada en {len(filas)} asignaciones antiguas.")

    def pares_copia(self):
        """CopyTrading.cnf trae IDs físicos: cada uno figura en todas sus cuentas lógicas (resets.expandir_pares)."""
        return resets.expandir_pares(self.ent.resets, fuentes.leer_copytrading(self.ent.atas_dir))

    def thresholds_manuales(self):
        return {r['cuenta']: r['valor'] for r in self.con.execute('SELECT cuenta, valor FROM threshold_manual')}

    def vigilar(self, avisar, t_utc=None):
        """Cortafuegos en vivo: cálculo desde SQLite y avisos nuevos (no escribe en el sheet)."""
        t_utc = t_utc or ahora_utc()
        res = self.calcular(t_utc)
        cortafuegos.vigilar(self.con, res, t_utc, self.ent.reglas, avisar, self.log)
        return res

    def calcular(self, t_utc=None, usar_foto=True, captura=None):
        return motor.calcular(self.ent, self.con, t_utc or ahora_utc(), self.pares_copia(),
                              self.thresholds_manuales(), usar_foto=usar_foto, captura=captura)

    # --- foto del cálculo (spec §16.10) ---
    def comprobar_foto(self, t_utc=None):
        """Diferencias entre el cálculo con la foto y el cálculo completo, en el mismo instante ([] = idénticos)."""
        t_utc = t_utc or ahora_utc()
        return motor.diferencias(motor.canon(self.calcular(t_utc, usar_foto=False)), motor.canon(self.calcular(t_utc)))

    def verificar_foto(self, t_utc=None):
        """Al arrancar el demonio (spec §16.10): si hay foto, la compara con el cálculo completo; si no coincide (reglas.yaml,
        cuentas.yaml, un reset o el emparejamiento de copias cambiados desde que se hizo), la rehace, y la rehecha se
        comprueba sola. Devuelve las diferencias encontradas ([] si coincidía o si no había foto)."""
        if not almacen.get_estado(self.con, 'foto_version', ''):
            return []
        difs = self.comprobar_foto(t_utc)
        if difs:
            self.log(f"[FOTO] No coincide con el cálculo completo ({len(difs)} diferencias; la primera: {difs[0]}). Se rehace.")
            self.hacer_foto(t_utc)
        return difs

    def hacer_foto(self, t_utc=None):
        """Guarda la foto de todo lo archivado (operaciones abiertas hasta el corte del último ciclo, spec §15) y la comprueba:
        el cálculo con foto tiene que dar EXACTAMENTE lo mismo que el completo; si no, la foto se borra y el demonio sigue
        calculándolo todo. Devuelve (operaciones en la foto, decisiones evaluadas, diferencias)."""
        t_utc = t_utc or ahora_utc()
        corte = almacen.corte_ciclo(self.con)
        if not corte:
            almacen.guardar_foto(self.con, {}, {}, '')
            return 0, 0, []
        captura = {}
        completo = self.calcular(t_utc, usar_foto=False, captura=captura)
        # el corte es la apertura de la última operación archivada; todas están cerradas (se archiva con las cuentas planas)
        viejas = {f.clave: f for f in completo.filas if f.t_apertura_utc <= corte}
        filas = {k: captura[k] for k in viejas if k in captura}
        evaluar = {k: cortafuegos._a_foto(e) for k, e in completo.cortafuegos.items()
                   if k in viejas and not viejas[k].es_seguidora}
        almacen.guardar_foto(self.con, filas, evaluar, f"{corte}|{len(filas)}|{len(evaluar)}")
        difs = motor.diferencias(motor.canon(completo), motor.canon(self.calcular(t_utc)))
        if difs:
            almacen.guardar_foto(self.con, {}, {}, '')
            self.log(f"[FOTO ERROR] La foto no coincide con el cálculo completo ({len(difs)} diferencias; la primera: {difs[0]}). "
                     f"Se descarta: el demonio sigue calculándolo todo.")
        else:
            self.log(f"[FOTO] Foto del cálculo hasta {corte} UTC: {len(filas)} operaciones y {len(evaluar)} decisiones. "
                     f"Comprobada: el cálculo con foto es idéntico al completo.")
        return len(filas), len(evaluar), difs

    # --- detección de 'operación cerrada con cuenta plana' ---
    def _estado_cuentas(self, t_utc):
        """{cuenta: (plana en todos sus símbolos, nº de fills)} con fills de MyTrade + log."""
        out = {}
        rel = reloj.cargar(self.con, self.ent.reglas)
        for c in motor.cuentas_en_juego(self.ent, self.con):
            pos, n = motor.posiciones_cuenta(self.ent, self.con, c, t_utc, rel)
            out[c] = (all(abs(v) < 1e-9 for v in pos.values()), n)
        return out

    def pendiente(self, t_utc=None):
        """True si alguna cuenta en juego está plana (símbolo a símbolo) con fills no sincronizados,
        o cambió la sesión."""
        t_utc = t_utc or ahora_utc()
        hechos = json.loads(almacen.get_estado(self.con, 'fills_sincronizados', '{}'))
        for c, (plana, n) in self._estado_cuentas(t_utc).items():
            if plana and n != hechos.get(c, 0):
                return True
        return almacen.get_estado(self.con, 'sesion_sincronizada') != self.sesion(t_utc).isoformat()

    def _marcar_sincronizado(self, t_utc, estado=None):
        """`estado`: la foto de _estado_cuentas tomada con el cálculo que se ha escrito (sync); sin ella, la de ahora."""
        prev = json.loads(almacen.get_estado(self.con, 'fills_sincronizados', '{}'))
        estado = self._estado_cuentas(t_utc) if estado is None else estado
        hechos = {c: (n if plana else prev.get(c, 0)) for c, (plana, n) in estado.items()}
        almacen.set_estado(self.con, 'fills_sincronizados', json.dumps(hechos))
        almacen.set_estado(self.con, 'sesion_sincronizada', self.sesion(t_utc).isoformat())
        almacen.set_estado(self.con, 'ultima_sync', almacen.ahora_madrid())
        self.con.commit()

    # --- sync completo ---
    def sync(self, t_utc=None):
        """Ingesta y cálculo primero; la lectura de las columnas manuales va justo antes de escribir
        para que la ventana en la que una edición del operador podría perderse sea mínima."""
        t_utc = t_utc or ahora_utc()
        self.ingerir(forzar_cdb=True)
        playbook = fuentes.leer_playbook(self.ent.db_dir)
        thr_antes = self.thresholds_manuales()
        res = self.calcular(t_utc)
        # Foto de las cuentas (plana, nº de fills) tomada con el cálculo, sin red de por medio: es lo que se va a escribir.
        # Mientras se espera a Sheets el ciclo del cortafuegos sigue ingiriendo (11/11/2026); un fill que llegue entonces no
        # está en este layout y no puede darse por sincronizado.
        estado = self._estado_cuentas(t_utc)
        if self.hoja.asegurar_pestana():
            self.log(f"[SHEETS] Pestaña '{self.ent.cfg['pestana']}' creada.")
        valores = self.hoja.leer()
        manual, thr_nombres, layout_ok = vista.parsear_hoja(valores)
        preop = vista.leer_preoperativa(valores, self.ent.reglas)
        if preop is not None:                           # sin la fila aún (pestaña antigua): no hay nada que leer
            self.observar_preoperativa(preop, t_utc)
        sesion = self.sesion(t_utc).isoformat()
        nueva_sesion = almacen.get_estado(self.con, 'compuerta_sesion') != sesion
        if nueva_sesion:        # §10.6: se vacía al empezar la sesión CME; lo que quede en el sheet es de la anterior
            self._guardar_compuerta([''] * len(self.ent.reglas['preoperativa']['compuerta']['filas']))
            self._guardar_mercado(self.ent.reglas['preoperativa']['mercado']['defecto'])     # §12, igual
        else:
            mercado = vista.leer_mercado(valores, self.ent.reglas)
            if mercado is not None:                     # sin la fila aún (pestaña antigua): no hay nada que leer
                self.observar_mercado(mercado, t_utc)
            comp = vista.leer_compuerta(valores, self.ent.reglas)
            if comp is not None:                        # sin las filas aún (pestaña antigua): no hay nada que leer
                self.observar_compuerta(comp, t_utc)
        vista.guardar_manual(self.con, manual)
        flotantes = vista.detectar_flotantes(self.con, manual)
        if layout_ok:
            self._guardar_thresholds(thr_nombres)
        if flotantes or self.thresholds_manuales() != thr_antes:   # edición del operador: recalcular todo
            res = self.calcular(t_utc)
            estado = self._estado_cuentas(t_utc)
        self._registrar_reloj(res.reloj)
        cortafuegos.registrar(self.con, res.filas, res.cortafuegos, self.ent.reglas)
        self._purgar(res, t_utc)
        queda = self._asignar_estrategia(res, t_utc)
        tardias = {r['op_id'] for r in self.con.execute('SELECT op_id FROM estrategia_asignada WHERE tardia=1')}
        manual = vista.cargar_manual(self.con)
        # Calificación (spec §4.2, 01/10/2026) con la estrategia final: la del sheet ya leída y la de la preoperativa ya
        # asignada; por eso va después de _asignar_estrategia y antes de guardar las operaciones.
        motor.calificar(res.filas, res.cortafuegos, manual, tardias, self.ent.reglas)
        # Etiquetas de análisis (spec §10.5, §10.6): ESPEJO (con la corrección manual de Observaciones) y COMPUERTA SIN SÍ
        motor.marcar_analisis(res.filas, almacen.lecturas_compuerta(self.con), res.reloj, manual, self.ent.reglas,
                              almacen.lecturas_mercado(self.con))
        mv5.asignar_turnos(res.filas, self._turnos_exchange(res.reloj), self.ent.reglas)     # spec §16.2: solo en SQLite
        motor.guardar_operaciones(self.con, res.filas, self.ent)
        # Recuento semanal de los objetivos de proceso (spec §11), con las etiquetas de análisis ya puestas
        rec = motor.recuento_semanal(res.filas, manual, tardias, self.ent.reglas)
        actual = motor.inicio_semana(self.sesion(t_utc))
        lay = vista.construir(res, self.ent, manual, playbook, queda, tardias, self.valores_compuerta(),
                              vista.fila_semana(rec, actual, self.ent.reglas), self.valor_mercado(),
                              desde_utc=almacen.corte_ciclo(self.con),     # spec §15: solo el ciclo en curso
                              hasta_utc=self._hasta_vista, recordatorio=self._recordatorio(res))
        lay['mostrar_columnas'] = vista.columnas_a_mostrar(valores)    # cambio de layout: la que ocultó el demonio
        a1 = vista.celda_a1(lay['preop_fila'], lay['preop_col'])
        with (self.r17.exclusivo() if self.r17 else contextlib.nullcontext()):
            # spec §13: la reescritura vuelve a poner el texto de R17 (no lo borra) y R17 se entera de dónde queda la fila
            vista.poner_r17(lay, self._texto_r17(valores))
            self.hoja.escribir(lay)
            if self.r17:
                self.r17.fijar_celda(a1)
        vista.guardar_escrito(self.con, lay)
        almacen.set_estado(self.con, 'preop_celda', a1)
        self.observar_preoperativa(queda, t_utc)        # lo que acaba de escribir también es una lectura
        almacen.set_estado(self.con, 'compuerta_sesion', sesion)    # con la escritura hecha, las lecturas ya son de la sesión nueva
        self.observar_compuerta(self.valores_compuerta(), t_utc)
        self.observar_mercado(self.valor_mercado(), t_utc)
        self._escribir_objetivos(rec, actual)
        self._marcar_sincronizado(t_utc, estado)
        for a in res.avisos:            # cada aviso, una vez por ejecución del demonio (antes, en cada sync)
            if a not in self._avisos_dichos:
                self._avisos_dichos.add(a)
                self.log(a)
        decisiones = lay['ops_fin'] - lay['ops_ini']
        self.log(f"[SYNC] {len(lay['valores'])} filas escritas: {lay['n_bal']} cuentas BAL, "
                 f"{decisiones} decisiones ({len(res.filas)} operaciones por cuenta).")
        return res, lay

    def _turnos_exchange(self, rel):
        """Turnos de SQLite [(inicio, fin|None)] pasados del reloj del PC (con el que se guardan) a la hora del exchange, que es la
        de las aperturas de las decisiones (V5.1, 17/11/2026: antes se comparaban en relojes distintos, con 0,3–0,8 s de deriva)."""
        return [(rel.pc_a_utc(a), rel.pc_a_utc(b) if b else None) for a, b in almacen.turnos(self.con)]

    def _recordatorio(self, res):
        """Texto del recordatorio de inicio de turno (spec §16.3) mientras el turno en curso no tenga ninguna decisión
        anotada; None después, y siempre fuera del demonio (el CLI no abre turno)."""
        if not self.turno_inicio:
            return None
        inicio = res.reloj.pc_a_utc(self.turno_inicio)         # el arranque es del reloj del PC; las aperturas, del exchange
        if any(not f.es_seguidora and f.t_apertura_utc >= inicio for f in res.filas):
            return None
        return mv5.recordatorio(self.ent.reglas)

    # --- histórico unificado (spec §16.9) ---
    FORMATOS_UNIFICADO = {'Fecha y hora apertura': ('DATE_TIME', 'dd/mm/yyyy hh:mm:ss')}

    def _insertar_unificado(self, filas):
        """Añade al histórico unificado las filas cuyo ID aún no está (la columna A manda): nunca reescribe lo ya puesto."""
        U = self.ent.reglas['v5']['unificado']
        ya = set(self.hoja.leer_columna_a(U['pestana']))
        nuevas = [r for r in filas if str(r[0]).strip() not in ya]
        cols = {c: i for i, c in enumerate(vista.UNIFICADO_COLS)}
        formatos = {cols[c]: f for c, f in self.FORMATOS_UNIFICADO.items()}
        return self.hoja.insertar_arriba(U['pestana'], vista.UNIFICADO_COLS, nuevas, formatos)

    def _manual_y_tardias(self):
        return (vista.cargar_manual(self.con),
                {r['op_id'] for r in self.con.execute('SELECT op_id FROM estrategia_asignada WHERE tardia=1')})

    def cargar_unificado(self, t_utc=None):
        """Carga inicial (o reparación) del histórico unificado: las decisiones de antes de la V4 (pestaña de la hoja antigua,
        config `pestana_decisiones_antiguas`) y todos los ciclos ya archivados, con las columnas de la V5. Solo añade lo que
        falte por ID. Devuelve (filas añadidas, decisiones antiguas leídas, decisiones de ciclos archivados)."""
        res, _ = self.sync(t_utc or ahora_utc())
        manual, tardias = self._manual_y_tardias()
        corte = almacen.corte_ciclo(self.con)
        archivadas = vista.filas_unificado(res.filas, self.ent, manual, tardias, hasta_utc=corte) if corte else []
        antiguas = []
        if self.ent.cfg.get('sheet_id_antiguo') and self.ent.cfg.get('pestana_decisiones_antiguas'):
            antiguas = vista.filas_unificado_antiguas(
                self.hoja.leer_otra_hoja(self.ent.cfg['sheet_id_antiguo'], self.ent.cfg['pestana_decisiones_antiguas']),
                self.ent.reglas)
        n = self._insertar_unificado(archivadas + antiguas)         # lo más nuevo arriba
        self.log(f"[UNIFICADO] {n} filas añadidas ({len(antiguas)} decisiones antiguas leídas, {len(archivadas)} de ciclos archivados).")
        return n, len(antiguas), len(archivadas)

    def _purgar(self, res, t_utc):
        """Purga de las dos tablas del cortafuegos que solo crecían (11/11/2026): infracciones que el cálculo ya no da y
        marcas de aviso sin evento y con más de config `purga_avisos_dias` días. Un fallo se registra y no tumba el sync."""
        try:
            n = cortafuegos.purgar_saltados(self.con, res.filas, res.cortafuegos, self.ent.reglas)
            m = cortafuegos.purgar_avisos(self.con, res, t_utc, self.ent.reglas,
                                          self.ent.cfg.get('purga_avisos_dias', 7))
            if n or m:
                self.log(f"[PURGA] cortafuegos_saltados: {n} filas que el cálculo ya no da; avisos_emitidos: {m} marcas sin evento.")
        except Exception as e:
            self.log(f"[PURGA ERROR] {type(e).__name__}: {e}")

    # --- ciclos (spec §15) ---
    def archivar_ciclo(self, t_utc=None, hasta=None):
        """Archiva el ciclo en curso (spec §15): sincroniza, copia la pestaña entera a otra con las fechas del ciclo
        (`ciclos.pestana`), anota el corte en SQLite y vuelve a sincronizar: la pestaña V4 queda con el BAL y sin operaciones,
        y lo que se abra después forma el ciclo nuevo. Exige todas las cuentas planas. No borra nada de SQLite: el cálculo
        (margen, rachas, balance, cortafuegos, objetivos semanales) sigue saliendo de toda la historia.
        `hasta` (date, opcional): último día de Madrid que entra en el archivo; lo abierto después se queda en el ciclo nuevo.
        Devuelve (nombre de la pestaña archivada, nº de decisiones). ValueError si no se puede archivar."""
        t_utc = t_utc or ahora_utc()
        hasta_utc = None
        if hasta is not None:
            hasta_utc = fuentes.fmt_ts(fuentes.madrid_a_utc(datetime(hasta.year, hasta.month, hasta.day) + timedelta(days=1)))
        self._hasta_vista = hasta_utc           # la pestaña que se copia solo lleva lo que se archiva
        try:
            res, _ = self.sync(t_utc)
        finally:
            self._hasta_vista = None
        if res.filas_abiertas:
            almacen.set_estado(self.con, 'forzar_sync', '1')
            self.con.commit()
            raise ValueError('hay una posición abierta; se archiva con todas las cuentas planas')
        corte = almacen.corte_ciclo(self.con)
        ciclo = [f for f in res.filas if (not corte or f.t_apertura_utc > corte)
                 and (not hasta_utc or f.t_apertura_utc < hasta_utc)]
        decisiones = sorted(f.t_apertura_utc for f in ciclo if not f.es_seguidora)
        if not decisiones:
            almacen.set_estado(self.con, 'forzar_sync', '1')      # por si la pestaña quedó con el tope puesto
            self.con.commit()
            raise ValueError('el ciclo en curso no tiene operaciones' + (' hasta ese día' if hasta else ''))
        C = self.ent.reglas['ciclos']

        def dia(t):
            return fuentes.utc_a_madrid(t).strftime(C['formato_fecha'])

        nombre = self.hoja.duplicar(C['pestana'].format(desde=dia(decisiones[0]), hasta=dia(decisiones[-1])))
        almacen.cerrar_ciclo(self.con, min(f.t_apertura_utc for f in ciclo), max(f.t_apertura_utc for f in ciclo),
                             nombre, len(decisiones))
        try:                    # spec §16.9: el ciclo pasa también al histórico unificado (si falla: cli_v4.py unificado)
            manual, tardias = self._manual_y_tardias()
            n = self._insertar_unificado(vista.filas_unificado(ciclo, self.ent, manual, tardias))
            self.log(f"[UNIFICADO] {n} decisiones del ciclo añadidas al histórico unificado.")
        except Exception as e:
            self.log(f"[UNIFICADO ERROR] El ciclo no se ha añadido al histórico unificado ({type(e).__name__}: {e}). "
                     f"Repetir con: python cli_v4.py unificado")
        self.log(f"[ARCHIVO] Ciclo archivado en la pestaña '{nombre}': {len(decisiones)} decisiones "
                 f"({dia(decisiones[0])} a {dia(decisiones[-1])}). La pestaña '{self.ent.cfg['pestana']}' empieza ciclo nuevo.")
        try:                    # spec §16.10: la foto del cálculo se extiende al corte nuevo (y se comprueba)
            self.hacer_foto(t_utc)
        except Exception as e:
            almacen.guardar_foto(self.con, {}, {}, '')
            self.log(f"[FOTO ERROR] {type(e).__name__}: {e}. Sin foto: el demonio sigue calculándolo todo.")
        try:
            self.sync(t_utc)
        except Exception:
            almacen.set_estado(self.con, 'forzar_sync', '1')      # el corte ya está anotado: el siguiente sync limpia la pestaña
            self.con.commit()
            raise
        return nombre, len(decisiones)

    def _texto_r17(self, valores):
        """Texto de R17 que se vuelve a escribir en la celda (spec §13). `valores`: la pestaña tal como estaba al empezar el
        sync; de ella sale el texto de R17 que había (None si no era de R17). Con el hilo de R17 manda lo que éste ha escrito; si
        aún no ha escrito nada (primer sync tras arrancar) se le siembra lo leído, y sin hilo (`cli_v4.py sync` con el demonio
        parado) se conserva lo leído: el sync no borra la celda. R17 es informativo: un fallo aquí se registra y no tumba el sync."""
        try:
            leido = vista.leer_r17(valores, self.ent.reglas)
            if self.r17:
                self.r17.sembrar(leido)
                return self.r17.texto_escrito()
            return leido
        except Exception as e:
            self.log(f"[R17 ERROR] {type(e).__name__}: {e}")
            return None

    # --- recuento semanal (spec §11.3) ---
    def _escribir_objetivos(self, rec, actual):
        """Pestaña `Objetivos semanales`: lee la columna 5 (manual) -> SQLite y regenera la pestaña entera desde el recuento.
        Un fallo (red, cuota) se registra y no tumba el sync de la pestaña principal: el siguiente sync la regenera."""
        R = self.ent.reglas
        try:
            if self.hoja.asegurar_pestana_objetivos():
                self.log(f"[OBJETIVOS] Pestaña '{R['objetivos']['pestana']}' creada.")
            lay = vista.layout_objetivos(rec, actual, {}, R)       # V5 (spec §16.8): sin columna manual que leer
            self.hoja.escribir_objetivos(lay)
        except Exception as e:
            self.log(f"[OBJETIVOS ERROR] {type(e).__name__}: {e}")

    # --- preoperativa (spec §7.2) ---
    def observar_preoperativa(self, valor, t_utc):
        """Anota lo leído en la celda 'Estrategia a usar'. Momento de elección = última lectura en la que la
        celda tenía otro valor (cota inferior, a favor del operador). Sin lectura previa: esta lectura, marcada
        como desconocida (no se juzga la tardía)."""
        defecto = self.ent.reglas['estrategia_por_defecto']
        valor = str(valor or '').strip()
        if not valor or valor.lower() == defecto.lower():
            valor = defecto
        previo = almacen.get_estado(self.con, 'preop_estrategia', defecto)
        if valor != previo:
            anterior = almacen.get_estado(self.con, 'preop_t_lectura', '')
            # sin lectura previa no hay cota: se toma esta lectura y no se juzga la tardía (no se sabe cuándo eligió)
            desconocido = valor != defecto and not anterior
            elegida = (anterior or t_utc) if valor != defecto else ''
            almacen.set_estado(self.con, 'preop_estrategia', valor)
            almacen.set_estado(self.con, 'preop_t_elegida', elegida)
            almacen.set_estado(self.con, 'preop_eleccion_desconocida', '1' if desconocido else '')
            if desconocido:
                self.log(f"[PREOPERATIVA] '{valor}' sin lectura previa: cuenta desde {t_utc} y sin control de tardía")
        almacen.set_estado(self.con, 'preop_t_lectura', t_utc)
        self.con.commit()
        return valor

    def observar_compuerta(self, valores, t_utc):
        """Anota lo leído en las filas de la compuerta (spec §10.6). Solo se guardan los cambios, con la hora de la lectura
        en que se vieron: la lectura vigente en una hora es la última anterior o igual a ella. El estado también es lo que se
        vuelve a escribir en el sheet."""
        valores = [str(v).strip().upper() for v in valores]
        ultimo = self.con.execute('SELECT valores FROM compuerta_lecturas ORDER BY t_pc_utc DESC LIMIT 1').fetchone()
        if ultimo is None or json.loads(ultimo['valores']) != valores:
            self.con.execute('INSERT OR REPLACE INTO compuerta_lecturas VALUES (?, ?)',
                             (t_utc, json.dumps(valores, ensure_ascii=False)))
        self._guardar_compuerta(valores)

    def _guardar_compuerta(self, valores):
        almacen.set_estado(self.con, 'compuerta_valores', json.dumps(valores, ensure_ascii=False))
        self.con.commit()

    def valores_compuerta(self):
        """Valores de la compuerta que se escriben en el sheet: los últimos leídos (vacíos si no hay ninguno)."""
        vacio = json.dumps([''] * len(self.ent.reglas['preoperativa']['compuerta']['filas']))
        return json.loads(almacen.get_estado(self.con, 'compuerta_valores', vacio))

    def observar_mercado(self, valor, t_utc):
        """Anota lo leído en la fila `Estado del mercado` (spec §12), como la compuerta: solo los cambios, con la hora de la
        lectura; el estado también es lo que se vuelve a escribir en el sheet."""
        ultimo = self.con.execute('SELECT valor FROM mercado_lecturas ORDER BY t_pc_utc DESC LIMIT 1').fetchone()
        if ultimo is None or ultimo['valor'] != valor:
            self.con.execute('INSERT OR REPLACE INTO mercado_lecturas VALUES (?, ?)', (t_utc, valor))
        self._guardar_mercado(valor)

    def _guardar_mercado(self, valor):
        almacen.set_estado(self.con, 'mercado_valor', valor)
        self.con.commit()

    def valor_mercado(self):
        """Valor de `Estado del mercado` que se escribe en el sheet: el último leído (el defecto si no hay)."""
        return almacen.get_estado(self.con, 'mercado_valor', self.ent.reglas['preoperativa']['mercado']['defecto'])

    def vigilar_preoperativa(self, t_utc=None):
        """Lectura periódica de la celda (demonio, cada preoperativa.lectura_s). La hora se toma ANTES de la
        petición (cota inferior, a favor del operador). La estrategia y las filas de la compuerta (spec §10.6) se piden en
        UNA petición, con el cliente de timeout corto. None si aún no se escribió o si en esa fila ya no está la etiqueta
        (filas insertadas o borradas a mano: el siguiente sync la recoloca). La compuerta solo se anota si sus textos son los
        de reglas.yaml y la sesión CME ya está escrita (hasta entonces el sheet tiene los valores de la sesión anterior)."""
        a1 = almacen.get_estado(self.con, 'preop_celda')
        if not a1:
            return None
        t_utc = t_utc or ahora_utc()
        fila, col = vista.celda_de_a1(a1)
        # una sola petición: la fila de encima (estado del mercado, §12), la estrategia y las de la compuerta
        filas = self.hoja.leer_filas(fila - 1, col, 2 + len(self.ent.reglas['preoperativa']['compuerta']['filas']))
        mercado, filas = (filas[0] if filas else []), filas[1:]
        valores = filas[0] if filas else []
        if not valores or str(valores[0]).strip() != self.ent.reglas['preoperativa']['estrategia']['etiqueta']:
            return None
        def escrita():          # el sheet ya es de esta sesión CME (hasta entonces tiene los valores de la anterior)
            return almacen.get_estado(self.con, 'compuerta_sesion') == self.sesion(t_utc).isoformat()

        comp = vista.valores_compuerta(filas, self.ent.reglas)
        if comp is not None and comp != self.valores_compuerta() and escrita():
            self.observar_compuerta(comp, t_utc)
        merc = vista.valor_mercado(mercado, self.ent.reglas)
        if merc is not None and merc != self.valor_mercado() and escrita():
            self.observar_mercado(merc, t_utc)
        return self.observar_preoperativa(valores[col] if len(valores) > col else '', t_utc)

    def _asignar_estrategia(self, res, t_utc):
        """Decisiones que se escriben por primera vez y cerraron tras el momento de elección reciben la estrategia
        de la celda. Devuelve el valor que debe quedar en la celda: el defecto si se consumió. El estado se
        resetea al observar lo escrito (sync), así que si la escritura falla la elección sigue armada y el
        siguiente sync repite la misma asignación (idempotente).
        Sin lectura previa, el momento de elección es la primera lectura con el valor y no se juzga la tardía
        (observar_preoperativa); así una base nueva no reparte la estrategia a lo ya cerrado."""
        defecto = self.ent.reglas['estrategia_por_defecto']
        valor = almacen.get_estado(self.con, 'preop_estrategia', defecto)
        elegida = almacen.get_estado(self.con, 'preop_t_elegida', '')
        if valor == defecto or not elegida:
            return valor
        # la lectura se guarda con el reloj del PC; se compara en hora del exchange (reloj ATAS de esa sesión,
        # con lo que se sepa ahora: la sesión ya tiene los fills de la decisión que se anota)
        elegida_exch = reloj.cargar(self.con, self.ent.reglas).pc_a_utc(elegida)
        escritas = {r['op_id'] for r in self.con.execute('SELECT op_id FROM escrito_ops')}
        nuevas = [f for f in res.filas
                  if not f.es_seguidora and f.op_id and f.op_id not in escritas and f.t_cierre_utc
                  and f.t_cierre_utc >= elegida_exch]
        if not nuevas:
            return valor
        tol = timedelta(seconds=float(self.ent.reglas['preoperativa']['tolerancia_tardia_s']))
        desconocido = almacen.get_estado(self.con, 'preop_eleccion_desconocida', '') == '1'
        tardias = [] if desconocido else [
            f.op_id for f in nuevas if fuentes.parse_ts(elegida_exch) > fuentes.parse_ts(f.t_apertura_utc) + tol]
        for f in nuevas:
            self.con.execute('INSERT INTO manual_ops (op_id, estrategia) VALUES (?, ?) '
                             'ON CONFLICT(op_id) DO UPDATE SET estrategia=excluded.estrategia', (f.op_id, valor))
            self.con.execute('INSERT OR REPLACE INTO estrategia_asignada (op_id, estrategia, t_elegida_utc, '
                             't_asignada_utc, tardia, t_elegida_exch_utc) VALUES (?, ?, ?, ?, ?, ?)',
                             (f.op_id, valor, elegida, t_utc, int(f.op_id in tardias), elegida_exch))
        self.con.commit()
        self.log(f"[PREOPERATIVA] '{valor}' asignada a {', '.join(f.op_id for f in nuevas)}"
                 + (f" (elegida con la operación abierta: {', '.join(tardias)})" if tardias else ''))
        return defecto

    def _registrar_reloj(self, rel):
        """Deja en el log del demonio el desfase del último tramo del reloj cada vez que cambia."""
        d = rel.detalle[-1]
        origen = 'salto del reloj del PC' if d.get('tipo') == 'salto' else 'arranque de ATAS'
        txt = (f"Tramo desde {d['inicio'] or '(sin arranque en el log)'} ({origen}): log − exchange = "
               f"{d['desfase_s']:+.3f} s ({d['fuente']}, {d['muestras']} muestras)")
        # Solo si cambia el tramo o su desfase se mueve al menos reloj_log_min_s (11/11/2026): la mediana se mueve unos ms con
        # cada fill y el texto lleva el nº de muestras, así que antes salía una línea por sync.
        previo = json.loads(almacen.get_estado(self.con, 'reloj_log', 'null') or 'null')
        minimo = float(self.ent.cfg.get('reloj_log_min_s', 0.1))
        if (previo is None or previo['inicio'] != d['inicio'] or previo['tipo'] != d.get('tipo')
                or abs(previo['desfase_s'] - d['desfase_s']) >= minimo):
            almacen.set_estado(self.con, 'reloj_log', json.dumps(
                {'inicio': d['inicio'], 'tipo': d.get('tipo'), 'desfase_s': d['desfase_s']}))
            almacen.set_estado(self.con, 'reloj_ultimo', txt)
            self.con.commit()
            self.log(f"[RELOJ] {txt}")

    def _guardar_thresholds(self, thr_nombres):
        """Filas BAL -> threshold_manual, emparejadas por cuenta interna (el nombre visible cambia con
        cada retiro). Solo se borra el valor de una cuenta cuya fila existe con la celda vacía."""
        for nombre, valor in thr_nombres.items():
            cuenta = self.ent.cuenta_por_nombre_visible(nombre)
            if cuenta is None:
                continue
            if valor is None:
                self.con.execute('DELETE FROM threshold_manual WHERE cuenta=?', (cuenta,))
            else:
                self.con.execute('INSERT OR REPLACE INTO threshold_manual VALUES (?, ?, ?)',
                                 (cuenta, valor, almacen.ahora_madrid()))
        self.con.commit()


def _copiar_cuentas(ent):
    """Copia diaria de cuentas.yaml (V5.1): es la única fuente de las fichas, los retiros, los resets y las simulaciones, y no se
    reconstruye desde SQLite. Solo se rotan las diarias (`cuentas_AAAAMMDD.yaml`); las manuales (`_antes_`) no se tocan."""
    if not os.path.exists(ent.cuentas_path):
        return None
    destino = os.path.join(ent.backups_dir, f"cuentas_{datetime.now():%Y%m%d}.yaml")
    if not os.path.exists(destino):
        shutil.copyfile(ent.cuentas_path, destino)
    diarias = sorted(n for n in os.listdir(ent.backups_dir) if re.fullmatch(r'cuentas_\d{8}\.yaml', n))
    for n in diarias[:-int(ent.cfg['backups_retener'])]:
        os.remove(os.path.join(ent.backups_dir, n))
    return destino


def backup_diario(ent, con, log=print):
    """Copia local diaria de la SQLite en backups\\ (API de backup de SQLite, consistente con WAL) y de cuentas.yaml. La copia
    se hace a un .tmp y se renombra al acabar (V5.1): una copia que falla no deja un fichero que se dé por bueno todo el día."""
    os.makedirs(ent.backups_dir, exist_ok=True)
    destino = os.path.join(ent.backups_dir, f"atas_v4_{datetime.now():%Y%m%d}.sqlite")
    if os.path.exists(destino) and os.path.getsize(destino) > 0:
        return None
    _copiar_cuentas(ent)
    tmp = destino + '.tmp'
    dst = sqlite3.connect(tmp)
    try:
        con.backup(dst)
    except Exception:
        dst.close()
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    dst.close()
    os.replace(tmp, destino)
    copias = sorted(n for n in os.listdir(ent.backups_dir) if n.startswith('atas_v4_') and n.endswith('.sqlite'))
    for n in copias[:-int(ent.cfg['backups_retener'])]:
        os.remove(os.path.join(ent.backups_dir, n))
    # los -shm / -wal de una copia que ya no está (rotada ahora o antes) no sirven de nada: se van con ella (11/11/2026)
    for n in os.listdir(ent.backups_dir):
        for suf in ('-shm', '-wal'):
            if (n.startswith('atas_v4_') and n.endswith('.sqlite' + suf)
                    and not os.path.exists(os.path.join(ent.backups_dir, n[:-len(suf)]))):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(ent.backups_dir, n))
    log(f"[BACKUP] {destino}")
    return destino
