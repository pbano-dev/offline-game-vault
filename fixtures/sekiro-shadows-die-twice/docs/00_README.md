# SEKIRO: SHADOWS DIE TWICE — Paquete de Archivo Digital

**Edición fuente:** compra digital en Steam (copia legítima, cuenta propia)  
**Versión preservada:** 1.06  
**AppID de Steam:** 814380  
**Capsule ID canónico:** `steam-814380-sekiro-shadows-die-twice-1.06`  
**Fecha de creación y verificación inicial:** 2026-07-18  
**Entorno validado:** Fedora Atomic/Silverblue, Bottles Flatpak, runner `ge-proton11-1`  
**Preservado y empaquetado por:** itsNeverwinter2, julio de 2026 *(autoría del paquete, no del juego; ver `PRESERVADO_POR.md`)*  
**Propósito:** copia personal, jugable, verificable y restaurable de *Sekiro: Shadows Die Twice*, desacoplada del cliente de Steam y acompañada por sus originales, estado persistente, documentación, evidencia y artbook.

Si estás leyendo esto dentro de muchos años y no recuerdas nada del proceso, no pasa nada: ése es exactamente el motivo por el que existe este documento. Léelo entero antes de modificar, restaurar o exportar la colección.

Este README forma parte de una colección canónica común. Ya no describe una carpeta monolítica ni una secuencia de clics como autoridad permanente. La autoridad es, por este orden:

1. el árbol real;
2. `COLLECTION_SHA256.txt` y los hashes de los objetos;
3. `capsule.json`, los contratos y los receipts;
4. el estado aceptado;
5. esta documentación.

La copia preservada usa dos operaciones técnicas distintas y no deben confundirse:

```text
SteamStub
→ retirado del ejecutable derivado mediante Steamless

Steamworks
→ reimplementado localmente mediante gbe_fork
```

El ejecutable original y la DLL original de Steamworks permanecen conservados.

---

## 1. Qué es esto y por qué existe

La copia distribuida por Steam dependía de dos capas relevantes para el arranque preservado:

- **SteamStub**, aplicado al ejecutable original;
- **Steamworks**, consultado mediante `steam_api64.dll`.

La cápsula retira esas dependencias de forma separada:

- el ejecutable preparado `sekiro.exe.unpacked.exe` procede de retirar SteamStub con Steamless;
- `steam_api64.dll` fue sustituida por una compilación identificada de **gbe_fork**, que responde localmente a las llamadas de Steamworks;
- `sekiro_ORIGINAL.exe` y la DLL original de Steamworks se conservan para auditoría y reversión.

En la copia preservada no se identificó un anti-cheat imprescindible para el punto de entrada aceptado. Esta afirmación se limita a la cápsula y a la versión preservada; no es una declaración universal sobre cualquier build futura.

La copia de tienda dependía de que el cliente, la cuenta y el servicio continuasen disponibles. Este paquete separa la supervivencia de la obra de esas dependencias, sin eludir la compra ni añadir contenido ajeno.

### Qué no es

No es:

- una distribución pública del juego;
- una sustitución de la licencia;
- una garantía de compatibilidad durante veinte años;
- una máquina virtual de seguridad;
- una prueba de que todas las vías de restauración funcionan;
- una cápsula inmutable que deba contener partidas futuras.

Es una colección personal de preservación basada en una copia adquirida legítimamente.

### La versión 1.06 importa

La versión 1.06 fue observada durante las pruebas funcionales y durante la restauración limpia.

No sustituyas el ejecutable preparado por uno de otra versión ni hagas *downpatch* sobre la línea base aceptada. Aunque una partida pueda ser técnicamente compatible entre determinadas revisiones, esa compatibilidad no se ha usado como fundamento del paquete. La combinación aceptada es:

```text
versión:
1.06

ejecutable:
drive_c/Games/Sekiro/sekiro.exe.unpacked.exe

estado:
03_PERSISTENT_STATE/steam-814380-sekiro-shadows-die-twice-1.06/accepted/
```

### Funciones online

La campaña y una partida existente fueron validadas offline.

Las funciones conectadas, incluidos los Remnants y cualquier servicio que dependa de infraestructura externa, no forman parte del objetivo funcional preservado. Que una función conectada deje de existir no invalida la campaña offline ni constituye corrupción del archivo.

---

## 2. Estructura canónica de la colección

Todas las rutas son relativas a `<OGV_ROOT>`.

```text
<OGV_ROOT>/
│
├── 00_README.md
├── INDEX.json
├── COLLECTION_LAYOUT.json
├── COLLECTION_SHA256.txt
│
├── 01_IMMUTABLE_VAULT/
│   ├── VAULT_INVENTORY.json
│   └── objects/sha256/
│       ├── 62/fa/62fa51a9a55ed445cb44d6ea7451bc4b8dc2dcc09d4ebb7b6ad08e5a49d77662
│       │   └── Sekiro Full Archive — objeto inmutable
│       └── 37/82/37820bc84240d5b786b5d2324fdb8c96c8328f4ce320560a1c2f47859fbe13fc
│           └── ge-proton11-1.tar.gz — runner compartido e inmutable
│
├── 02_CAPSULES/
│   └── steam-814380-sekiro-shadows-die-twice-1.06/
│       ├── capsule.json
│       ├── PROVENANCE.json
│       ├── CONTENT_STATUS.json
│       ├── docs/
│       │   ├── 00_README.md
│       │   ├── FICHA_DEL_JUEGO.md
│       │   ├── CREDITOS.md
│       │   └── PRESERVADO_POR.md
│       ├── host-contracts/
│       │   └── linux-bottles.json
│       ├── evidence/
│       │   ├── acceptance-summary.json
│       │   ├── appmanifest-summary.json
│       │   ├── artbook-summary.json
│       │   ├── documentation-summary.json
│       │   └── technical-preservation-summary.json
│       ├── public-fixture/
│       │   ├── README.md
│       │   ├── acceptance.json
│       │   └── capsule.json
│       └── supplemental-content/
│           └── artbook/
│               ├── COMO_SE_EXTRAJO.md
│               ├── informe_contenedor.md
│               ├── inventario.csv
│               └── content/
│                   ├── Sekiro_Digital_Artbook_EN.pdf
│                   ├── Sekiro_Digital_Artbook_JPG_4K.zip
│                   └── Sekiro_46_Illustrations_Ampliables_PNG.zip
│
├── 03_PERSISTENT_STATE/
│   └── steam-814380-sekiro-shadows-die-twice-1.06/
│       ├── accepted/
│       ├── snapshots/
│       ├── history/
│       └── accepted-provenance.json
│
├── 04_RECEIPTS/
│   └── steam-814380-sekiro-shadows-die-twice-1.06/
│       ├── acceptance/
│       ├── audits/
│       ├── migrations/
│       ├── operations/
│       └── repairs/
│
├── 05_PRIVATE_WORKSPACES/
│   └── steam-814380-sekiro-shadows-die-twice-1.06/
│       ├── README.md
│       └── archived/
│
├── 06_DERIVED_MATERIALIZATIONS/
│   └── steam-814380-sekiro-shadows-die-twice-1.06/
│       ├── README.md
│       ├── current/
│       └── history/
│
└── 07_EXPORTS/
    └── steam-814380-sekiro-shadows-die-twice-1.06/
        ├── README.md
        ├── portable/
        └── removable-media/
```

