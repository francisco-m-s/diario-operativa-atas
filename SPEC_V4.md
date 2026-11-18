# Especificación V4 / V5 — Registro de operaciones ATAS

> **Desde el 11/11/2026 el sistema es la V5** (§16: grill Q1–Q47). Este documento sigue siendo el contrato: §1–§15 describen
> la V4, que sigue rigiendo para todo lo anterior a la sesión CME del 11/11/2026; **§16 dice qué cambia desde esa sesión y
> manda sobre lo anterior donde lo contradiga**. La carpeta, los ficheros y los módulos conservan el nombre `v4`
> (decisión del operador: renombrarlos rompía rutas, copias y accesos; ver §16.11).

**Estado:** confirmada por el operador el 2026-09-22 (grill Q1–Q94). Cualquier cambio de regla se refleja aquí antes de tocar código. **§8 (Apex PA: contratos, inactividad y margen levantado) y §9 (pruebas) se aprobaron el 30/09/2026, en dos grills, y se implementan juntos.** Afectan también a §4.5 (sonido), §6 (threshold de las evaluaciones Apex) y §7.1 (columnas del BAL). **§10 (Plan 4, grill del 07/10/2026) se implementó el 07/10/2026** y modifica §4.5, §5, §7.2, §8 y §9 donde lo dice (manda sobre ellos). **Rige solo desde la sesión CME del 08/10/2026 (§10.0).** **§13 (R17 en la preoperativa, 09/10/2026) es solo informativa: no es una regla del Plan 4.** **§14 (reset de una cuenta, 03/11/2026) se implementó el 03/11/2026.** El estado de la implementación está en `INFORME_V4.md`.
**Antecedente:** `..\INFORME_V3_PARA_V4.md` (hechos verificados de las fuentes, fallos V2/V3).

---

## 1. Arquitectura

- **Ubicación:** `D:\proyectos\conceptos\atas\v4\` → código, `atas_v4.sqlite`, `cuentas.yaml`, `reglas.yaml`, `backups\` (copia local diaria de la SQLite).
- **Fuente de verdad:** SQLite. El sheet es una vista regenerada.
- **Fuentes ATAS:**
  - `Logs\app_*.log`: fills (`Order trade`, en ATAS 7 y 8; `Processing trade`, con la hora del exchange en ms, solo hasta ATAS 7), órdenes y etiquetas SL/TP (`Order registered … SL|TP`), modificaciones y cancelaciones, posición (`Position changed`), flotante de la cuenta (`Portfolio changed … PnL:<abierto>/<cerrado>`, irregular: cada ~1–3 s con posición abierta en ATAS 7, cada ~10 s en ATAS 8) y arranques de ATAS (`Started v…`). Rotan a ~6 días.
    - `Order trade <OrderId>/<tag>/<Id del fill>` no trae la cuenta: se toma de su orden en el log (o, si el log no la tiene, del fill de `MyTrade.cdb` con el mismo Id); si aún no se conoce, el fill espera en SQLite a una ingesta posterior. ATAS lo repite al reconectar: un fill por Id, con la primera hora. Si hay `Processing trade` del mismo fill, manda él (hora del exchange).
    - **Formato del Id del fill** (01/10/2026), igual en el log y en `MyTrade.cdb`: hasta ATAS 8.0.14, `<número>` (`Order trade 2642003069/41932828/1020511 …`); desde 8.0.15, `<OrderId>_<número>` (`Order trade 2642617452/43707906/2642617452_1025723 …`). Se guarda normalizado al número (`1020511`, `1025723`): el mismo fill tiene el mismo Id en las dos versiones y en las dos fuentes, para el cruce log ↔ MyTrade, la regla de un fill por Id, la cuenta de un `Order trade` pendiente y las muestras del reloj ATAS.
    - Decimales con coma hasta ATAS 8.0.14 (`at 30884,75`, `Bal:24783,01`) y con punto desde 8.0.15 (`at 30821.25`, `Bal:24684.03`), sin separador de miles: se leen igual.
  - `Database\HistoryMyTrade.cdb`: patas FIFO (UTC, PnL bruto). Desde ATAS 8.0.15 trae además `EnterTradeId` y `ExitTradeId` (no se usan; las columnas se leen por nombre).
  - `Database\MyTrade.cdb`: fills con `Id` (exchange; formato de arriba) y `OrderId` (broker).
  - `Database\Playbook.cdb`: nombres de estrategia.
  - `CopyTrading.cnf`: cuentas líder/seguidora.
- **Reloj ATAS** (decisión del operador, 28/09/2026; tramos y orden de línea, 29/09/2026): **la hora buena es siempre la de ATAS/Rithmic (exchange), nunca la del reloj del PC**; si hay desfase, se corrige hacia la del exchange. Las horas del log y el "ahora" del demonio son del reloj del PC.
  - **Tramos.** El reloj del PC salta: entre arranques de ATAS y también con ATAS abierto, cuando w32time lo corrige (−3,465 s el 21/09 a las 21:16:02; −7,131 s el 28/09 a las 17:32:17). Un tramo empieza en cada arranque de ATAS (`Started v…`) y en cada salto. ATAS delata el salto con una línea `Latency manager: <conexión> time: <PC, UTC> event: <exchange, UTC> diff: <−desfase que medía> delay: <salto>`: es salto si |delay| ≥ `reloj.salto_min_s` y la conexión es de `reloj.fuente_latencia` (Rithmic). Binance no cuenta: su hora deriva por su cuenta (23/09 18:10:43, delay −1,28 s sin salto del PC). Las líneas de un mismo salto (una por conexión) separadas ≤ `reloj.salto_agrupar_s` son un solo corte. ATAS 8.0.15 escribe además `Latency manager: <conexión> market data latency: median N ms, peak N ms` (y `back to normal …`), sin `time/event/diff/delay`: no se usan. Con 8.0.15 aún no se ha visto ningún salto, así que no consta que siga escribiendo la línea del salto (01/10/2026).
  - `desfase = hora del log − hora del exchange`, uno por tramo, mediana de sus muestras:
    - exactas (ms): fills con `Processing trade` cruzados por Id, y las líneas `Latency manager` de las conexiones de `reloj.fuente_latencia` (Rithmic): `time − event` para el tramo que empieza y `−diff` para el que acaba;
    - si el tramo no tiene exactas, las de `MyTrade.cdb` (truncada al segundo: centro del segundo).
    - No cuentan las cuentas sim ni las muestras de más de `reloj.desfase_max_s` (repeticiones al reconectar).
  - Tramo sin muestras: el del tramo con muestras más cercano en el tiempo **del mismo día (Madrid) y sin un salto de por medio**; si no hay, `reloj.desfase_por_defecto_s`. Nunca se hereda de otro día ni a través de un salto.
  - Tras un salto hacia atrás el reloj del PC repite unos segundos: una línea escrita antes del salto pertenece a su tramo por su **posición en el fichero**, no por su hora.
  - **Orden de línea.** Cada evento del log se guarda con su fichero y su posición (byte) en él. En el mismo ms manda el orden de las líneas. Los fills de una cuenta y símbolo se ordenan por la hora del log corregida y la línea cuando todos están en el log; si falta alguno, por la hora del exchange. Una hora estimada nunca adelanta así a una real.
  - Todas las horas del log se usan **corregidas** (hora del exchange): la del fill sin hora de exchange (hasta que esté en MyTrade), el cierre forzado por `Position changed`, las ventanas de flotante, SL/TP, SIN SL y SL cancelado, los `Bal:` y los avisos del cortafuegos. Dentro de un tramo es lo mismo que compararlas crudas; si un salto cae dentro de una operación, no la deforma. El "ahora" del demonio, las lecturas de la preoperativa (§7.2), los retiros y la sesión CME en curso se pasan a hora del exchange con el tramo en curso.
  - En SQLite se guardan las horas crudas; la corrección se aplica al usarlas, con la mejor estimación de ese momento.
- **Arranque de datos:** sesión CME del **22/09/2026**. Nada anterior entra en la V4 (el histórico sigue en la V3).
- **Demonio vivo:** acceso directo "Iniciar Demonio ATAS V4", mutex con nombre propio. Convive con V2 y V3 hasta que el operador diga "ya podemos borrar las V2 y V3".
- **Destino:** pestaña `V4` en el sheet `Registro_bot v3` (`ID_DE_HOJA_EJEMPLO_2`), cuenta de servicio y credencial: ver `config.local.yaml` (el `.json` vive fuera del repositorio). Locale es_ES (fórmulas con `;`).
- **Escritura:** al cerrar cada operación (cuenta plana). Orden: 1) leer del sheet las columnas manuales y guardarlas en SQLite por ID; 2) reescribir la pestaña entera desde SQLite.
- **Si el demonio se cae:** sin alertas externas. Operaciones sin log disponible → `[SIN TELEMETRÍA]`, no consumen margen por MAE; valen para análisis.
- **Tests** con fixtures reales antes del demonio.

## 2. Definiciones

- **Día** = sesión CME (cambio 17:00 CT). Operaciones que cruzan el cambio de sesión: se ignora el caso (no ocurre; Rithmic las cierra).
- **Días hábiles** = lunes a viernes, festivos CME incluidos (cuentan como "no se operó").
- **Ticks** siempre por contrato. Las fórmulas se calculan en ticks; los $ son para resultados, balances y rachas.
- **Neto** = con comisiones. Todo $ mostrado es neto. Comisiones por firma (Apex MNQ 0,51, Apex NQ 1,99 verificadas en la V3; E2T NQ 2,04 por lado desde el 07/10/2026, antes 2,02: tabla de comisiones de Rithmic para E2T, help.earn2trade.com/en/articles/2425493, que cuadra con los balances del 22/09; resto de las webs de cada firma). El operador corrige si ve descuadres con Rithmic.
- **Tabla de símbolos** (config): NQ 0,25/5 $, MNQ 0,25/0,50 $, ES 0,25/12,50 $, MES 0,25/1,25 $, CL 0,01/10 $, SI 0,005/25 $. Símbolo desconocido → aviso.

## 3. Cuentas

- `cuentas.yaml` lo gestiona el sistema. Cuenta nueva → se añade sola, firma/tipo/tamaño inferidos del nombre: `PA-APEX` → Apex PA; `APEX-` → Apex evaluación; `fsern…tcp25` → E2T 25k; `BX` → Bulenox. Sin patrón → `SIN REGLA` + aviso.
- **Quemada:** solo por orden del operador. Pasa a tabla `cuentas_quemadas` (solo SQLite) y deja de tener fila BAL. Desde el 03/11/2026, la `nota` `LOGRADA (…)` distingue las evaluaciones superadas; solo se guarda, no se muestra.
- **Reset** (03/11/2026): cuenta nueva `<ID>-resetN`, sin herencia; ver §14.
- **Retiro:** solo por orden del operador ("retiro de X $ de cuenta Y"). Se registra el movimiento (baja el balance), la cuenta pasa a `<nombre>_retiros_N` y hereda margen, racha y ancla de threshold (el ancla nunca baja).
- **Nombre por operación tras retiros** (decisión del operador, 22/09/2026): cada operación muestra el nombre según los retiros registrados antes de su apertura; las anteriores al retiro conservan el nombre que tenían, las posteriores llevan `_retiros_N`.
- **Sim** (`*_sim`, y desde el 24/09/2026 también `DEMO*` y `CRYPTO*`, demos de ATAS): ignoradas por completo. Se activan temporalmente solo a petición para pruebas; jamás entran en cálculos de estrategia ni estadísticas.
- Cuenta nueva (no `_retiros_N`): margen de operativa inicial 500.

## 4. Operaciones

### 4.1 Unidad
- **SQLite:** una fila por cuenta real (líder y seguidoras), porque cada una mueve su balance.
- **Sheet:** una fila por **decisión**. Datos de la líder; seguidoras listadas en `Copias`.
- **Copia** (redefinida por el operador el 01/10/2026). Una operación es copia de otra si cumple todo esto:
  - está en **otra cuenta**, viva o quemada pero no sim;
  - tiene la **misma dirección y el mismo instrumento**;
  - su apertura queda a **≤ `copia.ventana_s` (1,0 s)** de la apertura de la líder.
  
  Se considera copia **figure o no esa relación en `CopyTrading.cnf`**. Motivo: si el operador cambia la configuración de la copia, el pasado no puede reescribirse. Caso real: el 01/10 a las 15:40 cambió la seguidora de E2T-250 de APEX-82 a APEX-83, la operación de AP82 de las 15:37 pasó a contar como decisión propia y salió un `4 OPS EN 1 H` con solo 3 decisiones.
  - **Líder:** la cuenta que figure como líder de las otras en el `CopyTrading.cnf` actual. Si ninguna figura, la de apertura más temprana (mismo reloj, ver abajo). Si empatan, la primera por orden de línea y, si sigue el empate, por nombre.
  - Todas las seguidoras se miden contra la apertura de la **líder**, no en cadena.
  - (concretado en la implementación, 01/10/2026)
    - **Figurar como líder** es ser `ProviderAccount` de cualquier cuenta del `CopyTrading.cnf` actual, aunque las seguidoras de la configuración no operen a la vez. Caso real: E2T-250 figura (de APEX-83) y por eso es la líder de la operación de APEX-82 de las 15:37:47. Si varias de las cuentas que operan a la vez figuran como líder, manda la más temprana y las demás pasan a ser copias suyas si abren a ≤ `copia.ventana_s` de **esa** apertura (no en cadena): las seguidoras de la configuración de una líder que pasó a ser copia solo cuentan como copia si caen en la ventana de la nueva líder.
    - **Desempates:** misma apertura con el mismo reloj → la primera por orden de línea del `Order trade` de su primer fill de entrada (fichero y posición; si alguna de las dos no está en el log, no hay orden de línea) y, si siguen empatadas, por nombre de cuenta, ascendente. Con la hora de MyTrade (resolución de 1 s) dos aperturas del mismo segundo están empatadas aunque sus ms difieran.
    - **Cada fila de entrada** (escalonadas y giros) tiene como máximo una líder y la líder, como máximo una fila por cuenta: la más cercana en el tiempo. Una fila sin homóloga (una entrada de más en la seguidora) es decisión propia.
    - Una cuenta **sim activada para pruebas** (§3) no es líder ni copia de nadie, aunque figure en el `CopyTrading.cnf`.
    - La copia hereda el SL/TP de la líder (ATAS copia posiciones, no órdenes): no lleva `SIN SL` propio.
  - Las copias cuentan **solo para el BAL**: balance, margen y threshold de su cuenta. No cuentan como decisión ni para la estrategia, la Calificación, los cortafuegos o cualquier cosa propia de las filas de operaciones. El sonido sigue la regla de §4.5: suena si hay alguna real entre la líder y las seguidoras.
  - El deslizamiento de precio en la seguidora es normal (modo de copia: posiciones).
  - Las dos aperturas se comparan con horas del **mismo reloj** (29/09/2026): la del exchange con ms si las dos la tienen; si no, la del log corregida si las dos están en el log; si no, el segundo de MyTrade de las dos. Comparar una hora truncada al segundo con una estimada con ms dejaba huérfana a una copia real (24/09 18:16:43: 1,121 s con 0,159 s reales).
- **Entrada escalonada** = cada entrada que aumenta la posición es una fila independiente. Reparto de salidas FIFO (patas de `HistoryMyTrade`).
- **Una orden de N contratos** al mismo precio = una fila, `Contratos = N`.
- **Giro** = dos operaciones.

### 4.2 Campos
- **ID:** `<cuenta_corta>-<YYYYMMDD>-<nnn>` (ej. `PA16-20260922-003`), generado una vez en SQLite. Columna oculta con el `OrderId` del broker del primer fill de entrada.
- **Resultado ($):** real neto (lo que mueve el balance). **Resultado (ticks):** real, por contrato.
- **Entrada:** precio del primer fill.
- **Máxima amplitud del movimiento** (decisión del operador, 01/10/2026): **cuántos ticks se ha movido la operación**, `|MAE| + |MFE|` en ticks por contrato. Usa siempre el MAE y el MFE manuales cuando existan (cada uno por separado) y los del log si no. Queda vacía si falta alguno de los dos (operación `[SIN TELEMETRÍA]` sin manual). Antes era el precio del fill de salida más alejado de la entrada.
- **Fill de salida más alejado de la entrada** (interno, no se muestra): el de mayor |precio − entrada|; en caso de empate gana el negativo. Solo sirve para `Tipo cierre`.
- **Tipo apertura / Tipo cierre:** `Mercado | Límite | Stop`. El cierre es el tipo del fill de salida más alejado de la entrada.
  - **Tipo cierre** (decisión del operador, 22/09/2026): `Mercado | Límite | TP | SL`. Orden etiquetada SL o cualquier Stop (incluido SL movido a BE/beneficio) → `SL`; etiquetada TP → `TP`; Limit sin TP → `Límite`; Market → `Mercado`; orden desconocida → vacío.
- **SL (ticks):** SL inicial. **TP (ticks):** TP final antes del cierre. Modificaciones → texto en Observaciones demonio (`SL 30→45, TP 60→40`). En pruebas, desde el 07/10/2026, el SL que cuenta tiene un tope y la nota de SL cambia: ver §9.10.
- **MAE / MFE (ticks):** extremos del flotante del log durante la operación, convertidos a ticks por contrato. Si en la cuenta hay más de una posición abierta a la vez (entrada escalonada o dos símbolos), **cada fila recibe el flotante total de la cuenta** convertido a sus ticks (castigo máximo).
  - **Editables a mano** (decisión del operador, 22/09/2026): si la celda difiere de lo último que escribió el demonio, es manual (MAE = −|x|, MFE = +|x|, coma decimal). Se aplica a todas las cuentas de la decisión (líder y seguidoras) y sustituye al log en todos los cálculos; el valor del log se conserva en SQLite. Celda vaciada → vuelve al log. Marca: fondo `#ffe599` y nota `MAE manual (log −161)` / `MFE manual (log +42)` en Observaciones demonio.
