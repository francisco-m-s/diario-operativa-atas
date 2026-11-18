"""Cliente Google Sheets de la pestaña V4. Solo toca la pestaña configurada."""

import google_auth_httplib2
import httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import build

from . import vista
from .vista import BC, N_COLS, OC

FILAS_MIN = 500


def _color(hexa):
    h = hexa.lstrip('#')
    return {'red': int(h[0:2], 16) / 255, 'green': int(h[2:4], 16) / 255, 'blue': int(h[4:6], 16) / 255}


def _col_letra(i):
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _celda(v):
    """Valor del layout -> CellData de `updateCells`, con lo mismo que guardaba la escritura RAW: número, booleano o texto
    tal cual (un texto nunca se interpreta: ni fórmula ni fecha); vacío = celda sin valor."""
    if v is None or (isinstance(v, str) and v == ''):
        return {}
    if isinstance(v, bool):
        return {'userEnteredValue': {'boolValue': v}}
    if isinstance(v, (int, float)):
        return {'userEnteredValue': {'numberValue': v}}
    return {'userEnteredValue': {'stringValue': str(v)}}


def _filas_valores(valores, n_cols):
    return [{'values': [_celda(v) for v in fila[:n_cols]]} for fila in valores]


def _directo(fn, plazo_s):
    return fn()


