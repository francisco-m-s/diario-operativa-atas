# Registro ATAS: diario de operaciones automático (V5.1)

Contrato: `SPEC_V4.md` (§16 = V5, manda desde la sesión del 11/11/2026). Fuente de verdad: `atas_v4.sqlite`. El sheet (`Registro_bot_V5`,
pestaña `V5`) es una vista regenerada.

**Versión 5.1.** Sin cambios de reglas respecto a la V5; el detalle está en `SPEC_V4.md` §16.13.

## Instalación y configuración
1. **Python 3.12 en Windows** y `pip install -r requirements.txt`.
2. **Configuración en dos capas.** `config.yaml` va en el repositorio y no lleva datos personales (valores por defecto). Lo tuyo va en **`config.local.yaml`**, que git ignora y que se superpone a `config.yaml` clave a clave: `credencial`, `sheet_id`, `sheet_id_antiguo`, `quemadas_iniciales`… Por defecto `atas_dir` es `%APPDATA%\ATAS`, la carpeta estándar de ATAS.
3. **Credencial de Google (cuenta de servicio):** el `.json` se guarda **fuera del repositorio** y solo se referencia, con `credencial: <ruta>` en `config.local.yaml` o con la variable de entorno `ATAS_SHEETS_CREDENCIAL` (manda sobre el fichero). Comparte la hoja con el correo de esa cuenta de servicio.
4. **`cuentas.yaml`:** cópialo de `cuentas.example.yaml`; lo gestiona el demonio y está en `.gitignore`.
5. **Comprueba** con `python -m pytest tests -q`. Los tests son sintéticos: no tocan Google ni tus datos.

## Seguridad al contribuir
- **Nunca subas** tu `config.local.yaml`, tu `cuentas.yaml`, la base `*.sqlite`, tus logs ni ninguna credencial: el `.gitignore` es una
  lista blanca y ya los excluye.
- Activa el *hook* una vez por clon: `git config core.hooksPath .githooks`. Cada commit pasa por `herramientas/comprobar_secretos.py`,
  que bloquea claves y credenciales y **nunca imprime el valor**, solo `fichero:línea` y tipo.
- Si alguna vez se sube una clave: **revócala** (borrar el fichero o el commit no basta) y crea otra.

## V5 en dos minutos (11/11/2026)
- **Reglas de máquina:** las pone Rithmic (tope diario +1.050 $ en pruebas y +119 $ en reales; pérdida máxima de las reales −175 $). El demonio no las aplica.
- **Consejos que no pedí** (por sesión CME, sobre decisiones cerradas): 4 ganadoras sin perder → no operar más; 3 perdedoras seguidas → parar y analizar; 407 t perdidos → fin de sesión; 17:55 → fin de turno inminente. Notificación muda + `profit.wav` / `stoploss.wav` de ATAS, dos veces.
- **Infracciones:** rojo = `CONTRATOS > PERMITIDOS`, `OPERACIÓN TRAS OBJETIVO`, `SIN ESTRATEGIA`, `FUERA DE HORARIO` (15:30–18:00 de Madrid); naranja = `CONSEJO IGNORADO: …`. Todas suenan al abrir.
- **Banda:** ganadora ≥ +20 t, perdedora ≤ −20 t.
- **Turnos:** de arrancar el demonio a cerrarlo (en SQLite, no en la hoja). Al arrancar, recordatorio naranja hasta la primera operación.
- **Histórico unificado:** pestaña con una fila por decisión; el demonio solo añade al archivar. Carga inicial: `python cli_v4.py unificado`.
- **Foto del cálculo:** lo archivado no se recalcula desde el log; `python cli_v4.py foto [--comprobar|--quitar]`.
- Todo en `reglas.yaml` → `v5`. Lo anterior al 11/11 conserva las reglas de la V4.