- **Ratio B/R (estático):**
  - Base: `TP final (ticks) / SL más amplio que llegó a existir (ticks)`.
  - Cierre en ganancia sin TP, o con TP sin llenar: `ticks reales / SL`.
  - Cierre en pérdida sin TP: TP implícito **+136 ticks** (fijo para todo símbolo y cuenta): `136 / SL`.
  - Sin SL: denominador = margen de operativa de la cuenta en la apertura.
- **Racha** (en $ netos, por cuenta): ganadora ≥ +50 $; perdedora ≤ −10 $; entre medias neutra (ni suma ni rompe). Valor con signo (+3, −2).
- **Resultado anterior:** ticks de la operación previa de esa cuenta.
- **Fecha y hora apertura:** Madrid, `HH:MM:SS` (valor fecha-hora de Sheets).
- **Duración (s):** número; fondo rojo si > 300.
- **Estrategia** (decisión del operador, 24/09/2026):
  - La pone el demonio al anotar la operación, con lo elegido en la preoperativa (§7.2). Por defecto, `Sin estrategia`.
  - La columna sigue siendo editable a mano, **sin desplegable y sin control**: lo que escriba el operador se conserva por ID.
- **Calificación** (decisión del operador, 01/10/2026): matriz proceso × resultado, para el análisis posterior. Se calcula para todas las operaciones, también las anteriores, porque la V4 lo recalcula todo en cada sync.
  - **Proceso incorrecto** si la operación cumple al menos una de estas condiciones:
    - lleva alguna **infracción**: `SIN SL`, `SL CANCELADO CON POSICIÓN ABIERTA`, `CONTRATOS > PERMITIDOS`, `REENTRADA TRAS PÉRDIDA`, `SOBREOPERACIÓN 10 MIN`, `SOBREOPERACIÓN 1 H`, `OPERACIÓN TRAS OBJETIVO` u `OPERACIÓN TRAS PÉRDIDA DIARIA`;
    - su estrategia final (la del sheet si el operador la editó) es `Sin estrategia`;
    - lleva `ESTRATEGIA ELEGIDA CON LA OPERACIÓN ABIERTA`;
    - lleva `LARGA` · `AGUANTANDO PÉRDIDA`.
  - En cualquier otro caso, **proceso correcto**. Las preventivas (`… NO OPERAR HASTA`, `NO OPERAR MÁS HOY`, `MÁXIMO BAL DE HOY`) y las etiquetas de duración que no sean `AGUANTANDO PÉRDIDA` no cuentan.
  - **Resultado** (`Resultado (ticks)`, por contrato): **ganador** ≥ +10 t; **perdedor** ≤ −10 t; **neutro** entre medias, en sentido estricto.
  - **Cuadrantes:**
    - proceso correcto y ganador: `Trade Perfecto`;
    - correcto y perdedor: `Buen Trade`;
    - incorrecto y ganador: `Trade Peligroso`;
    - incorrecto y perdedor: `Trade Basura`;
    - resultado neutro: `Neutro`.
  - En SQLite se guardan también `proceso_ok` y la categoría del resultado, para poder separar los neutros por proceso.
  - Cada operación de SQLite (líder y seguidoras) recibe la suya con sus propias etiquetas. El sheet muestra la de la fila de la líder.
  - Umbrales y textos en `reglas.yaml`.
  - (concretado en la implementación, 01/10/2026)
    - En una seguidora cuentan las etiquetas de su propia fila de SQLite: las de entrada que hereda de la líder (`SIN SL`, `SL CANCELADO CON POSICIÓN ABIERTA`), las de su cuenta (`CONTRATOS > PERMITIDOS`, `OPERACIÓN TRAS OBJETIVO`, `OPERACIÓN TRAS PÉRDIDA DIARIA`) y su duración, con su propio MAE. `REENTRADA TRAS PÉRDIDA` y `SOBREOPERACIÓN …` solo las lleva la fila de la decisión (§4.5: la copia no cuenta en las ventanas), así que no cuentan en la seguidora.
    - La estrategia final y la marca `ESTRATEGIA ELEGIDA CON LA OPERACIÓN ABIERTA` son de la decisión: la seguidora no tiene fila en el sheet y usa las de su líder.
    - `Sin estrategia` se reconoce sin distinguir mayúsculas ni espacios de los extremos, como en la preoperativa (§7.2). Una celda de estrategia vaciada se escribe y se califica como `Sin estrategia`.
    - La estrategia final es la del sheet ya leída en ese sync y, en una decisión que se escribe por primera vez, la que acaba de asignar la preoperativa. Por eso la calificación se calcula después de esa asignación.
    - Una operación sin telemetría no lleva `SIN SL` (lleva `[SIN TELEMETRÍA]`), así que eso no la hace incorrecta.
    - El resultado que se compara con ±10 t es el de la columna, redondeado a 2 decimales.
    - En el sheet, `Calificación` es texto sin color. La amplitud se escribe con 2 decimales y el formato `0.##` de MAE/MFE.
    - En SQLite van en la foto de `operaciones` (campo `datos`): `calificacion`, `proceso_ok` (verdadero o falso) y `categoria_resultado` (`ganador`, `perdedor` o `neutro`). No hay migración de esquema: la foto se reescribe entera en cada sync.
    - Primer arranque con la pestaña de antes: si tiene menos columnas que el layout, el demonio añade las que faltan antes de leer. Las columnas manuales se leen por el nombre de la cabecera, así que sobreviven al desplazamiento. El OrderId pasa de la columna AC a la AD, y el demonio vuelve a mostrar la AC (ahora `Análisis LLM`), que había ocultado él; una columna que haya ocultado el operador no se toca.
- **Observaciones:** manual. **Análisis LLM:** solo lo escribe una LLM a petición del operador. Ambas se preservan por ID; el demonio nunca las escribe.

### 4.3 Columnas (orden)
`ID | Cuenta | Copias | Balance final | Margen de operativa | Largo/Corto | Tipo apertura | Tipo cierre | Estrategia | Resultado (ticks) | Resultado ($) | Flotante aguantado (ticks)(MAE) | Flotante máx (ticks)(MFE) | SL (ticks) | TP (ticks) | Entrada | Máxima amplitud del movimiento | Observaciones | Observaciones demonio | Calificación | Ratio B/R | Símbolo | Contratos | Fecha y hora apertura | Duración (s) | Comisión | Resultado anterior (ticks) | Racha | Análisis LLM | [OrderId, oculta]`

Formato: Resultado ($) y Resultado (ticks) verde > 0 / rojo < 0; Duración fondo rojo > 300.

### 4.4 Observaciones demonio (reglas en `reglas.yaml`, nada en código)
- `SIN SL`, `SL CANCELADO CON POSICIÓN ABIERTA`, `CONTRATOS > PERMITIDOS`, `[SIN TELEMETRÍA]`.
- Modificaciones de SL/TP en vivo.
- `ESTRATEGIA ELEGIDA CON LA OPERACIÓN ABIERTA`: la estrategia de la preoperativa se eligió después de la apertura de la decisión (§7.2). Es de análisis: no pone la celda en rojo.
- Duración: < 10 s `MICRO-RUIDO`; 10–60 s `CORTA`; 60–300 s `CORRECTA`; > 300 s `LARGA` + causa: `AGUANTANDO PÉRDIDA` (MAE > 50 % del SL), `DEJANDO CORRER` (MFE cerca del TP), si no `SIN GESTIÓN`.

### 4.5 Cortafuegos (decisión del operador, 22/09/2026; reglas en `reglas.yaml` → `cortafuegos`)

- **Aviso en vivo** (**apagado desde el 11/11/2026, por orden del operador: ni sonido ni notificación de Windows**; `cortafuegos.aviso.sonido` vacío y `notificacion: false` en `reglas.yaml`. El evento sigue registrándose igual, en `avisos_emitidos` y con su línea `[CORTAFUEGOS]` en el log; donde esta spec dice «suena» o «notifica», desde esa fecha solo queda eso y el sheet): lo lanza el ciclo del demonio (cada `intervalo_s`, 2 s) en cuanto el evento aparece en el log, no al sincronizar el sheet. Una sola vez por evento (`avisos_emitidos` en SQLite); lo anterior a `aviso.max_antiguedad_s` (arranque del demonio, reingesta) se registra sin sonar. Sin penalización de margen.
  - **Solo para cuentas reales** (decisión del operador, 30/09/2026). Suena y notifica si en la decisión (líder o seguidoras) hay **alguna cuenta real**; hoy son reales las Apex PA. Una decisión formada solo por pruebas no suena ni notifica en ninguna alerta, y solo deja las etiquetas en Observaciones demonio. La real manda siempre sobre la prueba. Las etiquetas y `cortafuegos_saltados` no cambian.
    - (concretado en la implementación, 30/09/2026) El evento de una decisión sin cuenta real se guarda igual en `avisos_emitidos`, con `notificado = 0`, para que no suene más tarde. Una sim activada para pruebas tampoco es real. Las infracciones `OPERACIÓN TRAS OBJETIVO` y `OPERACIÓN TRAS PÉRDIDA DIARIA` (§9.5, §9.7) son eventos de la operación cerrada, como las de entrada: se registran al cerrar, no al abrir.
- **Infracciones de entrada:** `CONTRATOS > PERMITIDOS`; `SIN SL` (a los 5 s del fill de entrada no hay SL); `SL CANCELADO CON POSICIÓN ABIERTA` (5 s de reposición).
- **Preventivos al cerrar** (decisiones globales de todas las cuentas; la copia no cuenta; la entrada escalonada sí; ventanas por hora de apertura):
  - Pérdida (≤ −10 $ netos) → `PÉRDIDA: NO OPERAR HASTA hh:mm:ss` = cierre + 612 s. Abrir antes → `REENTRADA TRAS PÉRDIDA`. **Desde la sesión CME del 08/10/2026 la pérdida se define como en §10.4** (cuenta que mide: −10 $ netos en una real, −20 t por contrato en una prueba) y hay un bloqueo nuevo por pérdidas seguidas.
  - 2 decisiones en 10 min → `2 DECISIONES EN 10 MIN: NO OPERAR HASTA` (más antigua + 10 min). La 3ª antes → `SOBREOPERACIÓN 10 MIN`. Antes del 01/10/2026 el texto era `2 OPS EN 10 MIN`.
  - 4 decisiones en 60 min → `4 DECISIONES EN 1 H: NO OPERAR HASTA` (más antigua + 60 min). La 5ª antes → `SOBREOPERACIÓN 1 H`. Antes del 01/10/2026 el texto era `4 OPS EN 1 H`.
  - Varios a la vez → se muestran todos; el aviso en vivo da la hora más tardía.
- **Sheet:** etiquetas en MAYÚSCULAS en Observaciones demonio, solo en filas de operaciones (el BAL no las lleva); con al menos una, la celda va en rojo (el mismo `colores.rojo` que Duración) con letra negra. Las de análisis (duración, MAE/MFE manual, `[SIN TELEMETRÍA]`) no ponen rojo.
- **SQLite:** `cortafuegos_saltados` (`id`, `decision_id`, `cuenta`, `tipo`, `hora_evento`, `hora_permitida`, `detalle`), una fila por infracción (no por preventivo), idempotente en reingestas, para análisis posterior. Las infracciones de una seguidora cuelgan de la decisión de su líder, y solo `CONTRATOS > PERMITIDOS` es propio de cada cuenta (cada una tiene su margen).

## 5. Margen de operativa (por cuenta, sin techo)

- Empieza en **500** en la sesión del 22/09/2026.
- Por operación: `resultado_ticks × 0,2 − |MAE_ticks|`. No se multiplica por contratos. Las perdedoras cuentan dos veces (pérdida + MAE) a propósito.
- Con MAE manual se usa el manual (también en operaciones `[SIN TELEMETRÍA]`) y la cadena de margen se recalcula entera desde esa operación (+140 diario y contratos permitidos incluidos).
- Al inicio de cada sesión hábil (L–V, festivos CME incluidos): **+140** si la sesión hábil anterior tuvo consumo neto de margen ≥ 0 o no hubo operaciones. Sábados y domingos no suman. **Desde la sesión CME del 08/10/2026 el bono es el de §10.2** (+140 sin operaciones; con operaciones, +70 si el neto del día fue ≥ +20 t de NQ y +0 si menos).
- Contratos permitidos (igual para NQ y MNQ): ≥ 750 → 3; 500–749 → 2; 141–499 → 1; < 141 → 0. **Desde la sesión CME del 08/10/2026, 141 o menos → 0 (§10.2).**

## 6. Threshold (nivel de liquidación)

- `threshold = máx(manual, mín(tope, máx(ancla previa, pico de equity en vivo − drawdown)))`. Nunca baja. Pico de equity = balance + flotante máximo observado en el log.
- Reglas por firma:

| Firma / cuenta | Drawdown | Tope |
|---|---|---|
| Apex 25k PA | 1.000 | 25.100 |
| Apex 50k PA | 2.000 | 50.100 |
| Apex 25k evaluación (30/09/2026) | 1.000 | **26.500** (balance objetivo: en Rithmic, el threshold de la evaluación sube hasta el objetivo) |
| Apex 50k evaluación (30/09/2026) | 2.000 | **53.000** |
| E2T 25k (EOD tratado como intradía) | 1.500 | 25.100 |
| Bulenox y otras | como Apex | como Apex |

- Anclas iniciales (22/09/2026): PA-APEX-91120-15 = 25.093,06; PA-APEX-91120-16 = 24.453,03; fsern2776tcp25d152250 = balance actual − 1.500 (`ANCLA ESTIMADA`).
- `Threshold Rithmic (manual)`: columna editable del BAL. Si es mayor que el calculado, manda y pasa a ser ancla.