Los directorios vacíos son deliberados. Reservan el mismo contrato estructural para todos los juegos. Su existencia se registra en `COLLECTION_LAYOUT.json`; los hashes corresponden a archivos y se registran en `COLLECTION_SHA256.txt`.

| Ruta | Qué contiene | Función |
|---|---|---|
| `01_IMMUTABLE_VAULT/objects/sha256/62/fa/62fa51…` | Full Archive de Bottles | Autoridad inmutable del juego, prefix inicial y originales |
| `01_IMMUTABLE_VAULT/objects/sha256/37/82/37820b…` | Runner `ge-proton11-1` | Motor exacto separado del Full Archive |
| `02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/` | Contrato, documentación, evidencia y artbook | Describe cómo interpretar y restaurar los objetos |
| `03_PERSISTENT_STATE/.../accepted/` | Partida e identidad gbe aceptadas | Estado mutable y restaurable |
| `04_RECEIPTS/.../` | Aceptaciones, auditorías y operaciones | Evidencia de lo realizado |
| `05_PRIVATE_WORKSPACES/.../` | Trabajo privado saneado o archivado | No es la autoridad jugable |
| `06_DERIVED_MATERIALIZATIONS/.../` | Árboles reconstruibles | Pueden limpiarse después de conservar el estado |
| `07_EXPORTS/.../` | Copias para transporte o soportes | Deben verificarse como unidades independientes |

El objeto original, la cápsula jugable derivada, el estado persistente, el fixture público y el artbook son capas distintas. No deben volver a mezclarse en una única carpeta monolítica.

### Fixture público

`public-fixture/` contiene únicamente metadatos saneados:

```text
README.md
acceptance.json
capsule.json
```

El fixture es **válido pero no operacional por diseño**. Omite payload, runner, partida, identidad, binarios y artbook, y mantiene redactada la ruta privada del save.

Sus dos advertencias esperadas son:

```text
SANITIZED_FIXTURE
UNRESOLVED_STATE_PATH
```

La cápsula privada es la autoridad operacional.

---

## 3. Qué es exactamente el objeto Full Archive

El objeto:

```text
01_IMMUTABLE_VAULT/objects/sha256/62/fa/
62fa51a9a55ed445cb44d6ea7451bc4b8dc2dcc09d4ebb7b6ad08e5a49d77662
```

es la exportación **Full Archive** de Bottles.

Contiene un prefix de Wine con:

- `drive_c/`, donde está la instalación Windows;
- `system.reg`, `user.reg` y `userdef.reg`;
- `bottle.yml`, con la receta del entorno;
- las dependencias instaladas;
- el ejecutable original;
- el ejecutable derivado;
- la DLL original de Steamworks;
- la DLL activa de gbe_fork;
- la configuración local de Steamworks;
- el contenido fuente de la compra que quedó dentro de la instalación.

No es un formato mágico que convierta el juego a Linux. Los `.exe` y `.dll` siguen siendo binarios de Windows. Bottles aporta gestión; Wine o GE-Proton aportan ejecución.

### Configuración preservada de la bottle

La configuración documentada para esta cápsula es:

```text
Runner:
ge-proton11-1

Windows emulado:
Windows 10

Arquitectura:
win64

DXVK:
dxvk-3.0.1
activado

VKD3D:
vkd3d-proton-3.0.1-3-074c5b6
activado

NVAPI:
dxvk-nvapi-v0.9.2
instalado, desactivado

Sincronización:
ntsync

GPU discreta:
sí

Overrides de DLL:
ninguno

Dependencias:
d3dx9
msls31
arial32
times32
courie32
d3dcompiler_43
d3dcompiler_47
mono
gecko
```

Dos campos de Bottles pueden confundir:

- `use_eac_runtime: true` y `use_be_runtime: true` pueden aparecer como valores generales; no prueban que esta cápsula necesite EAC o BattlEye;
- que el sandbox propio de Bottles esté desactivado no implica ausencia de aislamiento por otros medios.

### El Full Archive no es el estado vivo

Aunque el prefix inicial contenga un perfil de usuario, la autoridad de la partida y de la identidad aceptadas está en:

```text
03_PERSISTENT_STATE/steam-814380-sekiro-shadows-die-twice-1.06/accepted/
```

No modifiques el objeto para incorporar una partida posterior. Conserva el estado mediante una operación de backup y genera un receipt.

---

## 4. LEE ESTO ANTES QUE NADA: el runner no viaja dentro del Full Archive

Bottles exporta el prefix y su receta, pero no garantiza incluir el runner que ejecuta esa receta.

La colección conserva dos objetos separados:

```text
Full Archive Sekiro

SHA-256:
62fa51a9a55ed445cb44d6ea7451bc4b8dc2dcc09d4ebb7b6ad08e5a49d77662

Tamaño:
15987727148 bytes
```