## Arrancar
- Acceso directo del escritorio **"Iniciar Demonio ATAS V5"** (→ `INICIAR_DEMONIO_ATAS_V5.bat`; el `…_V4.bat` antiguo lo llama).
- En la consola del demonio: `sync` fuerza una escritura, `q` sale.
- Una sola instancia (mutex `Global\ATAS_DAEMON_V4`). Convive con V2 y V3.
- Escribe el sheet cuando una cuenta queda plana con operaciones nuevas, al cambiar de sesión CME y a petición.
- Cortafuegos: registra el evento (log `[CORTAFUEGOS]` y sheet; **sin sonido ni notificación de Windows desde el 11/11/2026**, `cortafuegos.aviso` en `reglas.yaml`) en cuanto el evento llega al log, sin esperar a la escritura del sheet.
  Solo si la decisión tiene alguna cuenta real (Apex PA); las de solo pruebas quedan en Observaciones demonio (30/09/2026).
- Log: `atas_v4.log`. Copia diaria de la SQLite: `backups\atas_v4_YYYYMMDD.sqlite` (se guardan 30).

## Acciones del operador (`cli_v4.py`)
```
python cli_v4.py quemar <cuenta>
python cli_v4.py retiro <importe> <cuenta>        # p. ej. retiro 1500 PA-APEX-91120-16
python cli_v4.py sim activar|desactivar <cuenta_sim>
python cli_v4.py reset <cuenta física> <desde>     # p. ej. reset APEX-91120-74 "2026-10-28 21:06:59.587"
python cli_v4.py sync
python cli_v4.py archivar [--hasta YYYY-MM-DD]   # cierra el ciclo: copia la pestaña V4 a otra con sus fechas (spec §15)
python cli_v4.py unificado                         # carga inicial o reparación del histórico unificado (demonio parado)
python cli_v4.py foto [--comprobar|--quitar]       # foto del cálculo de lo archivado (demonio parado)
python cli_v4.py cuentas
```
`archivar` (spec §15, 11/11/2026): la pestaña `V4` solo enseña el ciclo en curso. Al archivar se copia entera a `V4 <desde> a <hasta>` y la
`V4` empieza de cero (el BAL no cambia; SQLite no pierde nada). También vale escribir `archivar` en la consola del demonio. La pestaña
archivada es una foto fija: lo que se edite en ella no vuelve al demonio.
Si el demonio está en marcha, él sincroniza en el siguiente ciclo; si no, lo hace el propio CLI.
`reset` (spec §14, 03/11/2026) se usa **con el demonio parado** (como cualquier edición de `cuentas.yaml`; en marcha, se niega).
Apex resetea una evaluación conservando el ID de Rithmic: `<desde>` es la hora del log de ATAS (Madrid, reloj del PC) del corte, con la
cuenta plana. Desde entonces, todo lo de esa cuenta física es de la cuenta nueva `<ID>-resetN` (N automático), que arranca de cero
(margen 500, racha 0, ancla estimada, sin threshold manual, retiros ni semillas); lo anterior se queda en la original, quemada o no.
Registra el reset en `cuentas.yaml` (`resets:`), reasigna lo ya ingerido (idempotente) y sincroniza. Antes, copia `atas_v4.sqlite` y
`cuentas.yaml` a `backups\`. No hay "deshacer": se vuelve atrás restaurando esas copias.
Python: 3.12 (el `.bat` de arranque lo lanza con la ruta de tu instalación; ajústala ahí si cambia).

## En el sheet (lo que edita el operador)
- BAL, columna **Threshold Rithmic (manual)**: si es mayor que el calculado, manda y queda como ancla.
- Operaciones: **Estrategia** (desplegable de `Playbook.cdb`), **Observaciones**, **Análisis LLM**. Se conservan por ID.
- Preoperativa (encima de las operaciones): `Estrategia a usar` y, debajo, las 4 filas SÍ/NO de la compuerta (el demonio las vacía al empezar cada sesión CME; spec §10.6).
- A la derecha de la celda de `Estrategia a usar` (columna J): `R17 461 t · stop 172 t · 16:29:24`, solo informativo (R17 y stop calibrado de las últimas 5 M204s; la hora es la de Madrid del cierre de la última). La escribe el demonio, no se edita (spec §13).
- En **Observaciones**, escribir `espejo` marca la decisión con `ESPEJO` y `no espejo` se lo quita (manda sobre la detección automática; spec §10.5).
- Todo lo demás se reescribe en cada sincronización.

## Dónde están las reglas
- `reglas.yaml`: símbolos, comisiones (con estado VERIFICADO/NO_VERIFICADO), firmas (drawdown/tope), patrones de cuenta,
  ventana de copia, margen, ratio (TP implícito 136), racha, cortafuegos (esperas, ventanas, textos y aviso),
  textos y cortes de Observaciones demonio, colores; desde el 30/09/2026 también cuentas reales, Apex PA (contratos por
  símbolo, inactividad, margen levantado, días +100) y pruebas (NQ-equivalentes, objetivo, máximo y pérdida diaria);
  desde el 07/10/2026, el tope del SL de las pruebas por el Loss Limit de Rithmic (`pruebas.tope_sl`, spec §9.10). Desde el 07/10/2026 también el Plan 4 (spec §10; sus reglas rigen solo desde la sesión CME del 08/10/2026, `plan4.desde`): E2T real (`^livesim`), `margen.bono`, contratos de las pruebas de 25K por la distancia al threshold (`pruebas.contratos.por_distancia`), pérdidas seguidas (`cortafuegos.seguidas`), `espejo` y la compuerta de la preoperativa (`preoperativa.compuerta`). Desde el 09/10/2026, R17 y su stop (`preoperativa.r17`, spec §13, informativo).
- `semillas.yaml`: días anteriores a la V4 (spec §8.3, §9.5). El demonio solo lo lee; cambiarlo exige reiniciarlo.
- `cuentas.yaml`: registro de cuentas (lo mantiene el sistema; editar solo con el demonio parado).
- `config.yaml` (en el repositorio, sin datos personales) y `config.local.yaml` (tuyo, ignorado por git; manda sobre el anterior): rutas, sheet, credencial, sesión de arranque (22/09/2026), cuentas quemadas previas.

## Código
- `atasv4/fuentes.py` lectura de `.cdb`, logs y `CopyTrading.cnf`.
- `atasv4/almacen.py` esquema SQLite e ingesta incremental idempotente (con la línea de cada evento del log).
- `atasv4/reloj.py` reloj ATAS: hora del log (reloj del PC) → hora del exchange, por tramos entre arranques y saltos.
- `atasv4/motor.py` operaciones (FIFO desde fills), margen, threshold, ratio, racha, observaciones.
- `atasv4/vista.py` + `atasv4/sheets.py` layout y escritura de la pestaña.
- `atasv4/cortafuegos.py` infracciones de entrada, preventivos al cerrar, tabla `cortafuegos_saltados` y vigilancia en vivo.
- `atasv4/aviso.py` sonido del sistema + notificación de Windows (toast), sin servicios externos.
- `atasv4/sync.py` orden de escritura (manuales del sheet → SQLite → reescritura) y backup.
- `atasv4/resets.py` reset de una cuenta (spec §14): `cuenta_logica(registro, cuenta física, hora)`, el único sitio donde se traduce cuenta física + hora a cuenta lógica `<ID>-resetN`; la ingesta (`almacen.py`) lo aplica a cada evento y `almacen.reasignar_resets` a lo ya guardado. `cuentas.reset` lo registra.
- `atasv4/v5.py` reglas de la V5 (spec §16): banda, horario, consejos del día, columnas del día, turnos y ratio planeado.
- `atasv4/r17.py` R17 y stop calibrado en la preoperativa (spec §13, informativo): hilo propio que lee las M204s del indicador (solo lectura) y escribe UNA celda; reglas en `reglas.yaml` → `preoperativa.r17`.

## Tests
```
python -m pytest tests -q
```
`python -m pytest tests -q`. Los tests de regresión sobre logs reales de ATAS **no se publican** (contienen operaciones reales): aquí aparecen como *skipped* con ese motivo. El resto usa datos sintéticos. Los de `tests/test_historico_generado.py` usan el generador de datos ficticios [`generador-logs-pruebas`](https://github.com/francisco-m-s/generador-logs-pruebas) (otro proyecto) y se saltan si no está instalado.

## Aviso
Herramienta para llevar el propio diario de operaciones: **no es asesoramiento financiero** ni garantiza resultados, y se ofrece «tal
cual» (ver `LICENSE`). ATAS, Rithmic, Apex Trader Funding, Earn2Trade y Bulenox son marcas de sus propietarios; este proyecto no está
afiliado a ellas ni respaldado por ellas.

## Licencia
MIT: software libre y gratuito. Ver `LICENSE`.