## 7. Pestaña V4

- Fila 1: encabezado BAL. Filas 2..N+1: una por cuenta viva (no quemada, no sim), orden por nombre. Una cuenta nueva añade una fila (el layout se recalcula, sin márgenes fijos). Desde el 08/10/2026, la fila N+2 es la fila `SEMANA` (§11.2).
- 2 filas vacías.
- **Fila(s) de preoperativa** (§7.2), justo encima del encabezado de operaciones.
- Encabezado de operaciones y operaciones, **más recientes arriba**.

### 7.1 Columnas BAL
`Cuenta | Balance final | Threshold | Threshold Rithmic (manual) | Distancia al threshold | Margen de operativa | Contratos permitidos | Nº ops (día) | Nº ganadas (día) | Resultado (día, neto) | Ratio B/R (día) | Día semana | Fecha | Últimos 2 días +50 $ | Observaciones demonio`

- Datos del día actual, se sobrescriben.
- **Balance final:** hacia delante desde el `Bal:` real del log al arrancar la V4 + PnL neto + movimientos (retiros). Aviso en Observaciones demonio si difiere > 1 $ del `Bal:` real.
  - `Bal:` real = el último con la cuenta sin posición. Si sale en el mismo ms que la vuelta a plano, solo cuenta si su línea va **después** (29/09/2026): ATAS 8 con Apex escribe un `Bal:` de transición justo antes del `Position changed … vol 0` (PA-15, 28/09 17:06:50,710: DESCUADRE falso +25,02); en ATAS 7 y con E2T el bueno va detrás. Lo mismo para el balance base de una cuenta nueva.
- **Nº ganadas:** operaciones ≥ +50 $ netos (las menores cuentan en Nº ops, no en ganadas).
- **Ratio B/R (día):** `(Σ ticks netos del día − Σ |MAE|) / media de SL del día`; operación sin SL → su SL = margen de operativa en su apertura.
- **Últimos 2 días +50 $:** una columna con las dos fechas (`15/09/2026 · 19/09/2026`), día con resultado neto ≥ +50 $ de esa cuenta. Vacía hasta que haya días válidos.
- **Observaciones demonio (BAL):** hora de última sync, `SIN REGLA`, descuadre de balance, `ANCLA ESTIMADA`.
- Formato: Margen de operativa rojo si ≤ 141 (§10.2; antes, < 141). Distancia al threshold sin color.
- **Cambios del 30/09/2026** (§8.5 y §9.7):
  - `Distancia al threshold` pasa a **ticks de NQ**: (balance − threshold) ÷ 5.
  - `Contratos permitidos` muestra el límite de cada símbolo (§8.2 en Apex PA, §9.3 en pruebas).
  - Columnas nuevas:
    - `Días +50 (30 d)`, `Inactividad Apex` y `Días +100`: solo Apex PA.
    - `Prueba`: solo pruebas.
  - Cada columna nueva queda vacía en las cuentas a las que no se aplica.
  - (concretado en la implementación, 30/09/2026) Las columnas nuevas van entre `Últimos 2 días +50 $` y `Observaciones demonio`, en este orden: `Días +50 (30 d) | Inactividad Apex | Días +100 | Prueba`. `Contratos permitidos` se escribe `1 NQ / 3 MNQ` en las reales y `2 NQ / 20 MNQ` en las pruebas; en una sim sigue siendo el número de §5. El día +50 de `Últimos 2 días +50 $` es el de §8.3 (10 t = 50 $, el mismo valor) en todas las cuentas.

### 7.2 Preoperativa (decisión del operador, 24/09/2026)

Pasos que el operador hace antes de entrar. Cada paso es una fila justo encima del encabezado de operaciones: el texto va en la columna A y el control, en la columna de operaciones a la que alimenta. Los pasos futuros se añaden en esta misma zona.

**Paso 1: estrategia a usar.**
- **Dónde:**
  - Columna A: `Estrategia a usar`.
  - Columna de `Estrategia` (hoy la I): desplegable con los nombres de `Playbook.cdb` más `Sin estrategia` (por defecto).
  - Es la única celda de la pestaña con desplegable (hasta el 07/10/2026: desde entonces hay otras cuatro, la compuerta de §10.6).
- **Solo valores de la lista:** el desplegable es estricto. "sin estrategia", escrito como sea, cuenta como el defecto.
- **Lectura:** el demonio lee la celda cada `preoperativa.lectura_s` segundos (5; en la práctica unos 6, por el ciclo de 2 s) y otra vez en cada sync. Lo que escribe en ella también cuenta como lectura.
  - La hora de cada lectura se toma **antes** de la petición.
  - La lectura periódica usa un cliente con timeout corto (`config.yaml`: `preoperativa_timeout_s`, 3 s). Tras un fallo no se reintenta hasta pasados `preoperativa_pausa_error_s` (30 s). Así una red colgada no frena los avisos del cortafuegos.
  - La lectura periódica comprueba que la columna A de esa fila sigue diciendo `Estrategia a usar`. Si alguien insertó o borró filas a mano, no lee y espera al siguiente sync.
- **Momento de elección:** es la última lectura en la que la celda tenía otro valor. Es una cota inferior, a favor del operador: solo se le acusa cuando seguro que eligió después de entrar. Si no hay lectura previa (base nueva, primer arranque), cuenta desde la primera lectura con el valor: no se reparte a lo ya cerrado y no se marca ninguna tardía, porque no se sabe cuándo eligió. Queda registrado en el log.
- **Asignación al anotar:**
  - Cada decisión que se escribe por primera vez y que cerró después del momento de elección recibe la estrategia de la celda, si no es `Sin estrategia`. Entran la entrada y sus escalonadas cerradas en la misma escritura.
  - Después la celda vuelve a `Sin estrategia`.
  - Si en esa escritura no hay decisiones nuevas, la celda se conserva.
  - Si la escritura en el sheet falla, la elección sigue armada y el siguiente sync repite la misma asignación.
- **Hora del exchange** (28/09/2026): las lecturas se guardan con el reloj del PC; al asignar, el momento de elección se pasa a hora del exchange con el reloj ATAS (§1) de esa sesión, y con esa hora se decide qué decisiones la reciben y cuáles son tardías.
- **Control:** si el momento de elección (hora del exchange) es posterior a la apertura de la decisión en más de `preoperativa.tolerancia_tardia_s` (2 s), en Observaciones demonio aparece `ESTRATEGIA ELEGIDA CON LA OPERACIÓN ABIERTA`. La tolerancia existe porque las horas de apertura pueden ir truncadas al segundo y el desfase del reloj es una estimación.
- **Límites conocidos:**
  - Una elección hecha durante el medio segundo que tarda la escritura (entre la lectura del sheet y la escritura) se pierde.
  - Con el demonio apagado, la cota inferior es la última lectura antes de apagarlo: una operación hecha antes de elegir puede recibir la estrategia sin marca.
  - Una estrategia elegida entre la última lectura y el sync se la queda la decisión que acaba de cerrar (marcada como tardía), no la siguiente.
- **Registro:** cada asignación queda en la tabla SQLite `estrategia_asignada` (ID, estrategia, momento de elección con el reloj del PC y en hora del exchange, momento de asignación y si fue tardía), aunque luego se edite la columna a mano.

## 8. Apex PA: contratos, inactividad y margen levantado (decisión del operador, 30/09/2026)

> Se implementa junto con §9. Normas de Apex: `..\NORMAS_APEX.md` (§3.4, inactividad). Reglas del plan: `..\plan trading\4-plan.md`.
> **Unidades:** los umbrales de las reglas van en **ticks de NQ** en `reglas.yaml` (importe neto en $ ÷ 5), no en $. Balances, nivel del threshold y `Resultado ($)` siguen en $ (§2).

### 8.1 Ámbito
- Solo cuentas **Apex PA** (firma Apex, tipo PA). Son las únicas **reales** hoy.
- Las pruebas (evaluaciones) siguen §9.
- La E2T real tendrá reglas propias cuando exista.

### 8.2 Contratos permitidos (en Apex PA sustituye a la tabla de §5) — sustituida por §10.1 desde la sesión CME del 08/10/2026 (NQ 0; MNQ con más de 141)
- **MNQ** según el margen: ≥ 750 → 3; 500–749 → 2; 141–499 → 1; < 141 → 0.
- **NQ:** 1 si el margen es ≥ 141; 0 por debajo.
- **Con el margen levantado (§8.4):** 1 MNQ y 0 NQ.
- Pasarse de estos límites pone `CONTRATOS > PERMITIDOS`, igual que hoy.
- La columna BAL `Contratos permitidos` muestra el límite de cada símbolo.
- (concretado en la implementación, 30/09/2026) Un símbolo sin fila propia (ES, MES…) sigue la tabla de §5, también con el margen levantado.

### 8.3 Día válido de Apex, inactividad y contadores
- **Día +50:** sesión CME en la que el neto del día (balance al cierre − balance al empezar, sin contar movimientos) es **≥ 10 ticks de NQ** (50,00 $). Sin colchón. Comisiones de Apex: MNQ 0,51 y NQ 1,99 por lado (§2).
- **Contador de inactividad:** días naturales entre el más antiguo de los dos últimos días +50 y la sesión en curso (fecha de sesión). **Límite** = ese día + 30.
- **Estado:**
  - `OK`: contador < 15.
  - `DORMANT`: contador ≥ 15.
  - `MARGEN LEVANTADO`: contador ≥ 20 y margen < 141.
- **Días +50 (30 d):** días +50 con fecha de sesión dentro de los últimos 30 días naturales, contando la actual.
- **Días +100:** días con un neto igual o mayor que el umbral de su tamaño (25K: 20 t = 100 $; 50K: 40 t = 200 $; 100K: 50 t = 250 $; 150K: 60 t = 300 $) desde el último `retiro` registrado, o desde el alta si no hay ninguno. Sin fechas.
- **Semillas** (desde la V3, con días anteriores al 22/09; sin columna manual) en **`semillas.yaml`**. El demonio solo lo lee: `cuentas.yaml` lo reescribe el demonio al dar de alta cuentas, y una semilla escrita allí se perdería.
  - PA-15: últimos días +50, 17/09 y 21/09; 8 días +100.
  - PA-16: últimos días +50, 11/09 y 21/09; 2 días +100.
- (concretado en la implementación, 30/09/2026)
  - Con menos de dos días +50, el contador cuenta desde el que haya; sin ninguno, desde el alta de la cuenta.
  - En el BAL, la sesión en curso ya cuenta como día +50 si su neto hasta ahora es ≥ 10 t, igual que en `Últimos 2 días +50 $`. Para el margen levantado de una operación solo cuentan los días anteriores a su sesión; el de hoy entra con la parada (§8.4).
  - "Desde el último retiro" = sesiones posteriores a la del retiro; la sesión del retiro no cuenta. Los días +100 de la semilla solo cuentan mientras no haya ningún retiro.
  - Una semilla con fecha igual o posterior al inicio de la V4 (22/09) se ignora.

### 8.4 Margen levantado
- **Se activa** con el contador ≥ 20 y el margen < 141. Permite operar **1 MNQ** aunque el margen no lo autorice.
- **Parada del día:** con el margen levantado, cuando la cuenta queda plana con el neto del día ≥ +10 t, la operación que lo consigue lleva en Observaciones demonio `+50 DEL DÍA ALCANZADO: NO OPERAR MÁS HOY`. Es una preventiva, así que pone la celda en rojo, pero sin aviso sonoro.
  - Desde ese momento vuelve el bloqueo por margen: operar más ese día es `CONTRATOS > PERMITIDOS`.
  - La cuenta vuelve a levantarse cuando el contador, recalculado con el nuevo día +50, llega otra vez a 20 con el margen < 141.
- **Margen con la regla levantada:**
  - En un **día +50**, cada operación aporta `máx(0, ticks × 0,2 − |MAE|)`, de modo que nunca resta, y la sesión siguiente recibe **+140**. Es la lectura literal de "suma lo máximo"; si hay varias operaciones, confirmar en el grill de implementación si es por operación o por día.
  - En un día que no llega a +50, el margen funciona normal: resta, y el +140 solo entra si el consumo fue ≥ 0 o no se operó.
  - (concretado en la implementación, 30/09/2026)
    - "Suma lo máximo" va **por operación** (instrucción del 30/09/2026): cada operación de ese día aporta `máx(0, aportación)`, así que el consumo del día es ≥ 0 y la sesión siguiente recibe el +140.
    - El día es "con el margen levantado" si lo estaba **al empezar la sesión** (contador ≥ 20 y margen < 141 después del +140). Si se decidiera operación a operación, el día dependería de sus propias aportaciones y no tendría una solución única. Si el margen cae por debajo de 141 a mitad de un día normal, la operación siguiente puede abrir con 1 MNQ, pero ese día no suma lo máximo.
    - La parada salta al quedar plana con el neto del día ≥ +10 t si alguna de las operaciones que cierran en ese momento se abrió con el margen levantado. La etiqueta la lleva la última que cierra, una sola vez por día.
    - En el BAL, con la parada ya hecha hoy, el estado no es `MARGEN LEVANTADO`: sale `DORMANT` u `OK` según el contador.
- **Con el margen normal** (≥ 141) se aplica §5 sin cambios: un día +50 no para nada ni cambia el cálculo.
- **Sin suelo de margen.**

### 8.5 Vista
- **Columnas nuevas en el BAL**, rellenas solo en las Apex PA:
  - `Días +50 (30 d)`.
  - `Inactividad Apex`, con el formato `día N · límite dd/mm · ESTADO`.
  - `Días +100`.
- La columna existente `Últimos 2 días +50 $` usa las semillas de §8.3.
- Sin sonido ni notificación: el estado solo se ve en el BAL.

### 8.6 Fuera de la V4
- Los límites que el operador tiene en Rithmic (Loss Limit 175, Profit Limit 375, Limited Trailing Minimum Account Balance 200 activado a mano a partir de threshold + 800) están en `4-plan.md` §7. La V4 no los vigila.

## 9. Pruebas (evaluaciones) (decisión del operador, 30/09/2026)

> Objetivo: en las pruebas, **el margen informa pero no sanciona**. Normas de las firmas: `..\NORMAS_APEX.md` y la ayuda oficial de E2T (TCP25: objetivo 1.750 $, drawdown EOD de 1.500 $, límite diario de 550 $, máximo 3 contratos, consistencia del 30 %). Reglas del plan: `..\plan trading\4-plan.md`.
> **Unidades:** como en §8, los umbrales van en ticks de NQ; balances y niveles, en $.

### 9.1 Ámbito
- **Prueba** = cuenta viva, no sim y no real. Hoy:
  - Evaluaciones Apex Intraday 25K `APEX-91120-76` a `-85`.
  - La E2T TCP25 `fsern2776tcp25d152250`.
- `APEX-91120-74` y `-75` se queman (orden del operador, 30/09/2026).
- Las reglas dependen de la firma: **Apex** (§9.5 objetivo, §6 threshold) o **E2T** (§9.5 objetivo con consistencia, §9.6 máximo diario).
- Una prueba de otra firma (Bulenox) recibe las reglas generales (§9.2–§9.4), sin objetivo ni máximo diario. En la columna `Prueba` aparece `SIN REGLA`.
- (concretado en la implementación, 30/09/2026) Que la prueba esté viva solo decide si tiene fila BAL. Una prueba quemada conserva sus reglas en las operaciones que ya tiene; si no, quemarla cambiaría etiquetas e infracciones hacia atrás.

### 9.2 Margen informativo
- Mismo cálculo que en §5: 500 inicial, `ticks × 0,2 − |MAE|`, +140. Mismo color (rojo por debajo de 141).
- **Nunca limita los contratos:** en una prueba no existe `CONTRATOS > PERMITIDOS` por falta de margen. Tampoco se aplica el margen levantado de §8.