```text
Runner ge-proton11-1

SHA-256:
37820bc84240d5b786b5d2324fdb8c96c8328f4ce320560a1c2f47859fbe13fc

Tamaño:
528930444 bytes
```

El flujo normal es dejar que Offline Game Vault materialice ambos objetos conforme a `capsule.json` y `host-contracts/linux-bottles.json`.

No renombres ni edites los objetos dentro de `01_IMMUTABLE_VAULT`.

### Recuperación manual si Offline Game Vault ya no existe

Trabaja fuera del vault:

```bash
SEKIRO_OBJECT="<OGV_ROOT>/01_IMMUTABLE_VAULT/objects/sha256/62/fa/62fa51a9a55ed445cb44d6ea7451bc4b8dc2dcc09d4ebb7b6ad08e5a49d77662"
RUNNER_OBJECT="<OGV_ROOT>/01_IMMUTABLE_VAULT/objects/sha256/37/82/37820bc84240d5b786b5d2324fdb8c96c8328f4ce320560a1c2f47859fbe13fc"

mkdir -p "$PWD/SekiroPrefix" "$PWD/SekiroRunner"

tar -xzf "$SEKIRO_OBJECT" -C "$PWD/SekiroPrefix"
tar -xzf "$RUNNER_OBJECT" -C "$PWD/SekiroRunner"
```

El Full Archive puede extraer un directorio raíz adicional. El prefix efectivo es el directorio que contiene:

```text
bottle.yml
drive_c/
system.reg
user.reg
```

Para una importación manual en Bottles, crea una copia derivada con nombre y extensión reconocibles:

```bash
cp --reflink=auto \
  "$SEKIRO_OBJECT" \
  "$PWD/Sekiro.FullArchive.tar.gz"
```

No cambies el objeto original.

### Instalación manual del runner en Bottles Flatpak

La ruta usada por la instalación de referencia era:

```bash
mkdir -p "$HOME/.var/app/com.usebottles.bottles/data/bottles/runners"

tar -xzf "$RUNNER_OBJECT" \
  -C "$HOME/.var/app/com.usebottles.bottles/data/bottles/runners/"
```

Cierra Bottles y vuelve a abrirlo.

Una instalación futura puede usar otra ruta. El destino conceptual sigue siendo el directorio de runners de esa instalación concreta. Verifica además los symlinks del runner después de extraerlo.

DXVK y VKD3D sí forman parte del prefix exportado. El runner es el componente separado.

---

## 5. Cómo arrancar el juego en Linux con Bottles

### Vía canónica

1. Verifica `COLLECTION_LAYOUT.json` y `COLLECTION_SHA256.txt`.
2. Lee:
   ```text
   02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/capsule.json
   ```
3. Usa:
   ```text
   02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/host-contracts/linux-bottles.json
   ```
4. Materializa el Full Archive y el runner fuera de `01_IMMUTABLE_VAULT`.
5. Despliega la bottle derivada.
6. Verifica o restaura el estado aceptado.
7. Lanza:
   ```text
   drive_c/Games/Sekiro/sekiro.exe.unpacked.exe
   ```
8. Antes de limpiar, conserva y verifica cualquier estado nuevo.

La interfaz puede ser la CLI, un wrapper o la futura GUI. La lógica debe seguir siendo la misma y la autoridad debe permanecer en la cápsula, los contratos y los receipts.

### Los dos ejecutables

| Archivo | Función | Uso |
|---|---|---|
| `sekiro.exe.unpacked.exe` | Ejecutable derivado al retirar SteamStub | **Punto de entrada aceptado** |
| `sekiro_ORIGINAL.exe` | Ejecutable original de Steam con SteamStub | Conservar; no usar para la cápsula offline |

Identidades conservadas:

```text
Ejecutable original:
637aca527538c0ec6e1f136c8ed66046e95dfbdbb1f51926e134d9916398b856

Ejecutable derivado:
189b2fed665473c565d983a01c5af87f80d15e5446a74262801077fb1a6fd17c
```

No renombres el derivado a ciegas ni sustituyas el original. El nombre feo del ejecutable derivado conserva trazabilidad sobre su origen.

### Resultado funcional validado

En la restauración limpia se verificó:

```text
versión 1.06 observada:
sí

estado aceptado restaurado:
sí

partida existente cargada:
sí

gameplay:
funcional

cierre normal desde el juego:
sí

Bottles terminó por sí mismo:
sí

procesos relacionados después:
0
```

El código exacto del proceso hijo de `run-bottles` no quedó persistido. No debe inventarse ni sustituirse por el código de una sesión anterior terminada manualmente.

### Recuperación manual con Bottles

1. Instala el runner de la sección 4.
2. Crea una copia derivada del Full Archive con extensión `.tar.gz`.
3. En Bottles, importa un **Full Archive**, no una configuración parcial.
4. Entra en la bottle importada.
5. Ejecuta:
   ```text
   drive_c\Games\Sekiro\sekiro.exe.unpacked.exe
   ```
6. Restaura la partida y la identidad si la materialización no contiene el estado aceptado.
7. Conserva el estado antes de eliminar la bottle.

Si Flatpak no ve el directorio de la copia derivada, concede acceso únicamente a esa ubicación. Un permiso global a todo `$HOME` funciona, pero amplía innecesariamente el acceso.

---

## 6. Cómo arrancar el juego en Linux sin Bottles

La colección no declara actualmente un contrato `linux-direct-wine.json` para esta cápsula y esta vía no tiene una aceptación funcional cerrada.

Lo siguiente es un procedimiento de recuperación orientativo, no una garantía.

```bash
SEKIRO_OBJECT="<OGV_ROOT>/01_IMMUTABLE_VAULT/objects/sha256/62/fa/62fa51a9a55ed445cb44d6ea7451bc4b8dc2dcc09d4ebb7b6ad08e5a49d77662"
RUNNER_OBJECT="<OGV_ROOT>/01_IMMUTABLE_VAULT/objects/sha256/37/82/37820bc84240d5b786b5d2324fdb8c96c8328f4ce320560a1c2f47859fbe13fc"

mkdir -p "$PWD/SekiroPrefix" "$PWD/SekiroRunner"

tar -xzf "$SEKIRO_OBJECT" -C "$PWD/SekiroPrefix"
tar -xzf "$RUNNER_OBJECT" -C "$PWD/SekiroRunner"
```

