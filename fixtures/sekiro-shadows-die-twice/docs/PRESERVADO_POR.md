# Preservado por

> **Alcance de esta autoría.** Lo que se firma en este documento es **el paquete de
> preservación**: la extracción, la configuración, la documentación, la auditoría y el
> empaquetado. **NO** el juego. Sekiro: Shadows Die Twice y todo su contenido —código,
> arte, música, guion— son obra de **FromSoftware**; sus créditos están en `CREDITOS.md`
> y en el propio juego. Esta distinción es deliberada y no debe difuminarse: quien preserva
> una obra no es su autor.

---

## Autoría del paquete

**Preservado y empaquetado por:** itsNeverwinter2
**Fecha:** julio de 2026
**Contacto:** [itsneverwinter2@gmail.com]

---

## Qué se hizo en este paquete

- **Desacople de Steam:** sustitución de la capa de Steamworks por Goldberg Emulator (gbe_fork, commit `8ffe94a`), **compilado a mano desde fuente**, no descargado precompilado.
- **Retirada de SteamStub:** desempaquetado del ejecutable con un port propio de Steamless a Linux, ejecutado en sandbox sin red.
- **Auditoría de seguridad** de la DLL compilada antes de integrarla (ver `07_AUDITORIA_SEGURIDAD/`).
- **Configuración de preservación:** bottle de Wine con runner, DXVK/VKD3D y dependencias fijados; modo offline en dos capas; instantánea verificada.
- **Extracción del artbook** del contenedor cifrado original, documentada y reproducible (ver `08_CONTENIDO_ADICIONAL/`).
- **Documentación completa** pensada para ser restaurable dentro de 20 años sin conocimiento previo.

Todo el material de partida procede de una **copia comprada y legítima**; el alcance del
proyecto es la preservación y el aislamiento de copias en posesión legítima, no la elusión
de compras ni la distribución.

---

## Herramientas de terceros utilizadas (crédito a quien corresponde)

Este paquete se apoya en trabajo libre y de código abierto de otras personas, que merece
reconocimiento:

- **gbe_fork** (Goldberg Steam Emulator, fork de Detanup01) — reimplementación de la API de Steamworks.
- **Steamless** (atom0s y colaboradores) — retirada de SteamStub; aquí vía un port propio a Linux.
- **UXM Selective Unpacker** (Nordgaren) — lectura del formato de contenedor de FromSoftware para el artbook.
- **GE-Proton** (GloriousEggroll), **DXVK**, **VKD3D-Proton**, **Wine**, **Bottles** — la pila de ejecución.

La autoría del *paquete* no reclama nada sobre estas herramientas; solo sobre su
integración, configuración y documentación para este fin concreto.

---

## Intención y base legal de esta preservación