### 9.3 Contratos
- **Máximo 2 NQ-equivalentes** en la posición abierta de la cuenta. NQ cuenta 1 y MNQ cuenta 0,1 (10 MNQ = 1 NQ), sumando ambos símbolos si están abiertos a la vez. En resumen: 2 NQ o 20 MNQ. **En las pruebas de 25K, desde la sesión CME del 08/10/2026, manda la distancia al threshold (§10.3).**
- Pasarse → `CONTRATOS > PERMITIDOS`, con el detalle en NQ-equivalentes.
- El tamaño previsto es 1 NQ, pero operar hasta 2 no es infracción.
- `Contratos permitidos` (BAL) muestra `2 NQ / 20 MNQ`, sin tener en cuenta el margen.
- Apex permite 4 contratos en la 25K; el límite de 2 es del plan.
- (concretado en la implementación, 30/09/2026) Se mide la posición de la cuenta, en todos sus símbolos, justo después de la entrada de cada operación, con lo que sale en ese mismo instante ya descontado. Un símbolo sin peso (ES…) no suma. Detalle: `2,1 NQ-eq con 2 permitidos`.

### 9.4 Cortafuegos y avisos
- Los mismos cortafuegos de §4.5, con las mismas ventanas globales: las decisiones de pruebas y de reales cuentan juntas.
- **Sin sonido ni notificación** en ninguna alerta de pruebas (§4.5): solo Observaciones demonio. Si una real lidera y hay pruebas de seguidoras, **suena**.
- Una seguidora que es prueba lleva sus propias reglas: su límite de contratos es el de §9.3.

### 9.5 Objetivo
- **Balance objetivo:**
  - Apex 25K: 26.500. Apex 50K: 53.000.
  - E2T TCP25: 26.750 (25.000 + 1.750). En E2T, el **objetivo efectivo** = 25.000 + máx(1.750, mejor día ÷ 0,3), donde "mejor día" es el mejor neto diario desde que empezó la prueba (§9.6).
  - **Semilla E2T** (`semillas.yaml`, desde la V3): la prueba `fsern2776tcp25d152250` empezó el 21/09/2026 con 25.000, y el único día anterior al 22/09 dio +280,76 $ (56 t). Si la prueba se resetea, el operador lo indica y el recuento empieza de cero.
- **Objetivo alcanzado:** cuando la cuenta queda plana con el balance ≥ objetivo (efectivo en E2T), la operación lleva `OBJETIVO ALCANZADO: NO OPERAR MÁS`. Es preventiva: celda en rojo, sin sonido.
  - Cualquier operación posterior en esa cuenta → **infracción** `OPERACIÓN TRAS OBJETIVO`, que va a `cortafuegos_saltados`.
  - Sacarla del grupo de copia es tarea del operador.
- **Threshold de las evaluaciones Apex:** tope = balance objetivo (§6). E2T, sin cambios.
- (concretado en la implementación, 30/09/2026)
  - El "mejor día" incluye el neto del día en curso en el momento de quedar plana. El balance inicial de la prueba es el de la semilla o, sin ella, el tamaño.
  - `OBJETIVO ALCANZADO` se marca solo la primera vez. Desde entonces, toda operación abierta después en esa cuenta es infracción, hasta quemarla.

### 9.6 Máximo diario de E2T (consistencia del 30 %)
- **Máximo BAL del día** = balance al empezar la sesión + **105 t** (525 $).
- Cuando la cuenta queda plana con el balance por encima de ese máximo, la operación lleva `MÁXIMO BAL DE HOY: <nivel $> · REDUCIR BAL PARA CUMPLIR LA REGLA DEL 30 %`. Ejemplo: empieza en 25.500 y cierra en 26.040 → `MÁXIMO BAL DE HOY: 26.025 · …`.
- Es un aviso sin sonido. No sanciona: no es infracción.
- Si el día cierra por encima del máximo, el objetivo efectivo sube (§9.5). E2T incumple con "30 % o más": con un total de 1.750 justos, un día de 525 justos ya falla.
- (concretado en la implementación, 30/09/2026) "Por encima" es estricto: un balance igual al máximo no lleva la etiqueta. Sale en cada vuelta a plana por encima del máximo, no solo la primera. Pone la celda en rojo, como el resto de etiquetas en mayúsculas de §4.5.

### 9.7 Pérdida diaria (pruebas)
- **El cierre forzoso lo hace Rithmic** (Loss Limit 375). La V4 no puede cerrar posiciones: solo anota.
- Cuando la cuenta queda plana con el neto del día ≤ **−75 t** (−375 $, con comisiones), la operación lleva `PÉRDIDA DIARIA −75 T: NO OPERAR MÁS HOY`. Es preventiva: celda en rojo, sin sonido.
- Cualquier operación posterior en esa cuenta ese día → **infracción** `OPERACIÓN TRAS PÉRDIDA DIARIA`, que va a `cortafuegos_saltados`. Puede pasar porque Rithmic corta en bruto y la V4 cuenta en neto.
- Solo pruebas. En las Apex PA la V4 no vigila los límites de Rithmic (§8.6).
- (concretado en la implementación, 30/09/2026) La etiqueta sale la primera vez del día. Son infracción las operaciones abiertas después en esa misma sesión.

### 9.8 Vista
- Columna nueva `Prueba` en el BAL, rellena solo en pruebas:
  - Apex: `objetivo 26.500 · faltan N t`.
  - E2T: `objetivo <efectivo> · faltan N t · mejor día M t (máx 105)`.
  - Con el objetivo ya alcanzado: `OBJETIVO ALCANZADO`.
- Las columnas de §8.5 quedan vacías en las pruebas.
- (concretado en la implementación, 30/09/2026) `faltan` y `mejor día` van en ticks con un decimal; `faltan` nunca es negativo. El mejor día incluye el neto de hoy.

### 9.9 Fuera de la V4
- **Rithmic en pruebas:** Loss Limit 375, **sin** Profit Limit, y LTMAB 200 activado a mano **solo cuando el día ya lleva +40 t** sobre el balance inicial. Así nunca actúa como stop de pérdida. Detalle en `4-plan.md`.
- **Fin de una prueba** (aprobada, caducada o suspendida): `cli_v4.py quemar`, a mano.
- No se muestra la caducidad de la evaluación Apex.

### 9.10 Tope del SL por el Loss Limit de Rithmic (decisión del operador, 07/10/2026)
- **Por qué:** en las pruebas el SL de la orden se pone lejos (−999) porque es un BE: se aguanta hasta el nivel del plan (desde el Plan 4, §10.3: BE+1 en +40 t en las de 25K de Apex, +60 en la E2T y +80 en las de 50K, con trailing; antes, +80 en todas) y desde ahí sube tick a tick. El stop real hasta el BE es el Loss Limit de Rithmic (375 $). Con 999 en el denominador, el ratio y las métricas que usan el SL no dicen nada.
- **Ámbito:** solo pruebas (§9.1), también seguidoras, y solo operaciones de la sesión CME del **07/10/2026 en adelante** (`desde` en `reglas.yaml`). Las anteriores conservan su SL real (999 incluido). Las Apex PA no cambian.
- **Tope (ticks por contrato) = `limite_usd` ÷ (contratos de la fila × valor del tick del símbolo).** `limite_usd` = 375. Con 1 NQ, 75 t; con 2 NQ, 37,5 t; con 1 MNQ, 750 t. No depende del neto del día (decisión del operador: es una aproximación, no el riesgo exacto). **Desde la sesión CME del 08/10/2026 (§10.3) el tope es mín(`limite_usd`, distancia al threshold en $ en la apertura) ÷ (contratos × valor del tick)**: con 60 t de distancia, 60 t con 1 NQ.
- **SL que cuenta:** cada SL registrado en riesgo (ticks > 0) se sustituye por `mín(SL, tope)`. Los SL en beneficio (≤ 0 t de riesgo) no se tocan. Esto se aplica a todo lo que usa el SL: la columna `SL (ticks)` (muestra solo el tope, p. ej. `75`; el SL de la orden queda en SQLite), el `Ratio B/R` de la operación (§4.2, con el SL más amplio ya topado), el `Ratio B/R (día)` del BAL (§7.1) y la causa `LARGA · AGUANTANDO PÉRDIDA` de §4.4 (MAE > 50 % del SL que cuenta). Consecuencia aceptada: con el tope, `AGUANTANDO PÉRDIDA` puede salir donde con 999 no salía, y cambia la Calificación (§4.3).
- **Nota de SL en Observaciones demonio:** en estas operaciones no se anotan los movimientos del SL mientras está en riesgo (ni el salto del −999 al BE). La nota empieza en el primer SL a **+1 o mejor** (ticks de beneficio asegurado) y sigue sus cambios, sin hora: `SL BE +1→+12→+30` (prefijo en `reglas.yaml`). Con un solo nivel en beneficio: `SL BE +1`. Si el SL nunca llega a +1, no hay nota de SL. La nota de TP no cambia.
- Sin telemetría o sin SL: no cambia nada (`SIN SL` y el denominador por margen siguen como en §4.2).
- (concretado en la implementación, 07/10/2026)
  - **Por fila:** cada fila (también cada entrada de una escalonada) usa sus propios contratos. El tope se redondea a 2 decimales, como los ticks (7 NQ: 10,71 t). Cuenta la sesión CME de la **apertura** de la operación.
  - **Seguidoras:** heredan el SL de la líder (§4.1) y el tope se calcula después, con sus contratos y según su tipo de cuenta. Una prueba que sigue a una real queda topada y la líder, no. Una real que sigue a una prueba conserva el SL de la líder, y la líder queda topada.
  - **Una prueba quemada** sigue en el ámbito (§9.1): quemarla no cambia lo calculado hacia atrás.
  - **SQLite:** `operaciones.datos` lleva `sl_ini` y `sl_max` ya topados. Las filas topadas (prueba, sesión desde `desde`, con algún SL) llevan además `sl_ini_orden`, `sl_max_orden` (el SL inicial y el más amplio de la orden) y `sl_tope` (el tope aplicado). Las demás no llevan esos campos. Sin migración: la foto se reescribe entera en cada sync.
  - **Nota de SL:**
    - Los SL en riesgo se ignoran en toda la operación, también después de empezar la nota.
    - Tampoco se anotan los SL sin riesgo anteriores al primero que asegura ≥ +1 t (el BE exacto, p. ej.); ese +1 es `beneficio_min_ticks`.
    - Desde el primero se anotan todos los niveles sin riesgo, sin repetir niveles consecutivos.
    - Prefijo, separador y el +1 están en `reglas.yaml`.
  - Lo demás sale igual: `SIN SL`, `SL CANCELADO CON POSICIÓN ABIERTA`, cortafuegos, margen, MAE/MFE y resultado. Cambian `Ratio B/R`, `Ratio B/R (día)`, `LARGA · AGUANTANDO PÉRDIDA` y, por él, la Calificación.
  - El tope solo entra en el cálculo desde la sesión `desde`; las operaciones anteriores se reescriben en cada sync con su SL real.

## 10. Plan 4 (grill del operador, 07/10/2026) — IMPLEMENTADA (07/10/2026; sin verificación independiente todavía)

> Origen: grill del Plan de trading 4 (`..\plan trading\4-plan.md`). Este apartado **modifica** §4.5, §5, §7.2, §8 y §9 donde se indica; si algo choca, manda §10.
> **Unidades:** como en §8 y §9. Umbrales de cuenta en ticks de NQ (neto ÷ 5); resultados y disparos de una operación en ticks por contrato.
### 10.0 Fecha de entrada en vigor (decisión del operador, 07/10/2026)
- Las reglas nuevas rigen **solo desde la sesión CME del 08/10/2026**: una sola fecha, `plan4.desde` en `reglas.yaml` (la misma del bono de sesión de §10.2).
- Decide **la sesión CME de la apertura** de cada decisión u operación (una operación que cruza el cambio de sesión cuenta en la que abrió) para:
  - §10.1: contratos de las reales (NQ 0, 141 o menos → 0) y margen levantado hasta 141;
  - §10.2: 141 o menos → 0 en la tabla general, y el bono de sesión (por la sesión que empieza);
  - §10.3: contratos de las pruebas de 25K por la distancia y tope del SL con la distancia;
  - §10.4: pérdida con la cuenta que mide (−10 $ / −20 t), pausa de 612 s con esa pérdida y bloqueo por pérdidas seguidas (solo cuentan las decisiones abiertas desde esa sesión);
  - §10.6: `COMPUERTA SIN SÍ`.
- Lo anterior conserva **exactamente** lo que daban las reglas de antes. `plan4.antes` en `reglas.yaml` guarda las tablas de §5 y §8.2 y el margen levantado con `< 141`; la pérdida de antes es el neto de la líder ≤ −10 $.
- **No dependen de la fecha:** `ESPEJO` (§10.5: es de análisis y se marca también hacia atrás), la columna BAL `Contratos permitidos` y el rojo del margen ≤ 141 (son el estado actual y usan siempre las reglas nuevas), la comisión de E2T NQ (2,04 en toda la historia, §2) y el patrón de la E2T real.

### 10.1 Cuentas reales: Apex PA y E2T real
- **E2T real** pasa a ser cuenta real (`cuentas_reales`). Patrón provisional: nombre que empieza por `livesim` (la última E2T real fue `livesimT_franeznaco50e`) → firma E2T, tipo real, tamaño inferido. El operador lo confirma cuando exista una.
- La E2T real **copia todo §8** (contratos, día +50, inactividad, margen levantado, días +100, columnas del BAL). Decisión del operador: "copia todo".
  - **Excepción (orden del operador, 11/11/2026): la columna `Inactividad Apex` de la E2T real muestra la norma de E2T**, no la de Apex. E2T cierra la financiada tras **5 días de trading seguidos** de ausencia no avisada (`..\NORMAS_E2T.md` §2, fuente [S21], releída el 11/11/2026: «unscheduled absence of five consecutive trading days»; sigue estando solo en el blog oficial).
    - Cuenta **cualquier operación**, gane o pierda (no hace falta un día +50).
    - Contador = días hábiles (L–V; los festivos CME cuentan como hábiles, por prudencia) desde la última sesión CME con alguna operación de la cuenta, hasta la sesión en curso incluida. Con una operación hoy vale 0. Sin ninguna operación, cuenta desde el alta (el día del alta es el 1).
    - Límite = el hábil nº 5 tras la última operación: el último día en que operar evita los 5 días seguidos de ausencia.
    - Texto: `día N de 5 sin operar · operar como tarde el dd/mm · ESTADO`, con `OK`, `AVISO` (N ≥ 3), `ÚLTIMO DÍA` (N = 5) y `VENCIDO` (N > 5). Todo en `reglas.yaml` → `inactividad_firma.E2T`.
    - Solo cambia ese texto. El margen levantado (§8.4) de la E2T real sigue usando el contador de §8.3 (días naturales desde los días +50), como hasta ahora. Las ausencias avisadas a E2T no se registran: el demonio no las conoce.
- **Contratos en las reales (sustituye a §8.2):**
  - **NQ: 0 siempre.** Las reales se operan solo en MNQ.
  - **MNQ** según el margen: ≥ 750 → 3; 500–749 → 2; **más de 141** y < 500 → 1; **≤ 141 → 0**.
  - Margen levantado (§8.4): 1 MNQ, con la condición **margen ≤ 141** (antes < 141).
  - BAL `Contratos permitidos`: `0 NQ / N MNQ`.