Localiza `<PREFIX>` y `<RUNNER>` después de extraer.

Lanzamiento orientativo:

```bash
export WINEPREFIX="<PREFIX>"

"<RUNNER>/files/bin/wine" \
  "$WINEPREFIX/drive_c/Games/Sekiro/sekiro.exe.unpacked.exe"
```

La versión archivada de GE-Proton puede depender de bibliotecas, kernel o mecanismos de sincronización que no existan en un Linux futuro.

Orden razonable de diagnóstico:

1. comprobar que `<PREFIX>` es correcto;
2. comprobar que el runner conserva symlinks y permisos;
3. probar `ntsync`;
4. probar `fsync`;
5. probar `esync`;
6. comprobar DXVK y VKD3D;
7. probar Wine del sistema sobre una copia del prefix.

Cualquier adaptación funcional debe registrarse como una materialización derivada y no debe reemplazar el objeto inmutable.

Esta vía permanece no probada hasta que exista un contrato, una ejecución, una partida cargada, un cierre normal y un receipt.

---

## 7. Cómo recuperar la carga en Windows nativo

No existe actualmente un contrato `windows-native.json` aceptado para esta cápsula. Esta sección conserva el procedimiento técnico, pero no demuestra que haya sido ejecutado en Windows.

### Qué copiar

Del Full Archive hacen falta tres conjuntos:

| Desde `drive_c` | Destino orientativo en Windows |
|---|---|
| `drive_c\Games\Sekiro\` | `C:\Games\Sekiro\` o una ubicación equivalente |
| `drive_c\users\steamuser\AppData\Roaming\GSE Saves\` | `%APPDATA%\GSE Saves\` |
| `drive_c\users\steamuser\AppData\Roaming\Sekiro\<PRIVATE_SAVE_IDENTIFIER>\` | `%APPDATA%\Sekiro\<PRIVATE_SAVE_IDENTIFIER>\` |

No copies el resto de `drive_c`. El registro de Wine, `system32`, `syswow64` y los componentes del prefix no sustituyen a Windows.

La partida y la identidad forman una unidad:

```text
%APPDATA%\Sekiro\<PRIVATE_SAVE_IDENTIFIER>\S0000.sl2
%APPDATA%\GSE Saves\settings\configs.user.ini
```

El identificador privado exacto no se publica en este README.

### Ejecutable correcto

Ejecuta:

```text
sekiro.exe.unpacked.exe
```

No ejecutes:

```text
sekiro_ORIGINAL.exe
```

Junto al ejecutable preparado deben seguir presentes:

```text
steam_api64.dll
steam_settings/
```

La identidad que vincula la partida no vive únicamente en `steam_settings/`; debe restaurarse también `configs.user.ini`.

### DLL originales del juego y DLL emulada

La documentación histórica de la copia registró estas DLL junto al juego:

| DLL | Función | Conservar al copiar a Windows |
|---|---|---|
| `oo2core_6_win64.dll` | Oodle, descompresión de assets | Sí |
| `bink2w64.dll` | Vídeo Bink 2 | Sí |
| `fmodex64.dll` | Audio FMOD | Sí |
| `fmod_event64.dll` | Eventos FMOD | Sí |
| `fmod_event_net64.dll` | Eventos FMOD | Sí |
| `amd_ags_x64.dll` | AMD GPU Services usado por el juego | Sí |
| `steam_api64.dll` | gbe_fork activo | Sí |

No borres `amd_ags_x64.dll` por usar una GPU de otra marca y no borres `oo2core_6_win64.dll`.

### DLL de traducción que no deben acompañar al juego en Windows

DXVK y VKD3D pertenecen al entorno Linux. Si aparecen junto al ejecutable en una copia derivada para Windows, revisa y retira únicamente las DLL de traducción:

```text
d3d9.dll
d3d10.dll
d3d10core.dll
d3d11.dll
d3d12.dll
d3d12core.dll
dxgi.dll
```

No las retires del prefix Linux y no borres DLL del juego por similitud de nombre.

### Dependencias de Windows

En un Windows limpio pueden hacer falta componentes oficiales de Microsoft:

- DirectX End-User Runtime para bibliotecas heredadas;
- Visual C++ Redistributable cuando falte un `msvcp` o `vcruntime`.

No archives instaladores descargados posteriormente como si fueran parte de la copia original sin registrar su procedencia y hash.

### Compatibilidad futura

Si un Windows futuro no lanza el juego:

1. trabaja sobre una copia;
2. prueba los modos de compatibilidad de Windows;
3. comprueba primero el ejecutable correcto;
4. comprueba la DLL activa;
5. comprueba la identidad de la partida;
6. registra cada cambio.

La carga es software nativo de Windows, pero esta vía sigue sin prueba funcional cerrada. Tampoco se ha validado la portabilidad entre usuarios o anfitriones.

---

## 8. Contenido adicional: artbook digital

El artbook es una excepción específica de esta cápsula. No se convierte en requisito universal para otros juegos.

Ruta canónica:

```text
02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/
supplemental-content/artbook/
```

Inventario registrado:

| Archivo | Clasificación | Tamaño | SHA-256 |
|---|---|---:|---|
| `COMO_SE_EXTRAJO.md` | Método de extracción | 44252 | `aede005e53cf8f15ec309c44598523df4b02266ea04b70210afca0d2e03367a3` |
| `content/Sekiro_46_Illustrations_Ampliables_PNG.zip` | Derivado PNG | 243251186 | `003a1d75156c6f6761b36fe5ffe4fc3385046a632c6d8c5a066b5d6788886089` |
| `content/Sekiro_Digital_Artbook_EN.pdf` | PDF original registrado | 43914479 | `c5bae9e19fa67ddcab3fda959881d1e462f4ada279d5d6e7802ba797708bdf3c` |
| `content/Sekiro_Digital_Artbook_JPG_4K.zip` | Derivado JPG 4K | 43903776 | `abf6bf3d9c3ff6fc755a8e6b82bf68bb71fba349f0c3c4e5a0d60441784e0ec8` |
| `informe_contenedor.md` | Informe técnico | 6236 | `a979de378165b5c51e9b8eaa8181e796ca6c43efb7c5f88a0c1d3a070a1b9ecf` |
| `inventario.csv` | Inventario interno | 7894 | `42f3ffaa01de948bfc7f61dd618f21ab1b69ef24594a5606db917eb5abdb5c7a` |

Total registrado:

```text
6 archivos
331127823 bytes
```

La autoridad resumida es:

```text
evidence/artbook-summary.json
```

### Original y derivados

Los ZIP de JPG y PNG son derivados de acceso y no sustituyen al PDF registrado como original dentro del conjunto suplementario.

La documentación histórica del paquete describe además una extracción desde el contenedor de bonus incluido con la compra. El Full Archive conserva la instalación original como autoridad de la carga de tienda. Para afirmar reproducibilidad completa desde ese contenedor hay que seguir `COMO_SE_EXTRAJO.md` y contrastar los resultados con `inventario.csv`.

### Música

El árbol canónico `supplemental-content/artbook/` no afirma incluir una banda sonora. No mezcles automáticamente música y artbook ni presentes una omisión deliberada como pérdida.

Si se preserva música por separado, debe tener su propio inventario, procedencia, hashes y alcance.

### Fixture público

El artbook no forma parte del fixture público. El fixture contiene solo metadatos saneados y no distribuye este contenido.

---

## 9. Rutas importantes y estado persistente

Las rutas siguientes son internas a una materialización del Full Archive.

| Componente | Ruta |
|---|---|
| Juego | `drive_c/Games/Sekiro/` |
| Ejecutable aceptado | `drive_c/Games/Sekiro/sekiro.exe.unpacked.exe` |
| Ejecutable original | `drive_c/Games/Sekiro/sekiro_ORIGINAL.exe` |
| Partida | `drive_c/users/steamuser/AppData/Roaming/Sekiro/<PRIVATE_SAVE_IDENTIFIER>/S0000.sl2` |
| Identidad gbe | `drive_c/users/steamuser/AppData/Roaming/GSE Saves/settings/configs.user.ini` |
| Configuración gbe | `drive_c/Games/Sekiro/steam_settings/` |
| DLL gbe activa | `drive_c/Games/Sekiro/steam_api64.dll` |
| DLL Steamworks original | `drive_c/Games/Sekiro/steam_api64.dll.gbe_backup` |

### AppID e identidad de partida no son lo mismo

```text
AppID:
814380
```

identifica el juego.

```text
<PRIVATE_SAVE_IDENTIFIER>
```

identifica el espacio de partida asociado al usuario preservado.

No publiques ni cambies el identificador privado por comodidad. La ruta pública del fixture usa:

```text
ACCOUNT_ID_REDACTED
```

porque el fixture no es la cápsula operacional.

### Estado aceptado

Ruta:

```text
03_PERSISTENT_STATE/steam-814380-sekiro-shadows-die-twice-1.06/accepted/
```

Elementos declarados:

```text
sekiro-save
gbe-user-identity
```

Resultado verificado:

```text
presentes:
2