class HojaV4:
    corredor = staticmethod(_directo)      # ver __init__
    plazo_s = 90.0
    _timeout_rapido = 3.0

    def __init__(self, ent):
        self._cred = ent.cfg.get('credencial')
        if not self._cred:
            raise RuntimeError('Falta la credencial de Google: pon `credencial: <ruta al .json>` en config.local.yaml o define la '
                               'variable de entorno ATAS_SHEETS_CREDENCIAL. El .json va FUERA del repositorio.')
        self._timeout_rapido = float(ent.cfg.get('preoperativa_timeout_s', 3))
        self.plazo_s = float(ent.cfg.get('sheets_plazo_s', 90))
        # Quien ejecuta cada petición: `corredor(fn, plazo_s)`. Por defecto, en línea (CLI). El demonio pone el suyo
        # (`Ciclo.corredor`, 11/11/2026): la petición va en un hilo auxiliar con plazo de reloj de pared y, mientras se
        # espera, el ciclo del cortafuegos sigue corriendo. Así ni el sync ni la lectura de la preoperativa lo paran.
        self.corredor = _directo
        self._crear_clientes()
        self.sid = ent.cfg['sheet_id']
        self.tab = ent.cfg['pestana']
        self.q = f"'{self.tab}'"
        self.R = ent.reglas
        self.tab_id = None
        self.filas = 0
        self.columnas = 0
        self.n_cf = 0
        self.tab_obj = ent.reglas['objetivos']['pestana']      # pestaña del recuento semanal (spec §11.3)
        self.q_obj = f"'{self.tab_obj}'"
        self.obj_id = None
        self.obj_filas = 0
        self.obj_cols = 0
        self._cred_path = self._cred                           # cliente propio del hilo de R17 (spec §13), creado al usarlo
        self._timeout_r17 = float(ent.cfg.get('preoperativa_timeout_s', 3))
        self._svc_r17 = None

    def _crear_clientes(self):
        creds = service_account.Credentials.from_service_account_file(
            self._cred, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        self.svc = build('sheets', 'v4', credentials=creds, cache_discovery=False)
        # cliente aparte con timeout de socket corto para la lectura periódica de la preoperativa
        self.svc_rapido = build('sheets', 'v4', cache_discovery=False, http=google_auth_httplib2.AuthorizedHttp(
            creds, http=httplib2.Http(timeout=self._timeout_rapido)))

    def _ej(self, peticion, rapido=False):
        """Ejecuta una petición por el corredor. Si vence el plazo, la petición queda abandonada en su hilo con el cliente
        viejo: se crean clientes nuevos para que la siguiente no comparta conexión con ella (httplib2 no es seguro entre
        hilos)."""
        try:
            return self.corredor(peticion.execute, self._timeout_rapido if rapido else self.plazo_s)
        except TimeoutError:
            self._crear_clientes()
            raise

    def duplicar(self, nombre):
        """Copia la pestaña entera (valores, formatos, validaciones) a otra nueva, justo a su derecha (spec §15). Si el
        nombre ya existe, le añade ` (2)`, ` (3)`… Devuelve el nombre con el que quedó."""
        m = self._ej(self.svc.spreadsheets().get(spreadsheetId=self.sid, fields='sheets(properties(sheetId,title,index))'))
        props = [s['properties'] for s in m['sheets']]
        origen = next(p for p in props if p['title'] == self.tab)
        titulos = {p['title'] for p in props}
        final, k = nombre, 2
        while final in titulos:
            final, k = f'{nombre} ({k})', k + 1
        self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': [
            {'duplicateSheet': {'sourceSheetId': origen['sheetId'], 'insertSheetIndex': origen['index'] + 1,
                                'newSheetName': final}}]}))
        return final

    # --- pestañas auxiliares que el demonio solo crea y amplía (histórico unificado, spec §16.9) ---
    def _props(self):
        m = self._ej(self.svc.spreadsheets().get(spreadsheetId=self.sid,
                                                 fields='sheets(properties(sheetId,title,index,gridProperties))'))
        return {s['properties']['title']: s['properties'] for s in m['sheets']}

    def leer_columna_a(self, pestana):
        """Columna A de una pestaña ([] si no existe): los ID ya puestos en el histórico unificado."""
        if pestana not in self._props():
            return []
        v = self._ej(self.svc.spreadsheets().values().get(spreadsheetId=self.sid, range=f"'{pestana}'!A:A",
                                                          valueRenderOption='UNFORMATTED_VALUE')).get('values', [])
        return [str(r[0]).strip() if r else '' for r in v]

    def insertar_arriba(self, pestana, cabecera, filas, formatos=None):
        """Añade `filas` justo debajo del encabezado de `pestana`, empujando hacia abajo lo que hubiera (nunca lo reescribe).
        Si la pestaña no existe la crea con `cabecera` (negrita, fila fijada). Todo en UNA petición por paso. `formatos`:
        {índice de columna: (tipo, patrón)} de formato numérico para las filas nuevas."""
        props = self._props()
        n_cols = len(cabecera)
        if pestana not in props:
            r = self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': [
                {'addSheet': {'properties': {'title': pestana, 'gridProperties': {
                    'rowCount': 2, 'columnCount': n_cols, 'frozenRowCount': 1}}}}]}))
            sid = r['replies'][0]['addSheet']['properties']['sheetId']
            self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': [
                {'updateCells': {'range': {'sheetId': sid, 'startRowIndex': 0, 'endRowIndex': 1, 'startColumnIndex': 0,
                                           'endColumnIndex': n_cols},
                                 'rows': _filas_valores([cabecera], n_cols), 'fields': 'userEnteredValue'}},
                {'repeatCell': {'range': {'sheetId': sid, 'startRowIndex': 0, 'endRowIndex': 1},
                                'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}},
                                'fields': 'userEnteredFormat.textFormat.bold'}}]}))
        else:
            sid = props[pestana]['sheetId']
        if not filas:
            return 0
        n = len(filas)
        reqs = [{'insertDimension': {'range': {'sheetId': sid, 'dimension': 'ROWS', 'startIndex': 1, 'endIndex': 1 + n},
                                     'inheritFromBefore': False}},
                {'updateCells': {'range': {'sheetId': sid, 'startRowIndex': 1, 'endRowIndex': 1 + n, 'startColumnIndex': 0,
                                           'endColumnIndex': n_cols},
                                 'rows': _filas_valores(filas, n_cols), 'fields': 'userEnteredValue,userEnteredFormat'}}]
        for col, (tipo, patron) in (formatos or {}).items():
            reqs.append({'repeatCell': {'range': {'sheetId': sid, 'startRowIndex': 1, 'endRowIndex': 1 + n,
                                                  'startColumnIndex': col, 'endColumnIndex': col + 1},
                                        'cell': {'userEnteredFormat': {'numberFormat': {'type': tipo, 'pattern': patron}}},
                                        'fields': 'userEnteredFormat.numberFormat'}})
        self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': reqs}))
        return n

    def borrar_pestanas_vacias(self, titulos):
        """Borra las pestañas de `titulos` que existan y no tengan ningún valor (las `Hoja 1` / `Hoja 2` de una hoja nueva)."""
        props = self._props()
        reqs = []
        for t in titulos:
            if t in props and len(props) - len(reqs) > 1:
                v = self._ej(self.svc.spreadsheets().values().get(spreadsheetId=self.sid, range=f"'{t}'!A1:Z50")).get('values')
                if not v:
                    reqs.append({'deleteSheet': {'sheetId': props[t]['sheetId']}})
        if reqs:
            self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': reqs}))
        return len(reqs)

    def leer_otra_hoja(self, sheet_id, pestana, rango='A1:AZ'):
        """Valores sin formato de una pestaña de OTRA hoja (la antigua): solo lectura, para la carga inicial del unificado."""
        return self._ej(self.svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{pestana}'!{rango}", valueRenderOption='UNFORMATTED_VALUE')).get('values', [])

    def _cliente_r17(self):
        """Cliente de Sheets del hilo de R17: credenciales y conexión propias (httplib2 no es seguro entre hilos) y el mismo
        timeout corto que la lectura de la preoperativa."""
        if self._svc_r17 is None:
            creds = service_account.Credentials.from_service_account_file(
                self._cred_path, scopes=['https://www.googleapis.com/auth/spreadsheets'])
            self._svc_r17 = build('sheets', 'v4', cache_discovery=False, http=google_auth_httplib2.AuthorizedHttp(
                creds, http=httplib2.Http(timeout=self._timeout_r17)))
        return self._svc_r17

    def leer_fila_r17(self, fila, col):
        """La fila `fila` (0-based) de la columna A a `col` inclusive, para comprobar la etiqueta antes de escribir R17."""
        v = self._cliente_r17().spreadsheets().values().get(
            spreadsheetId=self.sid, range=f"{self.q}!A{fila + 1}:{_col_letra(col)}{fila + 1}",
            valueRenderOption='UNFORMATTED_VALUE').execute().get('values', [])
        return list(v[0]) if v else []

    def escribir_celda_r17(self, fila, col, texto):
        """Escribe UNA celda (RAW) con el cliente de R17: nunca toca otra."""
        self._cliente_r17().spreadsheets().values().update(
            spreadsheetId=self.sid, range=f"{self.q}!{vista.celda_a1(fila, col)}", valueInputOption='RAW',
            body={'values': [[texto]]}).execute()

    def _meta(self):
        m = self._ej(self.svc.spreadsheets().get(
            spreadsheetId=self.sid,
            fields='sheets(properties(sheetId,title,gridProperties),conditionalFormats)'))
        for s in m['sheets']:
            if s['properties']['title'] == self.tab:
                self.tab_id = s['properties']['sheetId']
                self.filas = s['properties']['gridProperties']['rowCount']
                self.columnas = s['properties']['gridProperties'].get('columnCount', 0)
                self.n_cf = len(s.get('conditionalFormats', []))
                return True
        return False

    def asegurar_pestana(self):
        if self._meta():
            # Layout más ancho que la pestaña (columna Calificación, 01/10/2026): se añaden las columnas que faltan antes
            # de leer y escribir hasta la última; si no, la API rechaza el rango por salirse de la cuadrícula.
            if self.columnas < N_COLS:
                self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': [
                    {'appendDimension': {'sheetId': self.tab_id, 'dimension': 'COLUMNS',
                                         'length': N_COLS - self.columnas}}]}))
                self.columnas = N_COLS
            return False
        r = self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': [
            {'addSheet': {'properties': {'title': self.tab, 'gridProperties': {
                'rowCount': FILAS_MIN, 'columnCount': N_COLS}}}}]}))
        self.tab_id = r['replies'][0]['addSheet']['properties']['sheetId']
        self.filas, self.columnas, self.n_cf = FILAS_MIN, N_COLS, 0
        return True

    def leer(self):
        return self._ej(self.svc.spreadsheets().values().get(
            spreadsheetId=self.sid, range=f"{self.q}!A1:{_col_letra(N_COLS - 1)}",
            valueRenderOption='UNFORMATTED_VALUE')).get('values', [])

    def _meta_objetivos(self):
        m = self._ej(self.svc.spreadsheets().get(spreadsheetId=self.sid, fields='sheets(properties(sheetId,title,gridProperties))'))
        for s in m['sheets']:
            if s['properties']['title'] == self.tab_obj:
                g = s['properties']['gridProperties']
                self.obj_id, self.obj_filas, self.obj_cols = s['properties']['sheetId'], g['rowCount'], g.get('columnCount', 0)
                return True
        return False

    def asegurar_pestana_objetivos(self):
        """Crea la pestaña del recuento semanal (spec §11.3) si falta. True si la creó."""
        if self._meta_objetivos():
            return False
        r = self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': [
            {'addSheet': {'properties': {'title': self.tab_obj, 'gridProperties': {
                'rowCount': FILAS_MIN, 'columnCount': len(vista.cabecera_objetivos(self.R))}}}}]}))
        self.obj_id = r['replies'][0]['addSheet']['properties']['sheetId']
        self.obj_filas, self.obj_cols = FILAS_MIN, len(vista.cabecera_objetivos(self.R))
        return True

    def leer_objetivos(self):
        return self._ej(self.svc.spreadsheets().values().get(
            spreadsheetId=self.sid, range=f"{self.q_obj}!A1:{_col_letra(len(vista.cabecera_objetivos(self.R)) - 1)}",
            valueRenderOption='UNFORMATTED_VALUE')).get('values', [])

    def escribir_objetivos(self, lay):
        """Reescritura completa de la pestaña del recuento semanal (spec §11.3): valores, negrita del encabezado, rojo de los
        ✘ y fondo de control de la columna manual. Todo en UNA petición (11/11/2026): o se aplica entera o no cambia nada."""
        self._meta_objetivos()
        C = self.R['colores']
        n_filas, n_cols = len(lay['valores']), lay['n_cols']
        reqs = []
        if n_filas + 50 > self.obj_filas:
            reqs.append({'appendDimension': {'sheetId': self.obj_id, 'dimension': 'ROWS',
                                             'length': n_filas + FILAS_MIN - self.obj_filas}})
        if n_cols > self.obj_cols:
            reqs.append({'appendDimension': {'sheetId': self.obj_id, 'dimension': 'COLUMNS', 'length': n_cols - self.obj_cols}})
        reqs.append({'updateCells': {'range': {'sheetId': self.obj_id}, 'fields': 'userEnteredFormat,dataValidation'}})
        # valores: las celdas del rango que no vienen en `rows` quedan vacías (sustituye al clear + update de antes)
        reqs.append({'updateCells': {'range': {'sheetId': self.obj_id, 'startRowIndex': 0, 'startColumnIndex': 0,
                                               'endColumnIndex': n_cols},
                                     'rows': _filas_valores(lay['valores'], n_cols), 'fields': 'userEnteredValue'}})

        def rango(f, c):
            return {'sheetId': self.obj_id, 'startRowIndex': f, 'endRowIndex': f + 1,
                    'startColumnIndex': c, 'endColumnIndex': c + 1}

        reqs.append({'repeatCell': {'range': {'sheetId': self.obj_id, 'startRowIndex': 0, 'endRowIndex': 1,
                                              'startColumnIndex': 0, 'endColumnIndex': n_cols},
                                    'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}},
                                    'fields': 'userEnteredFormat.textFormat.bold'}})
        for celdas, color in ((lay['manuales'], C['preoperativa']), (lay['rojos'], C['rojo'])):     # el rojo, el último
            for f, c in celdas:
                reqs.append({'repeatCell': {'range': rango(f, c), 'cell': {'userEnteredFormat': {
                    'backgroundColor': _color(color)}}, 'fields': 'userEnteredFormat.backgroundColor'}})
        self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': reqs}))

    def leer_filas(self, fila, col, n=1):
        """`n` filas desde `fila` (0-based), de la columna A hasta `col` inclusive, en UNA petición y con el cliente de timeout
        corto (spec §7.2, §10.6: la estrategia y las filas de la compuerta no se piden por separado). Siempre `n` filas
        (la API omite las vacías del final)."""
        v = self._ej(self.svc_rapido.spreadsheets().values().get(
            spreadsheetId=self.sid, range=f"{self.q}!A{fila + 1}:{_col_letra(col)}{fila + n}",
            valueRenderOption='UNFORMATTED_VALUE'), rapido=True).get('values', [])
        return [list(r) for r in v] + [[] for _ in range(n - len(v))]

    def escribir(self, lay):
        """Reescritura completa: valores, formatos, formato condicional y desplegable. Todo en UNA petición `batchUpdate`
        (11/11/2026), que Sheets aplica entera o no aplica: ya no hay un momento con la pestaña vacía (antes eran tres
        peticiones: vaciar, formatos y valores). Los valores van como `updateCells` de las columnas del layout; las celdas
        de ese rango que no vienen en `rows` quedan vacías. Los VALORES que haya a la derecha del layout no se tocan, pero los
        formatos, las validaciones y las reglas de formato condicional de TODA la pestaña se borran y se vuelven a poner: un
        formato propio puesto a mano fuera del layout se pierde en cada sincronización."""
        self._meta()
        n_filas = len(lay['valores'])
        reqs = []
        if n_filas + 50 > self.filas:
            extra = n_filas + FILAS_MIN - self.filas
            reqs.append({'appendDimension': {'sheetId': self.tab_id, 'dimension': 'ROWS', 'length': extra}})
            self.filas += extra
        todo = {'sheetId': self.tab_id}
        reqs.append({'updateCells': {'range': todo, 'fields': 'userEnteredFormat,dataValidation'}})
        reqs.append({'updateCells': {'range': {'sheetId': self.tab_id, 'startRowIndex': 0, 'startColumnIndex': 0,
                                               'endColumnIndex': N_COLS},
                                     'rows': _filas_valores(lay['valores'], N_COLS), 'fields': 'userEnteredValue'}})
        reqs += [{'deleteConditionalFormatRule': {'sheetId': self.tab_id, 'index': 0}}] * self.n_cf
        reqs += self._formatos(lay)
        self._ej(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': reqs}))

    def _rango(self, f0, f1, c0, c1=None):
        return {'sheetId': self.tab_id, 'startRowIndex': f0, 'endRowIndex': f1,
                'startColumnIndex': c0, 'endColumnIndex': (c1 if c1 is not None else c0 + 1)}

    def _formatos(self, lay):
        C = self.R['colores']
        nb = lay['n_bal']
        # la zona de operaciones cubre hasta el final de la cuadrícula: formatos, colores y desplegable
        # quedan puestos también para las filas que lleguen después
        oi, oh = lay['ops_ini'], lay['ops_header']
        of = self.filas
        reqs = []

        def negrita(fila):
            reqs.append({'repeatCell': {'range': self._rango(fila, fila + 1, 0, N_COLS),
                                        'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}},
                                        'fields': 'userEnteredFormat.textFormat.bold'}})

        def numero(f0, f1, col, patron, tipo='NUMBER'):
            if f1 <= f0:
                return
            reqs.append({'repeatCell': {'range': self._rango(f0, f1, col),
                                        'cell': {'userEnteredFormat': {'numberFormat': {'type': tipo, 'pattern': patron}}},
                                        'fields': 'userEnteredFormat.numberFormat'}})

        def cf(f0, f1, col, cond, valor, color):
            if f1 <= f0:
                return
            reqs.append({'addConditionalFormatRule': {'index': 0, 'rule': {
                'ranges': [self._rango(f0, f1, col)],
                'booleanRule': {'condition': {'type': cond, 'values': [{'userEnteredValue': str(valor)}]},
                                'format': {'backgroundColor': _color(color)}}}}})

        negrita(0)
        negrita(oh)
        # BAL
        for c in ('Balance final', 'Threshold', 'Threshold Rithmic (manual)', 'Resultado (día, neto)'):
            numero(1, 1 + nb, BC[c], '#,##0.00')
        numero(1, 1 + nb, BC['Distancia al threshold'], '#,##0.0')      # ticks de NQ (30/09/2026)
        numero(1, 1 + nb, BC['Margen de operativa'], '0.0')
        numero(1, 1 + nb, BC['Ratio B/R (día)'], '0.00')
        numero(1, 1 + nb, BC['Fecha'], 'dd/mm/yyyy', 'DATE')
        cf(1, 1 + nb, BC['Margen de operativa'], 'NUMBER_LESS_THAN_EQ', C['margen_rojo_hasta'], C['rojo'])
        # Operaciones
        for c in ('Balance final', 'Resultado ($)', 'Comisión'):
            numero(oi, of, OC[c], '#,##0.00')
        numero(oi, of, OC['Margen de operativa'], '0.0')
        for c in ('Resultado (ticks)', 'Flotante aguantado (ticks)(MAE)', 'Flotante máx (ticks)(MFE)',
                  'SL (ticks)', 'TP (ticks)', 'Máxima amplitud del movimiento',       # ticks desde el 01/10/2026
                  'Acumulado del día (ticks)', 'Ticks perdidos del día'):                # V5 (spec §16.7)
            numero(oi, of, OC[c], '0.##')
        numero(oi, of, OC['Entrada'], '0.00##')
        numero(oi, of, OC['Ratio B/R'], '0.00')
        numero(oi, of, OC['Fecha y hora apertura'], 'hh:mm:ss', 'DATE_TIME')
        numero(oi, of, OC['Duración (s)'], '0')
        numero(oi, of, OC['Nº en el día'], '0')
        numero(oi, of, OC['Racha'], '+0;-0;0')
        numero(oi, of, OC['OrderId'], '0', 'TEXT')
        for c in ('Resultado (ticks)', 'Resultado ($)'):
            cf(oi, of, OC[c], 'NUMBER_GREATER', 0, C['positivo'])
            cf(oi, of, OC[c], 'NUMBER_LESS', 0, C['negativo'])
        cf(oi, of, OC['Duración (s)'], 'NUMBER_GREATER', C['duracion_roja_mayor_que'], C['rojo'])
        # preoperativa (spec §7.2): desplegable de la estrategia; la columna Estrategia de las operaciones queda libre
        pf, pc = lay['preop_fila'], lay['preop_col']
        reqs.append({'setDataValidation': {'range': self._rango(pf, pf + 1, pc), 'rule': {
            'condition': {'type': 'ONE_OF_LIST',
                          'values': [{'userEnteredValue': e} for e in lay['estrategias']]},
            'showCustomUi': True, 'strict': True}}})         # solo valores de la lista
        reqs.append({'repeatCell': {'range': self._rango(pf, pf + 1, 0),
                                    'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}},
                                    'fields': 'userEnteredFormat.textFormat.bold'}})
        reqs.append({'repeatCell': {'range': self._rango(pf, pf + 1, pc), 'cell': {'userEnteredFormat': {
            'backgroundColor': _color(C['preoperativa']), 'textFormat': {'bold': True}}},
            'fields': 'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold'}})
        # estado del mercado (spec §12): desplegable estricto en la misma columna, encima de la estrategia
        mf = lay['mercado_fila']
        reqs.append({'setDataValidation': {'range': self._rango(mf, mf + 1, pc), 'rule': {
            'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': v}
                                                           for v in self.R['preoperativa']['mercado']['valores']]},
            'showCustomUi': True, 'strict': True}}})
        reqs.append({'repeatCell': {'range': self._rango(mf, mf + 1, 0),
                                    'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}},
                                    'fields': 'userEnteredFormat.textFormat.bold'}})
        reqs.append({'repeatCell': {'range': self._rango(mf, mf + 1, pc), 'cell': {'userEnteredFormat': {
            'backgroundColor': _color(C['preoperativa']), 'textFormat': {'bold': True}}},
            'fields': 'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold'}})
        # compuerta (spec §10.6): una fila SÍ/NO por texto, debajo de la estrategia, en su misma columna
        Q = self.R['preoperativa']['compuerta']
        cf0, nc = lay['compuerta_fila'], lay['compuerta_n']
        if nc:                      # V5: sin filas de compuerta (un rango vacío no vale en la API)
            reqs.append({'setDataValidation': {'range': self._rango(cf0, cf0 + nc, pc), 'rule': {
                'condition': {'type': 'ONE_OF_LIST',
                              'values': [{'userEnteredValue': Q['afirmativo']}, {'userEnteredValue': Q['negativo']}]},
                'showCustomUi': True, 'strict': True}}})
            reqs.append({'repeatCell': {'range': self._rango(cf0, cf0 + nc, 0),
                                        'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}},
                                        'fields': 'userEnteredFormat.textFormat.bold'}})
            reqs.append({'repeatCell': {'range': self._rango(cf0, cf0 + nc, pc), 'cell': {'userEnteredFormat': {
                'backgroundColor': _color(C['preoperativa']), 'textFormat': {'bold': True}}},
                'fields': 'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold'}})
        C5 = (self.R.get('v5') or {}).get('colores') or {}
        negro = {'foregroundColor': _color('#000000')}
        for fila, col in lay.get('celdas_naranja', ()):       # CONSEJO IGNORADO (spec §16.5): fondo naranja, letra negra
            reqs.append({'repeatCell': {'range': self._rango(fila, fila + 1, col), 'cell': {'userEnteredFormat': {
                'backgroundColor': _color(C5.get('consejo', C['rojo'])), 'textFormat': negro}},
                'fields': 'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.foregroundColor'}})
        if lay.get('recordatorio_fila') is not None:          # recordatorio de inicio de turno (spec §16.3): naranja, negrita
            rf = lay['recordatorio_fila']
            reqs.append({'repeatCell': {'range': self._rango(rf, rf + 1, 1, 9), 'cell': {'userEnteredFormat': {
                'backgroundColor': _color(C5.get('recordatorio', C['preoperativa'])),
                'textFormat': {'bold': True, 'foregroundColor': _color('#000000')}}},
                'fields': 'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat'}})
        for fila, col in lay.get('celdas_cortafuegos', ()):   # cortafuegos: fondo rojo, letra negra
            reqs.append({'repeatCell': {'range': self._rango(fila, fila + 1, col), 'cell': {'userEnteredFormat': {
                'backgroundColor': _color(C['rojo']), 'textFormat': {'foregroundColor': _color('#000000')}}},
                'fields': 'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.foregroundColor'}})
        for fila, col in lay.get('celdas_manuales', ()):      # MAE/MFE corregidos a mano
            reqs.append({'repeatCell': {'range': self._rango(fila, fila + 1, col),
                                        'cell': {'userEnteredFormat': {'backgroundColor': _color(C['manual'])}},
                                        'fields': 'userEnteredFormat.backgroundColor'}})
        for fila, col in lay.get('celdas_semana', ()):        # fila SEMANA del BAL con algún ✘ (spec §11.2)
            reqs.append({'repeatCell': {'range': self._rango(fila, fila + 1, col), 'cell': {'userEnteredFormat': {
                'backgroundColor': _color(C['rojo'])}}, 'fields': 'userEnteredFormat.backgroundColor'}})
        for c in lay.get('mostrar_columnas', ()):   # la que ocultó el demonio con el layout anterior (vista.columnas_a_mostrar)
            reqs.append({'updateDimensionProperties': {
                'range': {'sheetId': self.tab_id, 'dimension': 'COLUMNS', 'startIndex': c, 'endIndex': c + 1},
                'properties': {'hiddenByUser': False}, 'fields': 'hiddenByUser'}})
        reqs.append({'updateDimensionProperties': {
            'range': {'sheetId': self.tab_id, 'dimension': 'COLUMNS',
                      'startIndex': OC['OrderId'], 'endIndex': OC['OrderId'] + 1},
            'properties': {'hiddenByUser': True}, 'fields': 'hiddenByUser'}})
        return reqs

    def leer_verificacion(self):
        """Para comprobar el resultado: valores, reglas condicionales y validaciones."""
        vals = self.leer()
        m = self.svc.spreadsheets().get(
            spreadsheetId=self.sid, ranges=[f"{self.q}!A1:{_col_letra(N_COLS - 1)}{len(vals) + 1}"],
            fields='sheets(properties(title,gridProperties),conditionalFormats,'
                   'data(columnMetadata(hiddenByUser),rowData(values(dataValidation))))').execute()
        return vals, m['sheets'][0]