- (concretado en la implementación, 07/10/2026)
  - **Patrón y cuentas reales:** `patrones_cuenta` → `^livesim` (firma E2T, tipo `real`, tamaño `null`: se infiere del primer `Bal:` plano) y `cuentas_reales` → `{firma: E2T, tipo: real}`. Con `firmas.E2T` solo en 25K, una E2T real de 50K se daría de alta como de 25.000 (drawdown 1.500, tope 25.100) hasta que el operador dé su tabla; el tamaño de la ficha se corrige en `cuentas.yaml` (con el demonio parado).
  - **Tabla de contratos de las reales** (`apex_pa.contratos`): NQ → 0 siempre (`min: -.inf`) y MNQ → 3 / 2 / 1 con `mayor_que: 141`. Los umbrales de una tabla aceptan `min` (≥) o `mayor_que` (>). Un símbolo sin tabla propia (ES, MES…) sigue la tabla general de §5, que desde §10.2 también es `mayor_que: 141`.
  - **Margen levantado:** `apex_pa.margen_levantado.margen_max` (141, incluido).
  - **Fecha:** solo desde `plan4.desde` (§10.0), por la sesión de la apertura. Antes se conserva la tabla de §8.2 (`plan4.antes.apex_pa_contratos`: 141 daba 1, NQ incluido) y el margen levantado con `< 141`. La columna BAL `Contratos permitidos` es el estado actual y usa siempre la tabla nueva.

### 10.2 Margen de operativa (modifica §5; todas las cuentas)
- **≤ 141 → 0 contratos** también en la tabla general de §5 (sim y símbolos sin tabla). Rojo del margen: **≤ 141**.
- **Bono de sesión nuevo** (desde la sesión CME `desde` de `reglas.yaml`, la del **08/10/2026**; las sesiones anteriores conservan la regla de §5, sin recalcular hacia atrás). Al empezar cada sesión hábil, según la sesión hábil anterior **de esa cuenta**:
  - sin operaciones → **+140**;
  - con operaciones y neto del día **≥ +20 t de NQ** (100 $) → **+70**;
  - con operaciones y neto del día < +20 t → **+0**.
  - Esto va **aparte** del cálculo por operación (`ticks × 0,2 − |MAE|`), que no cambia.
- El margen levantado (§8.4) no cambia: un día +50 con el margen levantado sigue sumando lo máximo y la sesión siguiente recibe +140.
- Pruebas: el margen sigue siendo informativo (§9.2).
- (concretado en la implementación, 07/10/2026)
  - **Neto del día de la sesión hábil anterior** = suma del `Resultado ($)` neto de las filas de esa cuenta (líder o copia) abiertas en esa sesión, comparada al céntimo con `margen.bono.neto_ticks` × 5 $ = 100,00 $ (99,99 → +0; 100,00 → +70). "Con operaciones" = al menos una fila abierta en esa sesión.
  - **Fecha:** el bono nuevo rige desde la sesión que empieza en `plan4.desde` (la del 08/10/2026, que mira la sesión hábil anterior); las anteriores conservan la regla de §5 (`margen.bono_sesion`: +140 con consumo ≥ 0 o sin operar). La tabla general (141 o menos → 0) rige por la sesión de la apertura; lo anterior usa `plan4.antes.margen_contratos`.
  - **Margen levantado:** si la sesión hábil anterior fue un día +50 con el margen levantado ("suma lo máximo", §8.4), la siguiente recibe `apex_pa.margen_levantado.bono` (+140), no el +70 / +0. Un día con el margen levantado que no llega a +50 usa la regla nueva.
  - **Rojo:** `colores.margen_rojo_hasta` (141) con la condición `NUMBER_LESS_THAN_EQ` del formato condicional del BAL.
  - Las pruebas y las sim usan el mismo bono (en las pruebas el margen sigue siendo informativo, §9.2).

### 10.3 Pruebas: contratos por distancia al threshold (sustituye a §9.3 en las pruebas de 25K)
- En la apertura de cada operación se mira la **distancia al threshold** de la cuenta (balance − threshold, en ticks de NQ, la de la columna BAL; con la posición abierta de esa cuenta ya descontada si la hubiera, el balance es el último plano).
  - Distancia **≥ 75 t** → como máximo **1 contrato**: 1 NQ o 1 MNQ.
  - Distancia **< 75 t** → como máximo **1 MNQ** (0 NQ).
  - Pasarse → `CONTRATOS > PERMITIDOS`, con el detalle `<N> <símbolo> con distancia <D> t`.
- Pruebas de 50K: siguen con el máximo de 2 NQ-equivalentes de §9.3 (sin grill propio todavía).
- BAL `Contratos permitidos` en las pruebas de 25K: `1 NQ / 1 MNQ` o `0 NQ / 1 MNQ` según la distancia actual.
- **Tope del SL (§9.10):** tope = **mín(`limite_usd`, distancia al threshold en $ en la apertura)** ÷ (contratos × valor del tick). Motivo (grill): con distancia < 375 $, el riesgo real es la distancia, porque tocar el threshold quema la cuenta. Si la distancia es ≤ 0 o no se conoce, se usa `limite_usd`.
- Texto de §9.10: el BE de las pruebas ya no es "desde +80". Plan 4: BE+1 en +40 (Apex 25K y E2T usa +60) o +80 (50K), con trailing. La V4 no lo vigila; solo cambia la explicación.
- (concretado en la implementación, 07/10/2026)
  - **Distancia en la apertura:** (balance − threshold) ÷ 5 en el instante de la primera entrada de la fila (hora del exchange), redondeada a 2 decimales. El balance es el último plano: los cierres de la cuenta hasta ese instante, sin la posición abierta. El threshold es la fórmula de §6 con el pico de equity hasta ese instante (el mayor entre el balance tras cada cierre y los `Bal:` del log), partiendo del ancla inicial (la de la ficha o, si no, balance base − drawdown) y con el tope de la prueba (el objetivo en las evaluaciones Apex). **No incluye el threshold manual** de la columna del BAL (no se sabe desde cuándo manda), así que la distancia de la columna del BAL puede diferir.
  - **Posición:** la de la cuenta justo tras la entrada, sumando los símbolos de la tabla (NQ y MNQ; un símbolo sin fila no cuenta). Con distancia ≥ 75 t (`lejos`): hasta 1 contrato en total entre los dos (`max_total`). Con menos (`cerca`): 0 NQ y 1 MNQ. Una distancia desconocida (sin `Bal:` base o sin regla de threshold) cuenta como `lejos`. Detalle: `{posicion} con distancia {D} t`, con la posición como en el BAL (`2 NQ`, `1 NQ / 1 MNQ`) y D con un decimal.
  - **Tamaños:** `pruebas.contratos.por_distancia.tamanos` = [25000]. Las demás pruebas (50K, Bulenox) siguen con §9.3. La distancia para el tope del SL se calcula en todas las pruebas.
  - **Tope del SL:** mín(`limite_usd`, distancia en $) ÷ (contratos × valor del tick), con la distancia en $ = distancia en t × 5. Con distancia ≤ 0 o desconocida, `limite_usd`. Cada fila usa la distancia de su cuenta (una seguidora, la suya). El resto de §9.10 no cambia.
  - **Columna BAL:** `Contratos permitidos` de una prueba de 25K es `1 NQ / 1 MNQ` (distancia actual ≥ 75) o `0 NQ / 1 MNQ`.
  - **Fecha:** los contratos por la distancia y el tope del SL con la distancia valen para las operaciones abiertas desde `plan4.desde` (§10.0); las anteriores siguen con §9.3 (NQ-equivalentes) y con el tope de `limite_usd` de §9.10.

### 10.4 Pérdida de una decisión y bloqueo por pérdidas seguidas (modifica §4.5)
- **Cuenta que mide** una decisión: si en la decisión (líder o seguidoras) hay alguna real, **la real** (la líder si es real; si no, la primera real por nombre). Si no hay ninguna, la líder.
- **Decisión perdedora:**
  - la cuenta que mide es real → su neto ≤ **−10 $** (hoy `racha.perdedora_max`);
  - es una prueba → su `Resultado (ticks)` ≤ **−20 t** por contrato.
- **Decisión ganadora** (solo para la racha de abajo): `Resultado (ticks)` de la cuenta que mide ≥ **+10 t**.
- La pausa de **612 s** de §4.5 usa esta definición de pérdida (antes: −10 $ netos siempre).
- **Bloqueo por pérdidas seguidas (nuevo):**
  - Racha **global** (todas las cuentas, por decisión, la copia no cuenta), dentro de una sesión CME, con las decisiones en orden de **cierre**.
  - Una perdedora suma 1. Una **ganadora** pone la racha a 0. Una neutra (ni perdedora ni ganadora) **no suma ni rompe**. El cambio de sesión CME la pone a 0.
  - Cuando una decisión perdedora deja la racha en **≥ 2**: preventivo `<N> PÉRDIDAS SEGUIDAS: NO OPERAR HASTA hh:mm:ss` = cierre + **3.600 s**. Con la racha en 3, 4… cada nueva perdedora da otra hora desde su cierre.
  - Abrir una decisión antes → infracción **`REENTRADA TRAS PÉRDIDAS SEGUIDAS`**: va a `cortafuegos_saltados`, pone la celda en rojo, suena con las reglas de §4.5 y cuenta como **proceso incorrecto** en la Calificación.
  - Varios preventivos a la vez: como en §4.5 (se muestran todos; el aviso en vivo da la hora más tardía).
- **Se elimina** de la documentación la omisión "fin de sesión en la 9ª decisión" (la V4 nunca la comprobó).
- (concretado en la implementación, 07/10/2026)
  - **Cuenta que mide:** entre las filas ya **cerradas** de la decisión (si la seguidora real aún no ha cerrado, mide la líder y se corrige en cuanto cierre).
  - **Sim activada** (ni real ni prueba, §9.1): conserva el criterio de siempre, neto ≤ −10 $.
  - **Umbrales** (`reglas.yaml`): perdedora = `racha.perdedora_max` (real) o `cortafuegos.perdida.prueba_ticks` (−20, prueba); ganadora = `cortafuegos.seguidas.ganadora_ticks` (+10) sobre el `Resultado (ticks)` redondeado a 2 decimales (−20,00 es perdedora, −19,75 no; +9,75 es neutra).
  - **Sesión de la racha:** la del **cierre** de cada decisión. La infracción solo existe si la decisión que abre es de la misma sesión CME que el bloqueo: a las 17:00 CT la racha vuelve a 0 y el bloqueo deja de valer.
  - **Textos:** `{n} PÉRDIDAS SEGUIDAS: NO OPERAR HASTA {hora}` y la infracción `REENTRADA TRAS PÉRDIDAS SEGUIDAS` en `cortafuegos.seguidas`; entra en `calificacion.proceso_incorrecto.infracciones` (la seguidora no la hereda, como `REENTRADA TRAS PÉRDIDA`, §4.2).
  - **Fecha:** la pérdida con la cuenta que mide, la pausa de 612 s con esa pérdida y las pérdidas seguidas valen para las decisiones abiertas desde `plan4.desde`. Las anteriores conservan la pérdida de antes (neto de la líder ≤ −10 $), no cuentan para la racha y no llevan la infracción. Una decisión nueva que abre justo tras otra anterior que perdió sigue sujeta a la pausa de 612 s con la definición de esa anterior.

### 10.5 Etiqueta `ESPEJO` (análisis; nueva)
- **Espejo** (Plan 4): cuando el MAE llega al disparo, el TP se pone en el BE (o en el PoC de la M204s anterior si está a mejor precio que la entrada) y el stop sigue siendo el de Rithmic.
- **Disparo** (ticks por contrato, MAE ≤ disparo): prueba de 25K (Apex o E2T) **−40**; prueba de 50K **−80**; real **−80**. En `reglas.yaml`.
- **Detección automática:** la operación lleva `ESPEJO` si, **después** del primer momento en que su flotante por contrato llega a ≤ disparo, se da de alta o se modifica una orden **TP** de la operación a un nivel **≥ 0 t** (precio de entrada o mejor).
- **Corrección manual** en la columna `Observaciones` (la del operador): si contiene la palabra `espejo` (sin distinguir mayúsculas) → `ESPEJO`; si contiene `no espejo` → se quita. Manda lo manual.
- Es de **análisis**: no pone la celda en rojo, no suena y **no cambia la Calificación** (decisión del operador: `LARGA · AGUANTANDO PÉRDIDA` sigue contando como proceso incorrecto aunque haya espejo).
- Es de la **decisión**: la seguidora usa la de su líder.
- SQLite (`operaciones.datos`): `espejo` (verdadero/falso) y `espejo_origen` (`auto` / `manual`).
- (concretado en la implementación, 07/10/2026)
  - **Disparo:** `espejo.disparo_ticks` (real −80; prueba por tamaño, 25K −40 y 50K −80). Una sim activada o una prueba de otro tamaño no tienen disparo y no llevan ESPEJO automático.
  - **Detección:** el flotante de la cuenta ÷ (valor del tick × contratos de la fila), como el MAE; el primer momento es la primera muestra del log con ≤ disparo. Cuentan las altas de órdenes TP de la operación (modificar es cancelar y dar de alta) en hora **estrictamente posterior**, con el nivel a `espejo.tp_min_ticks` (0) o más de beneficio. Solo se evalúan las operaciones cerradas.
  - **Manual:** la palabra completa `espejo` o la frase `no espejo` (en `reglas.yaml`), sin distinguir mayúsculas ni el número de espacios entre palabras; `no espejo` va antes (la primera contiene a la segunda) y `espejos` no cuenta. Lo manual se aplica en el sync siguiente (como la Calificación).
  - **Dónde:** `ESPEJO` va al final de Observaciones demonio de la fila de la decisión (tras las etiquetas del cortafuegos; la marca de estrategia tardía la añade después la vista). `espejo` y `espejo_origen` van en `operaciones.datos` de la líder y de sus seguidoras.
  - **También hacia atrás:** `ESPEJO` no depende de `plan4.desde` (§10.0): es de análisis y se marca en toda la historia.

### 10.6 Preoperativa, paso 2: compuerta (modifica §7.2)
- Cuatro filas nuevas en la zona de preoperativa, debajo de `Estrategia a usar`, con el texto en la columna A y un desplegable `SÍ` / `NO` en la columna de Estrategia. Textos en `reglas.yaml` (el operador los cambiará):
  1. `Sueño ≥ 6 h (reloj)`
  2. `Ni enfermo ni con conflicto personal abierto`
  3. `Plan leído hoy`
  4. `Ayer sin infracción`
- El demonio las **vacía al empezar cada sesión CME**. Se leen en el mismo ciclo que la estrategia (§7.2).
- Si en la apertura de una decisión alguna de las cuatro no está en `SÍ` (última lectura anterior a la apertura, en hora del exchange), la decisión lleva **`COMPUERTA SIN SÍ`**.
- Es **solo aviso** (decisión del operador): etiqueta de análisis, sin rojo, sin sonido, **no** cuenta como proceso incorrecto.
- Ya no es cierto que la estrategia sea la única celda con desplegable.
- (concretado en la implementación, 07/10/2026)
  - **Filas:** `preoperativa.compuerta` en `reglas.yaml` (`filas`, `afirmativo` `SÍ`, `negativo` `NO`, `etiqueta`). Desplegable estricto y fondo de la preoperativa, como el de la estrategia. Van debajo de `Estrategia a usar` y encima del encabezado de operaciones: la fila de la estrategia queda 4 filas más arriba que antes.
  - **Lectura:** la lectura periódica (cada `preoperativa.lectura_s`) pide la fila de la estrategia y las 4 de la compuerta en **una sola petición** (`HojaV4.leer_filas`, con el cliente de timeout corto de §7.2), y cada sync lee la hoja entera. La compuerta solo se anota si el texto de la columna A de cada fila coincide con `reglas.yaml` (si el operador cambia un texto, no se lee hasta el sync siguiente) y si la sesión CME de la lectura ya está escrita en el sheet.
  - **Registro:** `compuerta_lecturas` (hora de la lectura con el reloj del PC y los 4 valores) guarda solo los **cambios**; la lectura vigente a una hora es la última anterior o igual a ella (una lectura en el mismo instante de la apertura cuenta). La hora de cada lectura se toma antes de la petición, como la de la estrategia.
  - **Vaciado:** el primer sync de cada sesión CME deja las 4 celdas vacías y descarta lo que quedara en el sheet (es de la sesión anterior). Una decisión cuya última lectura es de otra sesión (la compuerta aún sin rellenar) lleva la etiqueta. Solo `SÍ` puntúa: vacío y `NO` son "no está en SÍ".
  - **Sin lecturas anteriores:** antes de la primera lectura de la historia (el día del primer arranque con este cambio) no se juzga: las decisiones anteriores no llevan la etiqueta. Solo se etiquetan las decisiones abiertas desde `plan4.desde` (§10.0).
  - `COMPUERTA SIN SÍ` va tras `ESPEJO` en Observaciones demonio de la decisión. No lleva rojo ni sonido, no cuenta como proceso incorrecto y no tiene campo propio en SQLite (queda en `obs`).