ausentes:
0

backup:
completo y verificado
```

La partida y `configs.user.ini` deben restaurarse juntas. Copiar sólo `S0000.sl2` puede producir una partida invisible o un perfil nuevo.

### Historial y snapshots

`history/` conserva backups anteriores aceptados o recuperados.

`snapshots/` conserva instantáneas previas a restauraciones. Una instantánea previa no sustituye al estado aceptado, pero permite rollback y trazabilidad.

### Resultado de restauración

La restauración limpia verificó:

- materialización nueva;
- despliegue nuevo;
- snapshot obligatorio previo a restaurar;
- restauración del estado;
- carga de la partida;
- gameplay;
- cierre normal;
- ausencia de procesos relacionados al finalizar.

No borres una materialización con progreso nuevo sin ejecutar primero una operación de preservación de estado y verificarla.

---

## 10. Red: preservación e aislamiento son propiedades distintas

La cápsula está configurada para uso offline mediante gbe_fork.

Esto reduce dependencias, pero no es por sí solo una prueba independiente de que ningún proceso pueda alcanzar la red.

### Capa de aplicación

El juego y gbe_fork deben mantenerse en modo offline.

Objetivo:

- no consultar Steam;
- no depender de servicios;
- no esperar respuestas de red;
- degradar las funciones conectadas sin bloquear la campaña.

### Capa de aislamiento por sesión

El mecanismo previsto para Bottles Flatpak es:

```bash
flatpak run --unshare=network com.usebottles.bottles
```

Los procesos hijos heredan un namespace sin acceso exterior.

Este mecanismo no convierte una bottle en una VM y no protege frente a todos los riesgos de código hostil. Su función aquí es más estrecha: eliminar la ruta de red durante la sesión.

### Estado real de la prueba

Para esta cápsula están verificados:

```text
configuración offline:
sí

campaña y partida sin Steam:
sí

