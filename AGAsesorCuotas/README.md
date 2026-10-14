# AG Asesor — Envío de correos a clientes

Aplicación de escritorio (Windows) para enviar correos personalizados a clientes a partir de
plantillas (cuotas mensuales, recordatorios, avisos…) desde Outlook clásico, con la cuenta de Outlook que se elija en la pestaña Configuración
(por ejemplo `joaquin@agasesor.com`).

Interfaz HTML local + backend Python/Flask servido con waitress en `127.0.0.1`, historial en SQLite
y empaquetado con PyInstaller mediante GitHub Actions.

## Estructura

```text
AGAsesorCuotas/
├── app.py                 # Arranque, servidor local, API y coordinación del envío
├── core.py                # Validación, importes en céntimos, periodos y plantillas (campos {…})
├── database.py            # SQLite: plantillas, clientes, tandas, historial, duplicados, recuperación
├── mailer.py              # Interfaz común de envío
├── outlook_sender.py      # Envío real con Outlook clásico (pywin32/COM, solo Windows)
├── mock_sender.py         # Envío SIMULADO para desarrollo y pruebas
├── templates/index.html
├── static/styles.css, app.js
├── static/fonts/           # Newsreader (titulares), licencia SIL OFL incluida
├── tests/                 # pytest (validación, plantillas, clientes, duplicados, seguridad, migración)
├── AGAsesorCuotas.spec    # Configuración de PyInstaller (--onedir)
├── build.bat              # Compilación local en Windows
└── .github/workflows/build-windows.yml
```

## Desarrollo en macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python app.py --simulado
```

Se abre `http://127.0.0.1:8765/`. Fuera de Windows la aplicación **siempre** usa el envío simulado,
muestra una franja amarilla «MODO SIMULADO» y guarda los datos en una base distinta (`app-simulado.db`).

Direcciones especiales para probar resultados en modo simulado:

| Correo de prueba          | Resultado simulado                                  |
|---------------------------|-----------------------------------------------------|
| `alguien+error@...`       | Error confirmado (no llega a Outlook)               |
| `alguien+incierto@...`    | Resultado no confirmado (requiere revisión)         |
| `alguien+caido@...`       | Outlook deja de responder y se detiene la tanda     |

## Compilación para Windows

**GitHub Actions** (recomendado desde Mac): suba el repositorio a GitHub; el flujo
`Compilar para Windows` instala las dependencias, ejecuta las pruebas, compila con PyInstaller,
arranca el `.exe` en modo simulado para comprobar que sirve la interfaz y publica el artefacto
`AGAsesorCuotas-windows-x64` (pestaña *Actions* → ejecución → *Artifacts*).