### 10.7 Fuera de la V4 (en el plan, no en el código)
- Límites de Rithmic, LTMAB, BE y trailing, salida del espejo por PoC, horario 15:30–21:00, compras de pruebas y setups.

## 11. Recuento semanal de los objetivos de proceso (decisión del operador, 08/10/2026) — IMPLEMENTADA (08/10/2026; sin verificación independiente todavía)

> Origen: Plan 4.1 §1.2 y §18.2 (Doc `ATAS/Plan trading 4.1`). Objetivo: que la V4 saque sola los recuentos del sábado, con la mínima fricción. Textos, nombres y umbrales en `reglas.yaml`.

### 11.1 Qué se cuenta
- **Unidad:** la decisión (§4.1). Las copias no cuentan y las sim tampoco.
- **Semana:** de lunes a viernes, según la sesión CME de la apertura de la decisión. Solo cuentan las decisiones con sesión ≥ `plan4.desde` (08/10/2026). La primera semana es parcial (08–09/10).
- **Objetivos:**
  1. **Sin preoperativa:** decisiones cuya estrategia final es `Sin estrategia` (la del sheet si el operador la editó, como en la Calificación), o que llevan `ESTRATEGIA ELEGIDA CON LA OPERACIÓN ABIERTA` o `COMPUERTA SIN SÍ`. Se cumple con 0.
  2. **Reentrada / sobreoperación:** número de etiquetas de infracción `REENTRADA TRAS PÉRDIDA`, `REENTRADA TRAS PÉRDIDAS SEGUIDAS`, `SOBREOPERACIÓN 10 MIN` y `SOBREOPERACIÓN 1 H` en las decisiones de la semana. Se cumple con 0.
  3. **Sin SL:** número de etiquetas `SIN SL`. Se cumple con 0.
  4. **Proceso correcto:** decisiones con proceso correcto (Calificación, `proceso_ok`) ÷ decisiones, en % entero. Se cumple con ≥ 80 %. Sin decisiones, `—`, y no se evalúa.
  5. **Compras fuera de cupo:** la V4 no lo ve. En el BAL aparece `5) manual`; en el histórico es una columna manual.
- **Cumplidos:** `k/N`, con N el número de objetivos evaluados entre 1 y 4.

### 11.2 Fila `SEMANA` en el BAL (semana en curso)
- Es una fila más del bloque BAL, debajo de las cuentas y antes de las 2 filas vacías. Es siempre una sola fila.
- **Columna `Cuenta`:** `SEMANA dd/mm–dd/mm` (lunes y viernes de la semana de la sesión en curso).
- **Siguiente columna:** un único texto, por ejemplo `6 decisiones · 1) sin preop. 6 ✘ · 2) reentr./sobreop. 2 ✘ · 3) sin SL 1 ✘ · 4) proceso ok 0 % ✘ · 5) manual · cumplidos 0/4`. ✔ si se cumple y ✘ si no. El resto de columnas de la fila, vacías.
- **Color:** fondo rojo (`colores.rojo`) en esas dos celdas si hay algún ✘.
- **Fin de semana:** la sesión del lunes empieza el domingo a las 17:00 CT. Hasta entonces, el sábado incluido, la fila sigue mostrando la semana que acaba de terminar, que es la que se revisa ese día.
- Se recalcula en cada sync, como el resto del BAL.

### 11.3 Pestaña `Objetivos semanales` (histórico)
- Pestaña nueva en el mismo sheet. Una fila por semana **cerrada**: la semana entra en la pestaña cuando la sesión en curso pertenece a una semana posterior. La más reciente, arriba.
- **Columnas:** `Semana | Decisiones | 1 Sin preoperativa | 2 Reentrada / sobreoperación | 3 Sin SL | 4 % proceso correcto | 5 Compras fuera de cupo | Cumplidos`.
  - La 5 es **manual**: se preserva por semana en SQLite, como las columnas manuales de las operaciones.
  - Si el operador escribe en la 5 un número > 0, cuenta como ✘ y `Cumplidos` pasa a `k/N` con N + 1. Con un 0, ✔.
- **Celdas de 1–4:** el número con ✔ o ✘, en rojo si ✘.
- Se regenera entera en cada sync desde SQLite. Las semanas salen de `operaciones`, así que no se pierde ninguna y se corrigen solas si cambia una estrategia o un MAE manual.
- Sin semanas cerradas, solo la cabecera.

### 11.4 (concretado en la implementación, 08/10/2026)
- **Dónde está:** `motor.recuento_semanal` cuenta (por semana y objetivo), `vista.evaluar_semana` / `fila_semana` / `layout_objetivos` / `parsear_objetivos` dan el texto, los ✔/✘ y la pestaña, `sync._escribir_objetivos` y `sheets.escribir_objetivos` la escriben. Textos, nombres, umbrales y la pestaña, en `reglas.yaml` → `objetivos`; las etiquetas son alias YAML de las existentes (`estrategia_tardia`, `compuerta.etiqueta`, `estrategia_por_defecto`, `reentrada`, `reentrada_seguidas`, `sobreoperacion_10`, `sobreoperacion_1h`, `sin_sl`) y la fecha es `plan4.desde`, sin copia.
- **Qué se cuenta de cada decisión:** la fila de la líder (ni seguidoras ni sim, `Fila.sim`) con la sesión CME de su apertura ≥ `plan4.desde`. Las etiquetas son las de su Observaciones demonio **tal como las ve el operador** (cortafuegos, `SIN SL`, `COMPUERTA SIN SÍ`) más `ESTRATEGIA ELEGIDA CON LA OPERACIÓN ABIERTA` si la tiene, comparadas por texto exacto. El recuento se hace en cada sync después de calificar y de marcar el análisis, con la estrategia final del sheet.
  - Objetivos 1 y 4 cuentan decisiones (una con varias causas del 1 suma una). Objetivos 2 y 3 cuentan etiquetas (una decisión con `REENTRADA TRAS PÉRDIDA` y `SOBREOPERACIÓN 10 MIN` suma 2).
  - El 4 usa `proceso_ok` de la Calificación (§4.2): una decisión con el proceso correcto cuenta aunque su resultado sea neutro o perdedor.
- **% entero:** es el entero por defecto (79,5 % → 79 %, no cumple). Lo que se ve es lo que se juzga; con 4 de 5 sale 80 % y cumple.
- **Semana:** el lunes de la semana de la sesión de la apertura. La sesión de sábado y domingo (la CME no abre hasta el domingo a las 17:00 CT) cae en la semana que acaba de terminar. La fila `SEMANA` usa la sesión CME en curso (reloj ATAS), así que el viernes tras las 17:00 CT, el sábado y el domingo hasta las 17:00 CT siguen mostrando la semana terminada, y desde el domingo a las 17:00 CT se muestra la nueva con 0 decisiones.
- **Fila `SEMANA`:** siempre una fila, también sin decisiones: `0 decisiones · 1) sin preop. 0 ✔ · 2) reentr./sobreop. 0 ✔ · 3) sin SL 0 ✔ · 4) proceso ok — · 5) manual · cumplidos 3/3`. Con una sola decisión, `1 decisión`. Rojo (`colores.rojo`, solo fondo) en las dos celdas si hay algún ✘. No es una cuenta: al leer el BAL no se confunde con un threshold manual.
- **Pestaña `Objetivos semanales`:** una fila por semana cerrada desde la primera con decisiones hasta la anterior a la de la sesión en curso; las semanas intermedias sin decisiones también salen (`0` ✔ en 1–3, `—` en el 4). La columna `Semana` es `dd/mm–dd/mm` (sin el prefijo `SEMANA`). Las celdas 1–4 son `número ✔|✘` (el 4, `NN % ✔|✘`), en rojo si ✘. El conteo sale de la misma lista de operaciones que se guarda en SQLite en cada sync, no de la pestaña ni de un histórico propio.
- **Columna 5 manual:** al empezar cada sync se lee de la pestaña y se guarda en SQLite (`objetivos_manual`, semana = lunes ISO, valor = texto tal cual): el sheet manda para las semanas que siguen en la pestaña (celda vaciada = se borra) y las ausentes se conservan. Un número cuenta en `Cumplidos` (0 cumple, > 0 no cumple y la celda sale en rojo, N + 1); un texto que no es número se muestra y no puntúa; vacía, no cuenta. Si la pestaña no tiene la cabecera esperada (nueva o tocada a mano) no se lee ni se borra nada. La fila se reconoce por su etiqueta `dd/mm–dd/mm`. Como en las columnas manuales de las operaciones, lo que se escriba entre la lectura y la escritura del sync (< 1 s) se pierde.
- **Fallo de la pestaña:** si leerla o escribirla falla (red, cuota), el sync principal sigue, se registra `[OBJETIVOS ERROR]` en `atas_v4.log` y el siguiente sync la regenera.
- **Una fila más en el BAL:** la preoperativa y las operaciones bajan una fila. Las lecturas de la preoperativa localizan la fila por su etiqueta (§7.2), así que no se rompen; el primer sync tras el despliegue reescribe todo con el layout nuevo.

## 12. Preoperativa: estado del mercado (decisión del operador, 08/10/2026) — IMPLEMENTADA (08/10/2026; sin verificación independiente todavía)

> Modifica §7.2 y §10.6. Motivo: las estrategias a usar serán distintas según el mercado esté en equilibrio (balance) o en desequilibrio (imbalance).

- **Fila nueva** en la zona de preoperativa, **encima** de `Estrategia a usar`:
  - columna A: `Estado del mercado`;
  - columna de Estrategia: desplegable estricto con `Balance`, `Imbalance` y `-` (por defecto). Textos en `reglas.yaml`.
- **No es de la compuerta:** su valor no cuenta como SÍ/NO, no genera `COMPUERTA SIN SÍ` y no entra en los objetivos de §11.
- **Reseteo diario:** el demonio la vuelve a poner en `-` al empezar cada sesión CME, igual que las 4 filas de la compuerta. Se lee en la misma petición que la estrategia y la compuerta.
- **Registro:** cada decisión guarda en SQLite (`operaciones.datos.mercado`) el valor de la última lectura anterior a su apertura (hora del exchange), como la compuerta. Puede valer `Balance`, `Imbalance` o `-`. Sirve para analizar las estrategias por estado del mercado. No hay columna nueva ni etiqueta en Observaciones demonio.
- La lectura periódica comprueba la etiqueta de la columna A de cada fila, como ya hace con la estrategia. Insertar esta fila no puede romper la elección de estrategia en vivo.

### 12.1 (concretado en la implementación, 08/10/2026)
- **Textos y valores** en `reglas.yaml` → `preoperativa.mercado` (`etiqueta`, `defecto` `-`, `valores`: `Balance`, `Imbalance`, `-`); la fila usa la columna de `Estrategia` como el resto de la preoperativa y el mismo color de control. La fila queda **justo encima** de `Estrategia a usar`; la compuerta sigue bajo la estrategia. `preop_celda` (la celda de la estrategia) no cambia de significado.
- **Lectura:** el valor se lee en la misma petición que la estrategia y la compuerta, que ahora empieza una fila más arriba y pide una más (6 filas). La etiqueta de la columna A se comprueba por fila: si la del mercado no está (pestaña anterior al 08/10/2026, fila movida o texto cambiado) no se anota el mercado, pero la estrategia y la compuerta se leen igual; si falta la de la estrategia, no se lee nada, como antes. Un valor que no está en la lista (celda vacía) es el defecto.
- **Sesión:** como la compuerta, la lectura periódica solo se anota si el sheet ya es de la sesión CME en curso; el primer sync de cada sesión deja la fila en `-` y descarta lo que hubiera.
- **Registro:** tabla SQLite `mercado_lecturas` (hora de la lectura con el reloj del PC y valor; solo los cambios). `marcar_analisis` guarda en `operaciones.datos.mercado` el valor de la última lectura anterior o igual a la apertura (hora del exchange); si esa lectura es de otra sesión CME o no hay ninguna, el defecto. La seguidora guarda el de su líder. Se calcula para todas las operaciones, también las anteriores a `plan4.desde` (hoy sin lecturas dan `-`). Sin etiqueta, sin rojo, sin sonido, sin efecto en la Calificación, la compuerta ni los objetivos de §11.
- **Primer arranque con la pestaña antigua:** el primer sync no encuentra la fila, la escribe con `-` y baja una fila la preoperativa; `preop_celda` se actualiza al escribir. Hasta entonces, una lectura periódica con la celda guardada de antes lee la estrategia como siempre.

## 13. R17 en la preoperativa (encargo del operador, 09/10/2026) — IMPLEMENTADA (09/10/2026; sin verificación independiente todavía)

> **INFORMATIVO.** No es una regla del Plan 4 ni de ninguna otra: no genera etiquetas, rojos, sonidos ni calificación, no entra en los objetivos de §11 y no se guarda en SQLite (no hay histórico de R17 en la V4). La propuesta de stops por volatilidad (`..\estrategias\DISENO_stops_volatilidad.md`) sigue pendiente de grill; esta celda solo muestra el número para mirarlo. Modifica §7.2 en una sola cosa: la fila `Estrategia a usar` gana una celda a su derecha.

- **Qué es:** R17 = (máximo de los H − mínimo de los L) de las 5 últimas M204s cerradas, en ticks enteros (÷ 0,25); stop calibrado = 1,53 × R17^0,77 redondeado al tick entero (`DISENO_stops_volatilidad.md` §2–§3; implementación de referencia `indicador_velas\rasgos.py`: `r17`, `stop_calibrado`).
- **Dónde:** la celda inmediatamente a la derecha de la de la estrategia (`preop_celda`), en la fila `Estrategia a usar` (hoy, la columna J). Es la única celda que R17 escribe: nunca la de la estrategia ni ninguna otra.
- **Texto:** `R17 461 t · stop 172 t · 16:29:24`. La hora es la de Madrid del **cierre** de la última M204s usada (apertura + 204 s).
  - Sin datos: si la última vela cerrada tiene más de `antiguedad_max_s` (438 s = 2 × 204 + 30), o las 5 más recientes no son contiguas, `R17 sin datos desde HH:MM:SS` (hora de Madrid del cierre de la última vela que completó una ventana válida), o `R17 sin datos` si no se conoce ninguna.
  - Solo se escribe cuando el texto cambia: como mucho una escritura por vela cerrada (cada 204 s) más el paso a `sin datos`.