restauración limpia:
sí
```

No está cerrada una prueba independiente, registrada desde el interior del sandbox, que demuestre el bloqueo exterior de red para la sesión aceptada.

Por tanto:

- no se debe afirmar todavía “aislamiento exterior verificado”;
- sí se puede conservar el comando y la política como procedimiento;
- una prueba futura debe guardar un receipt saneado bajo `04_RECEIPTS/.../acceptance/` o `audits/`.

Un timeout online, un modo offline y un namespace de red son evidencias distintas.

---

## 11. Problemas conocidos y diagnóstico

### El juego pide Steam o se cierra al arrancar

Comprueba, por este orden:

1. que se lanzó `sekiro.exe.unpacked.exe`;
2. que no se lanzó `sekiro_ORIGINAL.exe`;
3. que existe `steam_api64.dll`;
4. que existe `steam_settings/`;
5. que la DLL activa conserva:
   ```text
   cf61e505e63852b24aefb9d9d0712bc1ae45921c7c3a0c01abb1dc0c95c8ca01
   ```

Si el árbol derivado fue alterado, restaura una materialización nueva. No repares el objeto inmutable.

### Bottles intenta descargar `ge-proton11-1`

El runner debe materializarse desde:

```text
01_IMMUTABLE_VAULT/objects/sha256/37/82/
37820bc84240d5b786b5d2324fdb8c96c8328f4ce320560a1c2f47859fbe13fc
```

No aceptes silenciosamente un runner distinto como equivalente.

### La partida no aparece

Comprueba juntas:

```text
AppData/Roaming/Sekiro/<PRIVATE_SAVE_IDENTIFIER>/S0000.sl2
AppData/Roaming/GSE Saves/settings/configs.user.ini
```

No generes una identidad nueva antes de comprobar el estado aceptado.

### El rendimiento es malo después de una restauración futura

Primer sospechoso:

```text
ntsync
```

Prueba sobre una copia:

1. `ntsync`;
2. `fsync`;
3. `esync`.

Después revisa DXVK, VKD3D y el runner.

Documenta cualquier cambio funcional como desviación del contrato original.

### Quiero volver a la copia que exige Steam

Hazlo sólo en una materialización derivada:

```bash
cd "<PREFIX>/drive_c/Games/Sekiro"

mv steam_api64.dll \
   steam_api64.dll.gbe

mv steam_api64.dll.gbe_backup \
   steam_api64.dll

cp sekiro_ORIGINAL.exe \
   sekiro.exe
```

Aparta `steam_settings/` sin borrarlo y conserva el ejecutable derivado.

La copia resultante volverá a depender de Steam. No sustituye a la cápsula offline aceptada.

### El artbook no abre

Verifica primero el hash del archivo concreto mediante `COLLECTION_SHA256.txt` y después consulta:

```text
evidence/artbook-summary.json
supplemental-content/artbook/COMO_SE_EXTRAJO.md
supplemental-content/artbook/informe_contenedor.md
```

No regenere derivados sobre el vault vivo. Trabaja en una copia y compara los resultados.

### He editado la documentación y el manifiesto ya no verifica

No regeneres el manifiesto para aceptar cualquier diferencia.

Usa el actualizador documental genérico:

```text
prepare
→ editar candidate/
→ validate
→ revisar plan
→ apply
```

Ese flujo:

- conserva `before/`;
- audita los cuatro documentos;
- comprueba referencias cruzadas;
- actualiza el resumen documental;
- actualiza status, provenance, index y layout;
- crea un receipt;
- regenera y verifica el manifiesto;
- hace rollback si falla.

La futura GUI debe utilizar el mismo motor.

### Registro vivo de problemas

Los problemas reales y sus soluciones deben quedar bajo:

```text
04_RECEIPTS/steam-814380-sekiro-shadows-die-twice-1.06/operations/
04_RECEIPTS/steam-814380-sekiro-shadows-die-twice-1.06/repairs/
```

Resume aquí únicamente lo que afecte a una restauración futura.

---

## 12. Procedencia de componentes modificados y originales

Esta sección existe para que una persona futura no tenga que confiar a ciegas en un ejecutable derivado o en una DLL compilada.

### gbe_fork

```text
Proyecto:
gbe_fork

Commit:
8ffe94aa1cbdb8410b97d33cff0d66974041c1c7

Perfil:
api_regular

DLL activa:
drive_c/Games/Sekiro/steam_api64.dll

SHA-256:
cf61e505e63852b24aefb9d9d0712bc1ae45921c7c3a0c01abb1dc0c95c8ca01
```

DLL original:

```text
drive_c/Games/Sekiro/steam_api64.dll.gbe_backup

SHA-256:
fc20547408a7c34f0bd4946a34c21aab48a75e3b98dce9e55969f486d37b212f
```

Steamworks/gbe_fork no es SteamStub/Steamless.

### Steamless y ejecutables

Original:

```text
drive_c/Games/Sekiro/sekiro_ORIGINAL.exe

SHA-256:
637aca527538c0ec6e1f136c8ed66046e95dfbdbb1f51926e134d9916398b856
```

Derivado:

```text
drive_c/Games/Sekiro/sekiro.exe.unpacked.exe

SHA-256:
189b2fed665473c565d983a01c5af87f80d15e5446a74262801077fb1a6fd17c
```

La versión exacta de Steamless no está registrada en el resumen técnico actual. No debe inventarse.

La relación demostrada es:

```text
original conservado
+
derivado identificado por SHA-256
+
procedimiento declarado:
retirada de SteamStub con Steamless
```

Para cerrar reproducibilidad completa harían falta además la versión exacta de la herramienta, su procedencia, su hash y una receta verificada.

### Reproducibilidad de gbe_fork

La documentación histórica describe un entorno `GBE-Standalone`, fuentes, toolchain y scripts de compilación.

El inventario inmutable actual de la colección contiene tres objetos grandes:

```text
Full Archive Sekiro
Full Archive del otro juego ya incorporado
runner compartido
```

No contiene todavía un objeto independiente para `GBE-Standalone` ni para un entorno de compilación.

Por tanto:

```text
identidad de la DLL:
verificada

procedencia documentada:
sí

recompilación offline a un comando desde objetos canónicos:
no cerrada
```

Los artefactos pequeños de receta deberían ingresar bajo:

```text
02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/evidence/gbe_fork/
```

Los entornos pesados deben convertirse en objetos inmutables con rol explícito.

### Evidencia técnica actual

```text
evidence/technical-preservation-summary.json
evidence/acceptance-summary.json
evidence/appmanifest-summary.json
```

El resumen técnico conserva:

- versión;
- hashes de ejecutables;
- hashes de DLL;
- identidad del runner;
- distinción SteamStub/Steamworks;
- estado persistente;
- limitaciones no probadas.

La aceptación conserva:

- versión 1.06 observada;
- restauración limpia;
- partida cargada;
- gameplay;
- cierre normal;
- salida autónoma de Bottles;
- estado final verificado;
- limitaciones.

### Auditoría de seguridad

La DLL activa coincide con una compilación usada y auditada en el archivo personal.

El informe completo de seguridad y un addendum específico deben importarse bajo:

```text
02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/evidence/security/
```

mientras no estén presentes, el README no debe afirmar que la colección canónica contiene esos documentos.

La coincidencia de SHA-256 permite reutilizar conclusiones sobre el mismo binario; no reemplaza una prueba funcional específica del juego.

### Appmanifest, depots y bonus

El `appmanifest_814380.acf` se conserva como evidencia externa, no como requisito del objeto jugable.

Resumen saneado:

```text
evidence/appmanifest-summary.json
```

Datos registrados:

```text
InstalledDepots:
814381
814382
814383