**Local en Windows**: ejecute `build.bat`. El resultado queda en `dist\AGAsesorCuotas\`.

Se distribuye la **carpeta completa** `AGAsesorCuotas\` (no solo el `.exe`). El usuario abre
`AGAsesorCuotas.exe`; se abre una ventana de consola (debe permanecer abierta) y el navegador.

## Uso

0. La primera vez, la aplicación abre la pestaña **Configuración**: elegir la cuenta de envío
   entre las cuentas configuradas en Outlook clásico y pulsar «Usar esta cuenta».
1. **Plantillas** → «Crear plantilla»: nombre, asunto y cuerpo. Los campos se escriben entre llaves:
   - `{nombre}` (nombre del cliente) y `{periodo}` (p. ej. «octubre de 2026») se rellenan solos.
   - `{importe}` se escribe en cada envío, se valida como importe (`1.234,56`) y se suma en el total.
   - Cualquier otro (`{fecha_limite}`, `{documento}`…) añade una columna que se rellena en cada envío.
   La vista previa muestra cómo quedará el correo. Hay una plantilla inicial, «Cuota mensual».
2. **Enviar**: elegir plantilla y periodo.
3. Marcar con la casilla los clientes a los que se envía y rellenar sus campos. «+ Añadir cliente»
   guarda un cliente nuevo (solo nombre y correo; queda marcado). Los clientes se pueden editar (✎)
   y eliminar (✕); el historial de envíos se conserva.
4. «Revisar y enviar…»: los datos que falten se marcan en rojo y no se puede continuar.
5. En la confirmación se ve el resumen, un ejemplo del correo y los avisos de duplicados.
   Hay que marcar «He comprobado…» para enviar.
6. Se envía un correo individual por cliente y se muestra el progreso y el resultado de cada uno.
7. En «Historial» se consultan todos los intentos (con la plantilla usada) y se resuelven los
   «No confirmado».

La selección y los valores escritos se guardan como borrador en el navegador hasta que se envían.

### Qué se guarda

- **Clientes**: nombre y correo (el correo no se puede repetir). Los importes y demás campos no se
  guardan en la ficha del cliente; solo quedan en el historial del envío en el que se usaron.
- **Plantillas**: al eliminar una plantilla deja de aparecer, pero el historial conserva su nombre.
- Cada tanda guarda una **copia de la plantilla** tal y como se confirmó: editar la plantilla después
  no cambia lo que se está enviando. Si se edita entre la revisión y el envío, se pide revisar de nuevo.
- Al actualizar desde la versión 1.0, los destinatarios del historial se convierten en clientes y la
  plantilla de cuotas pasa a llamarse «Cuota mensual», manteniendo el control de duplicados.

## Estados y prevención de duplicados

| Estado                 | Significado                                                       | ¿Se puede volver a enviar (mismo correo y periodo)? |
|------------------------|-------------------------------------------------------------------|------------------------------------------|
| Pendiente / En proceso | Tanda en curso                                                    | No                                        |
| Aceptado por Outlook   | Outlook aceptó la orden (no garantiza la entrega)                 | Solo marcando «reenviar intencionadamente» |
| Error                  | Fallo confirmado **antes** de entregar el mensaje a Outlook       | Sí                                        |
| No confirmado          | Fallo durante la entrega o cierre inesperado: pudo haberse enviado | No, hasta revisarlo en el Historial      |

- Clave lógica: correo normalizado (minúsculas) + periodo (`AAAA-MM`) + plantilla. La misma plantilla
  no se envía dos veces al mismo cliente en el mismo periodo sin confirmarlo; otra plantilla sí.
- La comprobación se repite dentro de la transacción que crea la tanda; solo puede haber una tanda
  enviándose a la vez.
- Cada destinatario se guarda como «En proceso» **antes** de llamar a Outlook. Si la aplicación se
  cierra a mitad, al volver a abrirla ese destinatario pasa a «No confirmado» y los que no se
  llegaron a intentar pasan a «Error» (se pueden reenviar sin riesgo).
- El texto del correo se genera a partir de los datos guardados, de modo que lo registrado es lo enviado.
- Si Outlook deja de estar disponible a mitad de tanda, se detiene y el resto queda como «Error».

## Seguridad

- Escucha solo en `127.0.0.1` (servidor waitress, no el servidor de desarrollo de Flask).
- Las peticiones que modifican datos o envían correos exigen un token aleatorio por ejecución
  (cabecera `X-CSRF-Token`), `Content-Type: application/json` y origen local; se rechazan cabeceras
  `Host` no locales (protección frente a *DNS rebinding*). CSP estricta, sin scripts en línea.
- No hay contraseñas en el código: se usa el perfil de Outlook de Windows. Antes de cada tanda se
  verifica que la cuenta elegida sigue configurada en Outlook, y en cada mensaje que es el
  remitente; si no, no se envía. La cuenta queda fijada al crear la tanda y se guarda en el historial.
- Los errores internos no se muestran en la interfaz; el registro (`agasesor.log`) no incluye
  nombres, correos ni importes.
- Datos en `%LOCALAPPDATA%\AGAsesorCuotas\` (carpeta del usuario de Windows), fuera del ejecutable,
  por lo que no se pierden al actualizar. Copia de seguridad automática al arrancar en `copias\`
  (se conservan las 30 últimas).
- `.gitignore` excluye bases de datos, registros y configuración: no se suben datos de clientes.

## Configuración

La cuenta de envío se elige desde la propia aplicación (pestaña **Configuración**), que lista las
cuentas de Outlook clásico del equipo. Solo se puede elegir una cuenta que exista en Outlook, y no se
puede cambiar mientras se envía una tanda. Sin cuenta elegida, el envío está bloqueado.

La elección se guarda en `%LOCALAPPDATA%\AGAsesorCuotas\config.json`:

```json
{ "sender": "joaquin@agasesor.com", "port": 8765 }
```

En modo simulado, Outlook «tiene» dos cuentas ficticias: `joaquin@agasesor.com` y
`administracion@agasesor.com`.

## Pruebas de aceptación en el equipo del cliente (pendiente)

La parte de Outlook no puede probarse en macOS ni en GitHub Actions. Antes del uso real:

1. Comprobar que el cliente usa **Outlook clásico** (el nuevo Outlook no admite COM) y la
   versión/arquitectura de Windows (el ejecutable x64 también funciona en Windows 11 ARM por emulación).
2. Abrir Outlook, abrir la aplicación, comprobar que en Configuración aparecen las cuentas del
   equipo, elegir la corporativa y verificar que el indicador superior la muestra en verde.
3. Enviar una tanda de 1–2 clientes a direcciones propias autorizadas y comprobar en el destino:
   remitente, asunto, cuerpo, importe y que cada mensaje lleva un único destinatario.
4. Probar con Outlook cerrado, con una dirección inválida y cerrando la aplicación a mitad de una tanda.
5. Si Outlook muestra avisos de seguridad sobre «un programa intenta enviar correo», revisar con el
   administrador el antivirus o la directiva de acceso programático.

## Decisiones tomadas en esta versión

- Importes con coma o punto decimal y puntos de miles (`1.234,56`); máximo 2 decimales, sin redondeo.
- Periodo en minúsculas: «octubre de 2026».
- Plantilla en texto plano, editable en `core.py` (`SUBJECT_TEMPLATE`, `BODY_TEMPLATE`).
- El identificador del cliente es su correo electrónico (no hay ficha de clientes en v1).
- Los clientes no se guardan como ficha permanente; se pueden recuperar de la última tanda.

Siguen abiertas las decisiones del documento de proyecto (importación desde Excel, política de
retención de datos/RGPD, distribución y actualizaciones).