- **Fuente:** `D:\proyectos\conceptos\atas\velas\NQ_204s\YYYY-MM-DD.jsonl` (fecha UTC de la apertura; una línea por versión de vela; campos `apertura`, `h`, `l`). Una vela se escribe cuando aparece la siguiente, así que toda vela del fichero está cerrada. Se leen **también** las líneas `tiempo_real` (a diferencia de `velas.py`, que las ignora para el análisis): para el vivo son lo único que hay.
- **Selección de la ventana:**
  1. En un mismo fichero conviven dos rejillas (el histórico recargado parte de las 18:00 ET; el vivo va en tiempo absoluto, múltiplos de 204 s desde 0001-01-01). Se toma la vela de **apertura más reciente** y solo se conservan las de **su rejilla**: las que distan de ella un múltiplo exacto de 204 s.
  2. De cada apertura vale la **última versión** del fichero.
  3. Se exigen las **5 más recientes contiguas**, separadas exactamente 204 s. Con un hueco entre ellas, o con menos de 5, no hay R17.
  4. Se miran el fichero del día UTC de ahora y el del día anterior (cruce de medianoche UTC). Si ninguno de los dos tiene velas (más de un día UTC parado), el texto es `R17 sin datos` a secas.
- **Lectura sin molestar a ATAS:** los ficheros solo se abren en modo lectura binaria (`rb`); nunca se modifican ni se bloquean. De cada fichero se recuerda la posición y solo se lee lo añadido (la primera vez, entero: unos pocos MB). Una línea a medio escribir (sin salto de línea) espera a la siguiente lectura; una línea corrupta se salta; si el fichero se encoge se vuelve a leer entero.
- **Hilo propio** (`atasv4\r17.py`, `ServicioR17.bucle`): mira los ficheros cada `r17_intervalo_s` (5 s, `config.yaml`) y no comparte con el ciclo del demonio ni el cliente de Sheets ni la conexión SQLite. Un fallo de red, de fichero o de cálculo se registra (`[R17 ERROR]`, como mucho una vez por minuto) y deja al hilo en pausa `preoperativa_pausa_error_s` (30 s); no llega al ciclo principal, a los avisos del cortafuegos ni a la lectura de la preoperativa (§7.2).
- **Escritura:** cliente propio con el timeout corto de §7.2 (`preoperativa_timeout_s`, 3 s). Antes de escribir se lee la fila de la estrategia (columna A hasta la de la celda de R17) y se comprueba que la columna A sigue diciendo `Estrategia a usar`, igual que la lectura de §7.2; si no (alguien insertó o borró filas a mano), no se escribe y se espera al siguiente sync (el hilo no vuelve a mirar el sheet hasta pasados 30 s o hasta que el sync le diga dónde está la fila). Dos peticiones por escritura, una de ellas de una sola celda en modo RAW.
- **Convivencia con el sync** (decisión): el sync reescribe la pestaña entera (borra A:AD y vuelve a escribir), así que **reescribe la celda de R17 con el último texto escrito** (`vista.poner_r17`) en lugar de dejarla en blanco hasta la siguiente vela. Además:
  - Un cerrojo (`ServicioR17.cerrojo`) serializa «leer la etiqueta y escribir R17» con la escritura de la pestaña del sync, para que una fila que el sync acaba de mover no reciba el texto en la celda equivocada.
  - El sync espera al cerrojo como mucho `r17_espera_sync_s` (10 s): si R17 está colgada en la red, el sync sigue sin él. En ese caso extremo la comprobación de la etiqueta sigue protegiendo a R17.
  - El sync le dice a R17 dónde queda la fila de la estrategia (`fijar_celda`) tras cada escritura correcta; al arrancar el demonio parte de la guardada (`preop_celda`).
  - El texto que se vuelve a escribir es el que R17 cree que hay en el sheet, nunca uno calculado y aún sin escribir.
- **Reglas en `reglas.yaml` → `preoperativa.r17`:** `activo` (`false` = ni hilo ni celda), `carpeta`, `marco_s` (204), `velas` (5), `tick` (alias del tick del NQ de `simbolos`), `stop.coef` (1,53) y `stop.exponente` (0,77), `antiguedad_max_s` (438) y los tres textos. El código no contiene ninguno de estos valores. En `config.yaml`: `r17_intervalo_s` y `r17_espera_sync_s`.
- **Límites conocidos:**
  - «Ahora» es el reloj del PC, sin la corrección del reloj ATAS (§1): el desfase de unos segundos queda dentro de los 30 s de margen de la antigüedad.
  - Una vela se escribe al aparecer la siguiente: en un mercado sin operaciones durante más de 438 s la celda pasa a `sin datos` aunque no haya pasado nada.
  - Con la V4 parada el hilo no corre y la celda conserva el último texto, con su hora: cuenta por la hora que lleva, no por estar ahí.
  - Si el fichero lo reescribe el indicador entero (carga histórica) y queda más corto, se vuelve a leer entero; si queda igual de largo con otro contenido, no se nota hasta que crezca.
  - Los números de stop del diseño (`DISENO_stops_volatilidad.md` §3, tabla de referencia) difieren en 1 tick de la fórmula en R17 = 150 (73 frente a 72) y 200 (91 frente a 90): manda la fórmula, que es la que implementan `rasgos.py` y la V4. Para R17 = 461 la fórmula da 172 (no 169: ese es el valor de R17 = 450).

### §13.1 Robustez de R17 (09/10/2026)

- **Plazo de reloj de pared** (`r17_plazo_s`, por defecto 6 s, siempre menor que `r17_espera_sync_s`): la E/S con Sheets va en un hilo auxiliar con `join(plazo)`. Si no vuelve, se abandona y se suelta el cerrojo, así que el sync nunca espera más de `r17_espera_sync_s`.
- **El hilo de R17 no muere:** cualquier excepción imprevista se registra como `[R17 ERROR]` y el bucle sigue.
- **Configuración inválida** (reglas.yaml o config.yaml): `[R17 ERROR]` y el demonio arranca sin R17.
- **Solo escribe** si la celda destino está vacía o su texto empieza por `R17`. Si no, no escribe y lo registra (protege frente a columnas insertadas).
- **El sync conserva el texto** que hay en la celda (empiece por `R17`), aunque el hilo no haya escrito todavía o el demonio esté parado.
- Sin revisión independiente de estos cambios (decisión del operador, 09/10/2026). Suite: 625 tests en verde.

## 14. Reset de una cuenta (decisión del operador, 03/11/2026) — IMPLEMENTADA (03/11/2026; verificación independiente Sonnet: APROBADA CON RESERVAS, 0 ALTA / 3 MEDIA / 8 BAJA, ver INFORME §7)

Apex resetea una evaluación conservando el mismo ID de Rithmic. Orden del operador: "ponles un sufijo 74-reset1 y 75-reset1 para no confundirla con las 74 y 75 ya quemadas".

### 14.1 Regla
- Un reset crea una **cuenta lógica nueva** `<ID>-resetN` (N = 1, 2, … por cada reset de la misma cuenta física). Ejemplo: `APEX-91120-74-reset1`.
- La cuenta original (o el reset anterior) conserva todo lo de antes del corte y sigue como esté (quemada o no; el reset NO la quema ni la desquema).
- **Corte `desde`** (hora del log de ATAS, Madrid, reloj del PC; formato `YYYY-MM-DD HH:MM:SS[.mmm]`): todo evento de la cuenta física con hora ≥ `desde` pertenece a `<ID>-resetN`; lo anterior, a la cuenta (o reset) anterior. Vale para todas las fuentes: fills y órdenes del log, `Bal:`, `Position changed`, `MyTrade`, `HistoryMyTrade`, `Orders`. Las horas UTC de los `.cdb` se comparan con `desde` pasado a UTC. Un reset se registra con la cuenta plana y sin operaciones alrededor del corte: la precisión de segundos no importa.
- `Portfolio.cdb` (balance actual, sin hora) se lee por el ID físico y pertenece a la lógica más reciente de esa cuenta.
- **Sin herencia:** la cuenta nueva arranca como un alta automática (§3): firma y tipo por el patrón del ID físico, tamaño por el primer balance tras el corte, margen inicial 500, racha 0, ancla de threshold estimada desde el tamaño, sin threshold manual, sin retiros, sin semillas, sin días previos. Nada de la vida anterior cuenta para sus reglas (margen, racha, objetivo, máximo y pérdida diaria, inactividad, días +50/+100).
- Las copias por simultaneidad (§4.1) tratan `<ID>-resetN` como una cuenta distinta de la original.
- **Retroactivo:** registrar un reset reasigna a la cuenta nueva lo ya ingerido tras `desde`. Es idempotente.

### 14.2 Cómo se registra
- `python cli_v4.py reset <cuenta física> <desde>`, con el demonio parado (como cualquier edición de `cuentas.yaml`). Se guarda en `cuentas.yaml` (lo mantiene el sistema): por cuenta física, la lista de resets con su `desde` y su nombre.
- Error si `desde` no es posterior al reset anterior de esa cuenta, o si la cuenta física no existe.

### 14.3 Vista
- Fila BAL propia con el nombre `<ID>-resetN` y su columna manual de threshold.
- ID corto de operación: `AP<n>R<N>` (`APEX-91120-74-reset1` → `AP74R1`), en `reglas.yaml` → `cuenta_corta`.

### 14.4 Primer uso
- `APEX-91120-74` y `APEX-91120-75`, `desde 2026-10-28 21:06:59.587`: primer `Bal:50000` de ambas en el log. Último balance viejo: 01/10/2026 15:55:50 (48.024,08 y 50.195,90). Último fill: 23/09 (74) y 28/09 (75). Sin eventos entre medias.

### 14.5 (concretado en la implementación, 03/11/2026)
- **Dónde está:** `atasv4\resets.py` (`cuenta_logica(registro, cuenta física, hora)`: el único sitio donde se traduce cuenta física + hora a cuenta lógica; registro, nombres y `expandir_pares`), `almacen.py` (la ingesta lo aplica a cada evento antes de guardarlo; `reasignar_resets` reasigna lo ya guardado; `aplicar_resets` lo repite si cambió el registro; `primer_bal`), `cuentas.reset` (registro y validaciones), `cli_v4.py reset`, `sync.py` (pasa el registro a la ingesta y expande `CopyTrading.cnf`). La columna `cuenta` de SQLite es la lógica: el resto del código (motor, cortafuegos, vista) no distingue un reset de una cuenta cualquiera. Reglas en `reglas.yaml`: `reset.sufijo` (`-reset{n}`) y los patrones de `cuenta_corta` (`AP74R1`, `PA15R1`, `E2T250R1`, `BX50R1`).
- **Registro:** `cuentas.yaml` → `resets: {<ID físico>: [{desde: 'YYYY-MM-DD HH:MM:SS.mmm', nombre: <ID>-resetN}]}`. `desde` ya normalizado (con ms y comillas); si se edita a mano vale con o sin ms y sin comillas. `estado.resets_aplicados` guarda la huella del registro con el que se reasignó SQLite: si cambia (CLI o edición a mano), la siguiente ingesta reasigna una vez.
- **Corte:** `hora >= desde`, inclusivo (el primer `Bal:50000` de APEX-74 y -75 está exactamente en `desde`). Log: hora del log (Madrid, cruda). `.cdb`: la hora UTC contra `desde` pasado a UTC. `HistoryMyTrade`: una pata es de la cuenta en que **abrió**. `Orders.cdb`, `Position.cdb` y `Portfolio.cdb` no se leen en el código (la V4 no los ingiere): `Portfolio.cdb` solo sirve para verificar a mano (INFORME §8) y su balance es el de `resets.logica_actual`.
- **Decisiones fuera de la spec** (la 1, CONFIRMADA por el operador el 03/11/2026: «Desde el reset: 1.060»; manda sobre el «margen inicial 500» de §14.1, que es el valor en el alta):
  1. **Alta de la cuenta nueva = sesión CME del corte**, no la del primer sync tras registrarla: la cuenta nace en el corte y su margen cuenta desde entonces (500 + 140 por sesión hábil sin operaciones, §10.2), como si se hubiera dado de alta en vivo. Con el corte del 28/10 y hoy 03/11, APEX-74-reset1 y -75-reset1 salen con margen 1.060 (500 + 4 × 140). Si se prefiere el alta del día del registro, saldría 500.
  2. **Tamaño y balance base = primer `Bal:` de la cuenta lógica** (§14.1: «primer balance tras el corte»), no el último plano de las altas automáticas; sin diferencia cuando no hay operaciones. El ancla estimada sigue siendo `balance base − drawdown`.
  3. **`CopyTrading.cnf` trae IDs físicos:** cada uno figura en la cuenta original y en todos sus resets (así lo anterior al reset se juzga como antes y la cuenta nueva también figura como líder).
  4. **Operaciones ya anotadas tras el corte** (reset registrado después de operar con la cuenta nueva): pasan a `AP74R1-…` y sus columnas manuales del sheet no se trasladan; el CLI lo avisa con la lista de ID. No hay migración de ID.
  5. **El CLI se niega** con el demonio en marcha, con una cuenta sim, con una cuenta lógica (`…-reset1`) en vez de la física y si `reglas.yaml` → `cuenta_corta` no tiene patrón para el nombre nuevo (sin él, el ID corto sería `RESET1` en todas las cuentas y el `op_id` único chocaría). Hoy hay patrón para Apex PA y evaluación, E2T TCP y Bulenox; la E2T real (`^livesim`) no lo tiene.
  6. No hay «deshacer un reset»: se vuelve atrás restaurando `atas_v4.sqlite` y `cuentas.yaml` previos.
- **Límites conocidos:** una orden registrada antes del corte y cancelada o llenada después daría dos cuentas a un mismo `Order trade` (el fill queda pendiente en `fills_ot_pendientes`); la precisión del corte es de segundos (MyTrade trunca al segundo): por eso el reset ha de registrarse con la cuenta plana y sin operaciones alrededor.

## 15. Ciclos de operaciones (orden del operador, 11/11/2026) — IMPLEMENTADA (11/11/2026; sin verificación independiente)

- **Qué es:** la pestaña `V4` solo muestra las operaciones del **ciclo en curso**. Cuando el operador lo pide (porque le interesa o porque ya son muchas), el ciclo se **archiva**: la pestaña entera se copia a otra y la `V4` empieza un ciclo nuevo, con el BAL y sin operaciones.
- **Solo a mano**, como las quemas y los retiros: `archivar` en la consola del demonio, o `python cli_v4.py archivar` (con el demonio en marcha se lo encarga a él; parado, lo hace el CLI). Con `--hasta YYYY-MM-DD` (solo con el demonio parado) entra en el archivo hasta ese día de Madrid incluido y lo posterior se queda en el ciclo nuevo.
- **Exige todas las cuentas planas.** Sin operaciones en el ciclo no se archiva.
- **Nombre de la pestaña archivada:** `V5 <día de la primera decisión> a <día de la última>` (hasta el 11/11/2026, `V4 …`) (días de Madrid; `reglas.yaml` → `ciclos`). Si el nombre ya existe, se le añade ` (2)`. Queda justo a la derecha de la `V4`.
- **La pestaña archivada es una foto fija.** Lleva valores, formatos y desplegables, pero el demonio no vuelve a leerla ni a escribirla: lo que se edite en ella (Estrategia, Observaciones, Análisis LLM, MAE/MFE) **no llega a SQLite**. Lo que hubiera escrito en el momento de archivar sí está guardado (el archivo empieza con un sync).
- **SQLite no pierde nada y el cálculo no cambia:** margen, rachas, balance, threshold, cortafuegos y recuento semanal siguen saliendo de toda la historia; un ciclo es solo qué filas enseña la pestaña. El corte se guarda en la tabla `ciclos` (apertura de la primera y de la última operación archivada, pestaña, nº de decisiones, hora).
- **No hay «desarchivar».** Para volver a ver todo en la `V4`: borrar la fila de `ciclos` en SQLite (con el demonio parado) y sincronizar; la pestaña archivada se borra a mano.
- **Primer uso (11/11/2026):** ciclo del 22/09/2026 al 10/11/2026 archivado; el ciclo nuevo empieza con la operación de la E2T real del 11/11.

## 16. V5: plan cumplible (grill del operador, 11/11/2026, Q1–Q47) — IMPLEMENTADA (11/11/2026; revisión independiente: ver INFORME)