DLCAppID observado:
1039230
```

Los depots instalados prueban presencia de contenido en la biblioteca inspeccionada. No prueban por sí solos propiedad contractual de todos los DLC.

El artbook sí está físicamente presente, inventariado y hasheado en la colección.

### Resto de la carga

El resto de la instalación procede de la copia de Steam. Las modificaciones activas relevantes son:

```text
ejecutable derivado:
SteamStub retirado

steam_api64.dll:
Steamworks reimplementado localmente
```

Los originales desplazados se conservan.

---

## 13. Soportes físicos y exportación

Tamaños verificados:

| Componente | Tamaño |
|---|---:|
| Full Archive Sekiro | 15987727148 bytes |
| Runner compartido | 528930444 bytes |
| Total de objetos necesarios | 16516657592 bytes |
| Artbook suplementario registrado | 331127823 bytes |
| Objetos más artbook | 16847785415 bytes |
| Estado, documentos, evidencia y receipts | Deben medirse en la exportación concreta |

La cifra de una exportación no debe deducirse sólo de esta tabla. `07_EXPORTS/` puede añadir manifiestos, redundancia, documentación y otros artefactos.

### Ruta de exportación

```text
07_EXPORTS/steam-814380-sekiro-shadows-die-twice-1.06/
├── portable/
└── removable-media/
```

No generes una exportación dentro del vault inmutable y no fragmentes los objetos originales.

### Capacidad orientativa

Los dos objetos y el artbook suman menos de 25 GB decimales. En principio pueden caber en un BD-R de 25 GB, pero sólo una exportación terminada y medida confirma el encaje real, especialmente si incluye PAR2.

| Soporte | Uso | Riesgo o límite |
|---|---|---|
| BD-R 25 GB | Exportación compacta | Margen limitado para redundancia |
| BD-R DL 50 GB | Más espacio para PAR2 y documentación | Coste y compatibilidad |
| BD-R XL 100 GB | Varias cápsulas o generaciones | Coste |
| M-DISC | Archivo óptico | No elimina la necesidad de verificar |
| HDD externo | Copia completa y actualizable | Fallo mecánico y borrado accidental |
| SSD externo | Transporte y lectura rápida | Retención y coste |
| Varias partes | Cuando el soporte obliga | Una parte perdida invalida el conjunto |

### División de una exportación

Divide una copia de exportación, no el objeto del vault:

```bash
split -b 23G \
  "<EXPORT_ARCHIVE>" \
  "<EXPORT_ARCHIVE>.part-"

cat "<EXPORT_ARCHIVE>.part-"* \
  > "<EXPORT_ARCHIVE>.restored"
```

### PAR2

Genera paridad sobre el archivo final de exportación:

```bash
cd "<OGV_ROOT>/07_EXPORTS/steam-814380-sekiro-shadows-die-twice-1.06/removable-media"

par2 create -r10 -n1 \
  Sekiro.par2 \
  <EXPORT_ARCHIVE>

par2 verify Sekiro.par2
```

Registra:

- soporte;
- fecha;
- tamaño;
- SHA-256;
- porcentaje PAR2;
- resultado de lectura posterior.

Mantén al menos tres copias, en dos tipos de soporte o ubicaciones, con una fuera del equipo principal.

---

## 14. Verificación de integridad

Desde `<OGV_ROOT>`:

```bash
sha256sum -c COLLECTION_SHA256.txt
```

Ese comando verifica archivos, no directorios vacíos.

La forma canónica se verifica con:

```text
COLLECTION_LAYOUT.json
```

Identidades principales:

```text
Full Archive:
62fa51a9a55ed445cb44d6ea7451bc4b8dc2dcc09d4ebb7b6ad08e5a49d77662

Runner:
37820bc84240d5b786b5d2324fdb8c96c8328f4ce320560a1c2f47859fbe13fc

DLL gbe activa:
cf61e505e63852b24aefb9d9d0712bc1ae45921c7c3a0c01abb1dc0c95c8ca01

DLL Steamworks original:
fc20547408a7c34f0bd4946a34c21aab48a75e3b98dce9e55969f486d37b212f

Ejecutable original:
637aca527538c0ec6e1f136c8ed66046e95dfbdbb1f51926e134d9916398b856

Ejecutable derivado:
189b2fed665473c565d983a01c5af87f80d15e5446a74262801077fb1a6fd17c
```

### Cápsula privada

```bash
ogv audit-capsule \
  --capsule \
  "02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/capsule.json" \
  --json
```

Resultado esperado de la línea aceptada:

```text
valid:
true

operational:
true

errors:
0

persistent state declarations:
2
```

### Estado aceptado

```bash
ogv verify-state-backup \
  --capsule \
  "02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/capsule.json" \
  --backup \
  "03_PERSISTENT_STATE/steam-814380-sekiro-shadows-die-twice-1.06/accepted" \
  --json
```

Resultado esperado:

```text
verified:
true

present:
2

missing:
0
```

### Fixture público

El fixture ensamblado debe ser:

```text
valid:
true

operational:
false

errors:
0