**Uso: estrictamente PERSONAL.** Este paquete es una copia de una copia comprada, legítima y
original de Sekiro: Shadows Die Twice. **No está destinado a compartirse, difundirse ni
distribuirse por ningún medio**, ni gratuita ni onerosamente. Difundirlo vulneraría los
derechos de propiedad intelectual de FromSoftware y quedaría fuera del amparo legal que se
describe abajo. La copia existe por un único motivo: **preservación a largo plazo**, para que
la copia comprada siga siendo jugable aunque Steam deje de operar (ver la sección "qué era
Steam" en `FICHA_DEL_JUEGO.md`).

### Marco legal en el que se ampara (España)

> **Esto no es asesoramiento jurídico.** Es la base normativa, verificada, en la que el
> autor entiende que se apoya este uso personal. Las leyes cambian y su interpretación
> corresponde a los tribunales.

El límite legal de **copia privada** está en el **artículo 31.2 del texto refundido de la Ley
de Propiedad Intelectual (Real Decreto Legislativo 1/1996, de 12 de abril)**, en su redacción
vigente tras el Real Decreto-ley 12/2017. Permite reproducir sin autorización del autor una
obra ya divulgada cuando concurren **a la vez** estas condiciones:

- **(a)** la realiza una **persona física** para su **uso privado**, no profesional ni
  empresarial, y **sin fines comerciales** ni directos ni indirectos;
- **(b)** se hace **a partir de una fuente lícita** y **sin vulnerar las condiciones de acceso**
  a la obra;
- **(c)** la copia **no es objeto de utilización colectiva ni lucrativa, ni de distribución
  mediante precio**.

**Cómo encaja este paquete:**

- **(a) y (c): se cumplen con holgura.** Persona física, uso estrictamente personal, sin ánimo
  de lucro, sin distribución. Ese es el compromiso central de este documento.
- **(b) fuente lícita: se cumple.** El juego se compró en Steam; **no se ha descargado ningún
  fichero de origen desconocido**. El desacople de Steam (Goldberg compilado a mano, Steamless)
  se hizo **manualmente sobre la copia comprada**, no obteniendo una versión ya pirateada de un
  tercero. Esta es una diferencia jurídica y ética relevante frente a la piratería.

### El matiz honesto que este archivo debe reconocer

Aquí conviene separar **dos operaciones distintas**, porque tienen encaje legal diferente y
mezclarlas debilitaría el argumento en vez de reforzarlo:

**1. Steamworks → Goldberg: esto es EMULACIÓN, y la emulación es legítima.**
Goldberg no "rompe" nada: **reimplementa la interfaz (API) de Steamworks** para que el juego
reciba localmente las respuestas que esperaría de Steam. Reimplementar de forma limpia una
interfaz para lograr interoperabilidad es una práctica **respaldada por jurisprudencia
consolidada** (el precedente de referencia es *Sony Computer Entertainment v. Connectix*, EE.UU.,
2000, sobre emulación por reimplementación). Emular no es piratear: no se copia el código de
Valve, se sustituye por una implementación propia y libre. Esta capa está, por tanto, sobre
base sólida.

**2. SteamStub → Steamless: esto NO es emulación, es desempaquetado — y es el punto discutible.**
SteamStub es el envoltorio cifrado del ejecutable; Steamless **retira ese cifrado**. Eso no
"emula" nada: elimina una capa de protección. Y aquí sí entra en juego el **artículo 160 del
texto refundido de la LPI**, que protege las **medidas tecnológicas de protección**. El límite
de copia privada, por sí solo, **no habilita automáticamente a eludir una medida tecnológica**.

Por tanto, con precisión:

- La **copia** para uso privado y desde fuente lícita: sólidamente amparada.
- La **emulación de Steamworks** (Goldberg): sobre base jurídica sólida; no es piratería.
- El **desempaquetado de SteamStub** (Steamless): el punto jurídicamente **discutible** — no
  por ser piratería (no lo es: no hay copia ajena descargada, se opera sobre la copia comprada),
  sino porque tocar una medida tecnológica no está claramente cubierto por el límite de copia
  privada.

El autor sostiene que su caso es **razonable y defendible**: copia comprada, uso exclusivamente
personal, sin difusión alguna, motivado por la preservación ante la obsolescencia programada de
un servicio, y con un perjuicio económico al titular que es **nulo** (no sustituye ninguna venta,
puesto que el juego ya se pagó). Lo presenta como **la posición en la que se ampara**, distinguiendo
con honestidad lo que está sólidamente cubierto (la copia y la emulación) de lo que es defendible
pero no incontrovertible (el desempaquetado).

### Licencia de la documentación de este paquete

Todo lo anterior se refiere al **juego**, que **no es del autor y no puede relicenciar**. Cosa
distinta es la **documentación** escrita para este paquete (los `.md`, los scripts, las notas de
extracción y auditoría), que sí es obra propia del autor.

---

## Parte de una colección

Este paquete forma parte de un archivo personal de preservación de videojuegos que sigue
una metodología común (ver `00_INDICE_DE_LA_COLECCION.md` en la raíz de la colección).
Paquetes hermanos hasta la fecha: Dark Souls: Prepare to Die Edition, Dark Souls III.