> Idea: «un plan mediocre que cumplo es mejor que el perfecto que no cumplo». En la V4, la gran mayoría de las decisiones tenían proceso
> incorrecto y los cortafuegos por tiempo no separaban ganadoras de perdedoras. Registro del grill: `..\CONTEXT.md`. Plan
> resultante: `..\plan trading\5-plan.md`. Todo número y texto, en `reglas.yaml` → `v5`.

### 16.1 Alcance y fecha
- Las reglas de este apartado rigen para las decisiones cuya **sesión CME de apertura es ≥ `v5.desde` (2026-11-11)**. Todo lo anterior conserva exactamente las reglas, etiquetas y calificación de la V4 (§1–§15).
- **«Regla» es solo lo que impone Rithmic** y lo configura el operador allí, no el demonio: tope diario de ganancia de **+1.050 $ por cuenta en las pruebas** y **+119 $ (238 t de MNQ) en las reales**; pérdida máxima diaria de las reales **−175 $ (−350 t de MNQ)**; el Loss Limit de las pruebas no cambia.
- Lo voluntario se llama **«Consejos que no pedí»**.
- **Banda de resultado, para todo** (rachas, Calificación, avisos, `Nº ganadas (día)`): ganadora ≥ **+20 t**, perdedora ≤ **−20 t**, neutra entre medias; por contrato, fila de la líder (`v5.banda_ticks`).
- No cambian: margen de operativa (§5, §10.2), threshold (§6), contratos permitidos, reglas de las firmas (§8, §9), inactividad (§8.3 y §10.1), copias (§4.1), reloj ATAS (§1), reset (§14) y ciclos (§15).

### 16.2 Turnos de operativa
- Un **turno** va de arrancar el demonio a cerrarlo. Se numeran dentro de cada día CME por su primera decisión (`T1`, `T2`…): un arranque sin operar no consume número. Lo operado con el demonio apagado es **`turno fantasma`**.
- Si el demonio se cuelga o se cierra con la X, el turno acaba en su último latido (config `turno_latido_s`, 60 s).
- El turno **no gobierna nada** (reglas y avisos van por sesión CME): es un dato para comparar turnos. Se guarda en SQLite (`turnos`; `operaciones.datos.turno`) y **no se muestra en la hoja**.
- El arranque y el cierre del turno se guardan con el reloj del PC; para compararlos con la apertura de la decisión (hora del exchange) se pasan antes a la hora del exchange con el reloj ATAS (`Sincronizador._turnos_exchange`, `reloj.pc_a_utc`; desde la 5.1, §16.13). El recordatorio de inicio de turno (§16.3) compara igual.

### 16.3 Preoperativa
- Quedan `Estado del mercado` (§12), `Estrategia a usar` (§7.2) y R17 (§13). **Las cuatro filas SÍ/NO de la compuerta y la etiqueta `COMPUERTA SIN SÍ` desaparecen** (`preoperativa.compuerta.mostrar: false`; el código sigue ahí porque la etiqueta de las decisiones de la V4 se calcula con él).
- **Recordatorio de inicio de turno:** al arrancar el demonio, una celda en mayúsculas con fondo naranja encima de la preoperativa: `SUEÑO ≥ 6 H · PLAN LEÍDO HOY · NI ENFERMO NI CON CONFLICTO PERSONAL ABIERTO` (`v5.recordatorio`). No hay nada que rellenar. Desaparece con la primera decisión anotada del turno. El CLI no la muestra (no abre turno).

### 16.4 Consejos que no pedí: avisos por sesión CME
Se calculan sobre las decisiones **cerradas** (nunca el flotante), en orden de cierre, dentro de la sesión CME de la apertura.

| Aviso | Cuándo salta | Sonido | Abrir después |
|---|---|---|---|
| `4 GANADORAS SIN PERDER: NO OPERAR MÁS HOY` | al cerrar la 4ª ganadora del día sin ninguna perdedora antes (las neutras ni cuentan ni lo anulan) | `profit.wav` ×2 | `CONSEJO IGNORADO: OPERAR TRAS 4 GANADORAS` |
| `N PERDEDORAS SEGUIDAS: PARAR Y ANALIZAR EL MERCADO` | con la racha de perdedoras ≥ 3; se repite con cada perdedora nueva; solo la rompe una ganadora | `stoploss.wav` ×2 | no es infracción |
| `N T PERDIDOS HOY: FIN DE SESIÓN` | cuando la suma de los ticks de **todas** las decisiones cerradas en negativo (también una neutra de −12) llega a **407** | `stoploss.wav` ×2 | `CONSEJO IGNORADO: OPERAR TRAS PERDER 407 T` |
| `FIN DE TURNO INMINENTE` | a las 11:55 de Nueva York (17:55 de Madrid), con el demonio abierto, una vez al día | sin sonido | — |

- **Canal:** notificación de Windows **sin su tono** + el wav de ATAS (`C:\Program Files (x86)\ATAS Platform\Sounds`), dos veces. Windows no admite un wav propio en la notificación de un programa sin empaquetar: por eso van separados.
- Notifican **siempre**, sean cuentas reales o pruebas (en la V4 solo notificaba una decisión con alguna real). Un evento con más de `v5.avisos.max_antiguedad_s` (300 s) se registra sin avisar (arranque, reingesta).
- El texto del aviso queda también en Observaciones demonio de la decisión cuyo cierre lo disparó, **sin color**.
- Los eventos de la V4 siguen sin sonido ni notificación (`cortafuegos.aviso`).

### 16.5 Infracciones
- **En rojo** (fondo rojo, texto negro): `CONTRATOS > PERMITIDOS`, `OPERACIÓN TRAS OBJETIVO`, **`SIN ESTRATEGIA`** (la estrategia final de la decisión es la de defecto) y **`FUERA DE HORARIO`** (apertura fuera de 09:30–12:00 de Nueva York = 15:30–18:00 de Madrid; definida en hora de Nueva York para que el desfase del cambio de hora se ajuste solo).
- **En naranja** (texto negro), con el prefijo `CONSEJO IGNORADO:`: abrir tras el aviso de las 4 ganadoras o tras el de los 407 t. Si la misma decisión lleva una roja, manda el rojo.
- **Todas notifican al abrir la operación**, con `stoploss.wav` ×2 (también `OPERACIÓN TRAS OBJETIVO`, con la posición aún abierta). `SIN ESTRATEGIA` se avisa al abrir si, en la primera lectura de la casilla **posterior** a la apertura (cada `preoperativa.lectura_s`, 5 s; durante un sync no se lee), sigue en el valor por defecto: así no salta si la estrategia se eligió justo antes de entrar. Una operación que se abre y se cierra antes de esa lectura no avisa; su `SIN ESTRATEGIA` queda igual en la hoja.
- **Desaparecen** (para las decisiones de la V5): `REENTRADA TRAS PÉRDIDA`, `SOBREOPERACIÓN 10 MIN`, `SOBREOPERACIÓN 1 H`, `REENTRADA TRAS PÉRDIDAS SEGUIDAS`, todos los `… NO OPERAR HASTA hh:mm`, `SIN SL`, `SL CANCELADO CON POSICIÓN ABIERTA`, `OPERACIÓN TRAS PÉRDIDA DIARIA`, `COMPUERTA SIN SÍ` y `ESPEJO` (automático y manual).
- **Se quedan como texto sin color:** `OBJETIVO ALCANZADO: NO OPERAR MÁS`, `+50 DEL DÍA ALCANZADO…`, `PÉRDIDA DIARIA −75 T…`, `MÁXIMO BAL DE HOY…` y las etiquetas de duración (`CORTA`, `LARGA · …`), hasta el grill de setups.
- `cortafuegos_saltados` guarda las de entrada y los consejos ignorados; `SIN ESTRATEGIA` no (se conoce al anotar; está en Observaciones demonio y en los objetivos).

### 16.6 Calificación y Ratio B/R
- **Proceso correcto** = estrategia elegida **antes de entrar** (ni la de defecto ni elegida con la operación abierta) **y ninguna infracción**, roja o naranja. La duración ya no cuenta.
- Resultado con la banda de ±20 t. Los cuatro textos de los cuadrantes y `Neutro` no cambian.
- **`Ratio B/R` pasa a ser el planeado por la estrategia** (`v5.ratio_planeado`): 1,5 (+204 / −136) mientras no haya setups; cada estrategia traerá el suyo.
- `Racha` y `Nº ganadas (día)`: por ticks, con la banda.

### 16.7 Hoja y columnas
- Hoja nueva **`Registro_bot_V5`** (config `sheet_id`), pestañas `V5`, `Objetivos semanales` e `Histórico unificado`. La hoja antigua (`Registro_bot v3`, config `sheet_id_antiguo`) queda intacta: el demonio ya no escribe en ella.
- Operaciones: **entran** `Nº en el día` (por apertura), `Acumulado del día (ticks)` y `Ticks perdidos del día` (los dos, tras el cierre de la decisión y solo con cerradas); **salen** `Análisis LLM`, `Tipo apertura` y `Resultado anterior (ticks)`. La columna `analisis_llm` de SQLite se conserva.
- BAL: `Inactividad Apex` pasa a llamarse `Inactividad`. Sin filas nuevas.

### 16.8 Objetivos semanales
- Tres recuentos por semana, solo con decisiones de la V5: **1) `SIN ESTRATEGIA`** (máximo 0), **2) infracciones en rojo** distintas de la anterior (máximo 0; los consejos ignorados no cuentan) y **3) % de proceso correcto** (mínimo 80 %). Sin columna manual (las compras fuera de cupo ya no se registran).
- Las semanas de la V4, con sus cinco objetivos, se quedan en la pestaña de la hoja antigua.

### 16.9 Histórico unificado
- Pestaña `Histórico unificado`: **una fila por decisión** (sin copias), lo más nuevo arriba, con las columnas de la V5 más `Origen` (`histórico 16/03–21/09`, `V4`, `V5`). Lo que una fila no tenía queda vacío.
- **Carga inicial** (`python cli_v4.py unificado`, hecha el 11/11/2026): las decisiones anteriores a la V4 (pestaña `Decisiones histórico` de la hoja antigua) y el ciclo archivado del 22/09 al 10/11 (410). Repetirla no duplica: solo añade los ID que falten.
- **Después el demonio solo añade**, al archivar un ciclo (§15), sin reescribir lo ya puesto: lo que el operador anote en esa pestaña no se pisa (tampoco vuelve a SQLite).
- Archivar sigue creando además la pestaña del ciclo (`V5 <desde> a <hasta>`), que el operador renombra.

### 16.10 Foto del cálculo
- Para acortar el cálculo sin perder datos: de cada operación **ya archivada** se guarda lo que da el log (SL/TP, tipos de orden, MAE/MFE, `SIN SL`…: `motor.CAMPOS_FOTO`) y la evaluación del cortafuegos de su decisión (tablas `foto_filas`, `foto_evaluar`). El motor las toma de ahí y no vuelve a consultarlas.
- Lo que encadena la historia (margen, rachas, balance, threshold, reglas de la cuenta, recuentos) **se sigue calculando entero** con esos datos: la foto no cambia ningún resultado.
- **Se comprueba sola:** al hacerla, el cálculo con foto tiene que dar exactamente lo mismo que el completo (`motor.canon`); si no, se descarta y el demonio sigue calculándolo todo. `python cli_v4.py foto` la rehace, `--comprobar` la compara y `--quitar` la borra. Archivar un ciclo la extiende al corte nuevo.
- La foto congela los `CAMPOS_FOTO` de cada operación archivada **y la evaluación entera del cortafuegos de su decisión**, que depende de `cortafuegos.*`, `racha.perdedora_max`, `v5.*` (banda, 407, N), de qué cuentas son reales o pruebas, de los resets y del emparejamiento de copias.
- Por eso **el demonio la comprueba al arrancar** (antes del primer sync; ~0,5 s): si ya no coincide con el cálculo completo (porque cambió algo de lo anterior), la rehace y la rehecha se comprueba sola. Con el demonio en marcha, un cambio en `reglas.yaml` no se lee hasta reiniciarlo, así que tampoco puede descuadrarla.
- Medido el 11/11/2026 con unos cientos de operaciones: 383 ms → 120 ms por cálculo; 4.198 → 1.018 consultas.

### 16.11 Nombres que NO cambian (para futuras mejoras)
- Carpeta `atas\v4\`, paquete `atasv4`, `demonio_v4.py`, `cli_v4.py`, `atas_v4.sqlite`, `atas_v4.log`, `SPEC_V4.md`, `INFORME_V4.md` y el mutex `Global\ATAS_DAEMON_V4`: se conservan. Cambian el título de la ventana, el acceso directo (`Iniciar Demonio ATAS V5` → `INICIAR_DEMONIO_ATAS_V5.bat`), la hoja y la pestaña.
- Si algún día se renombra la carpeta: revisar `config.yaml` (rutas relativas: `sqlite`, `backups_dir`, `logs_respaldo`, `semillas`), el `.bat`, el acceso directo, `reglas.yaml` → `preoperativa.r17.carpeta` (ruta absoluta a `atas\velas`), los scripts de `atas\analisis_historico` y la memoria del asistente.

### 16.12 Pendiente de otros grills
- **Setups:** definirlos; el stop máximo por operación será −136 t y el objetivo +204 t; ratios por estrategia en `v5.ratio_planeado.estrategias`; decidir sobre `CORTA` / `LARGA` y `Estado del mercado`.
- **Margen:** restar margen por cada consejo ignorado (propuesta: −70 por decisión y cuenta). No implementado por decisión del operador: primero se prueba la V5 sin mezclar el margen.

### 16.13 V5.1 (17/11/2026): revisión de código, SIN cambios de reglas
- Versión **5.1** = la V5 tras una revisión de código y errores pedida por el operador. No cambia ninguna regla, número ni texto de `reglas.yaml`, ni el cálculo de la hoja: con la base real, un `sync` completo da la misma pestaña que antes (`INFORME_V4.md`, sección del 17/11/2026).
- **Turnos y recordatorio en la misma hora** (§16.2): los turnos se comparan con las aperturas en hora del exchange, no del PC (la deriva medida era de 0,3 a 0,83 s). Con los datos reales de 12–16/11 no cambia ninguna decisión.
- **Foto del cálculo** (§16.10): `almacen.conectar` descarta la entrada de la caché `_FOTO` que haya dejado otra conexión con el mismo `id` (hallazgo 4 de la revisión del 11/11: en producción no pasa, una conexión por proceso).
- Código muerto retirado (`aviso.avisador`, `v5.hora_madrid` y sus imports). Título de la ventana, log de arranque y banner dicen «V5.1»; los mensajes de salida y de los avisos no cambian.
- **Configuración en capas:** `config.yaml` (sin datos personales) < `config.local.yaml` (tuyo, ignorado por git) < variable de entorno `ATAS_SHEETS_CREDENCIAL` (solo la credencial) < `cfg_override` (tests). `atas_dir` por defecto es `%APPDATA%\ATAS`. El demonio no arranca si no existe esa carpeta, y su log y su consola sustituyen el ID de la hoja y la ruta de la credencial por `<id-de-hoja>` y `<credencial>`.
- **`cuentas.yaml`:** copia diaria `backups\cuentas_AAAAMMDD.yaml` (rotan solo las diarias), `fsync` antes de reemplazar, y un fichero existente pero vacío o sin la clave `cuentas` es un error (no «cero cuentas»). La copia diaria de la SQLite se hace a un `.tmp` y se renombra.
- **Peticiones del CLI** (`forzar_sync`, `archivar_ciclo`): se borran antes de actuar y se rearman, con espera de 60 s, si la acción falla por algo transitorio; un rechazo de negocio no se repite.
- Lo que la revisión dejó SIN tocar, con su motivo, está en el INFORME (no incluido en la versión pública). Hace falta **reiniciar el demonio** para que rija.