warnings:
SANITIZED_FIXTURE
UNRESOLVED_STATE_PATH
```

No conviertas el fixture en operacional añadiendo partidas o identidades privadas.

### Actualizaciones documentales

La edición manual debe hacerse en un workspace externo:

```text
before/
candidate/
```

Después:

```text
validate
plan
apply
```

`apply` debe:

- volver a validar;
- comprobar que la línea base no cambió;
- hacer staging;
- preservar versiones anteriores;
- auditar cápsula y estado;
- actualizar metadatos derivados;
- regenerar el manifiesto;
- verificarlo;
- crear un receipt;
- hacer rollback ante un fallo.

Verificar un manifiesto anterior no registra un archivo nuevo. Regenerarlo sin explicar la diferencia tampoco es una auditoría.

### Verificación periódica

Verifica cada soporte y cada exportación por separado. El bit rot es silencioso y una copia no verificada es sólo una esperanza.

---

## 15. Estado, pendientes y regla de continuidad

Este documento debe leerse junto con:

```text
02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/capsule.json
02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/PROVENANCE.json
02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/CONTENT_STATUS.json
02_CAPSULES/steam-814380-sekiro-shadows-die-twice-1.06/evidence/
03_PERSISTENT_STATE/steam-814380-sekiro-shadows-die-twice-1.06/
04_RECEIPTS/steam-814380-sekiro-shadows-die-twice-1.06/
```

Si esta documentación contradice el árbol, los hashes, la cápsula o un receipt, manda la evidencia real.

### Verificado

- [x] Compra legítima en Steam.
- [x] AppID 814380.
- [x] Versión 1.06 observada.
- [x] Full Archive ingresado como objeto inmutable.
- [x] Full Archive verificado por SHA-256.
- [x] Runner exacto ingresado por separado.
- [x] Runner verificado por SHA-256.
- [x] Cápsula privada válida y operacional.
- [x] Contrato Bottles presente.
- [x] SteamStub retirado del ejecutable derivado.
- [x] Ejecutable original conservado.
- [x] Ejecutable derivado identificado por SHA-256.
- [x] Steamworks reimplementado con gbe_fork.
- [x] DLL original de Steamworks conservada.
- [x] DLL activa identificada por SHA-256.
- [x] Partida existente cargada.
- [x] Gameplay funcional.
- [x] Estado aceptado con partida e identidad.
- [x] Dos elementos de estado presentes.
- [x] Restauración limpia realizada.
- [x] Snapshot previo a restauración creado.
- [x] Cierre normal desde el juego.
- [x] Bottles terminó por sí mismo.
- [x] Cero procesos relacionados después de la sesión.
- [x] Artbook registrado: seis archivos.
- [x] Artbook inventariado por tamaño y SHA-256.
- [x] Documentación canónica presente.
- [x] Fixture público de tres archivos presente.
- [x] Fixture público sin payload, partida, identidad, runner, binarios ni artbook.
- [x] Fixture público válido y no operacional por diseño.
- [x] Resumen técnico saneado presente.
- [x] Resumen de aceptación saneado presente.
- [x] Estructura común con directorios vacíos deliberados.
- [x] Manifiesto regenerado y verificado después del fixture y la evidencia.

### Limitaciones declaradas

- el código exacto del proceso hijo de `run-bottles` no quedó registrado;
- el aislamiento exterior independiente no está cerrado;
- Wine directo no está probado;
- Windows nativo no está probado;
- otro usuario u otro anfitrión no están probados;
- la versión exacta de Steamless no está documentada en el resumen técnico;
- la recompilación offline completa de gbe_fork no está cerrada desde objetos canónicos;
- la garantía de funcionamiento dentro de diez o veinte años no puede probarse hoy.

### Pendiente antes del cierre final de la colección

- [ ] Aplicar este README mediante el actualizador documental genérico.
- [ ] Auditar los cuatro documentos juntos.
- [ ] Actualizar hashes y metadatos derivados mediante la transacción documental.
- [ ] Exportar el fixture repo-ready al repositorio.
- [ ] Añadir y ejecutar las pruebas de CI del fixture.
- [ ] Importar el informe completo de seguridad y su addendum, si se conservan.
- [ ] Incorporar recetas y fuentes de gbe_fork si se quiere cerrar recompilación offline.
- [ ] Registrar versión, procedencia y hash de Steamless si se quiere cerrar esa derivación.
- [ ] Probar formalmente el namespace de red desde dentro de la sesión.
- [ ] Probar Wine directo o mantenerlo explícitamente no probado.
- [ ] Probar Windows nativo o mantenerlo explícitamente no probado.
- [ ] Probar otro usuario y otro anfitrión.
- [ ] Ejecutar auditoría final de privacidad.
- [ ] Ejecutar verificación final completa de la colección.
- [ ] Generar una exportación portable o para soporte extraíble.
- [ ] Medir y verificar la exportación.
- [ ] Generar PAR2 cuando el soporte lo justifique.
- [ ] Verificar copias físicas después de escribirlas.

### Trazabilidad respecto al paquete monolítico histórico

- El antiguo Full Archive ya no vive bajo una carpeta numerada: es un objeto inmutable por SHA-256.
- El runner ya no se duplica dentro del paquete: es un objeto compartido.
- Los originales y las modificaciones siguen dentro del Full Archive y se describen mediante la cápsula y la evidencia.
- Los ZIP de partida dejan de ser autoridad: manda el backup transaccional de `03_PERSISTENT_STATE`.
- Un lanzador offline histórico puede conservarse como evidencia, pero la política debe estar en contratos y receipts.
- El artbook ya no vive en una carpeta genérica de contenido adicional: está bajo `supplemental-content/artbook/`.
- El appmanifest es evidencia externa resumida, no payload jugable.
- Los logs brutos no deben entrar en la colección.
- Los documentos y manifiestos se actualizan mediante una operación transaccional, no mediante edición directa del vault.
- Una exportación física pertenece a `07_EXPORTS`, no a `01_IMMUTABLE_VAULT`.

### Regla de continuidad

Al retomar el proyecto:

1. no reconstruyas desde cero;
2. verifica antes de experimentar;
3. trabaja sobre materializaciones o copias;
4. no cambies la identidad de la partida;
5. no sustituyas runner ni componentes silenciosamente;
6. conserva originales;
7. guarda el estado antes de limpiar;
8. registra cualquier desviación;
9. no confundas una prueba de arranque con una restauración completa;
10. no declares cerrado lo que sigue sin probar.

Un archivo no está completo porque “parezca ordenado”. Está defendible cuando cada afirmación tiene un objeto, un hash, un receipt o una limitación explícita.
