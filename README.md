# ritmos-tempo

Genera un lote de `.m4a` del mismo ritmo a distintos tempos (p.ej. de 60 a 120 bpm de 5 en 5)
estirando uno o varios audios "ancla" exportados de BiaB, sin cambiar la afinación. Pensado para
alimentar `render-rhythm-video.js --dir` de guitar-visualizer sin tener que exportar cada tempo
a mano desde BiaB.

Solo necesita `ffmpeg` con el filtro `rubberband` (el de evermeet.cx lo trae) y el `python3`
del sistema. Sin dependencias de Python.

## Uso

```sh
# Tres anclas: cada tempo se genera desde la más cercana
python3 tempos.py Ritmo_Render_60.aiff Ritmo_Render_85.aiff Ritmo_Render_115.aiff \
  --desde 60 --hasta 120 --paso 5 --nombre JazzFunk --estilo "JAZZ FUNK" --color funk

# Una sola ancla, con el BPM indicado a mano
python3 tempos.py ritmo.aiff --bpm-origen 90 --desde 75 --hasta 105 --paso 5
```

El BPM de cada ancla se lee del número final del nombre (`Ritmo_Render_60.aiff` → 60, igual que
`bpmFromFilename()` en guitar-visualizer); si el nombre no lo lleva, `--bpm-origen`.

Salida, por defecto en una carpeta `<Nombre>_<desde>_<hasta>_<paso>/` junto al ancla:

```
JazzFunk-60.m4a  JazzFunk-65.m4a  …  JazzFunk-120.m4a  config.json
```

Los nombres acaban en el BPM, que es lo que lee `render-rhythm-video.js`. El `config.json` solo
se escribe si se pasa `--estilo`:

```sh
node scripts/render-rhythm-video.js --dir ~/Downloads/JazzFunk_60_120_5 --out ~/Downloads/JazzFunk_60_120_5
```

## Tempo progresivo

Con `--progresivo` sale un único audio que recorre todos los tempos (N compases en cada uno, tras
la claqueta al primer tempo) y el XML con el mapa de tempo: el par que pide
`render-rhythm-video.js --xml`, sin montar el Tempo Track en BiaB.

```sh
python3 tempos.py Ritmo_Render_65.aiff Ritmo_Render_85.aiff Ritmo_Render_110.aiff \
  --progresivo --desde 60 --hasta 120 --paso 2 --compases-por-tempo 8 --nombre JazzFunk

node scripts/render-rhythm-video.js --xml JazzFunk-60-120-incr2-progresivo.xml \
  --audio JazzFunk-60-120-incr2-progresivo.m4a --style "JAZZ FUNK" --color-preset funk
```

Salida, por defecto en `<Nombre>_progresivo/` junto al ancla: `<Nombre>-<desde>-<hasta>-incr<paso>-progresivo.m4a`
y `.xml` con el mismo nombre (lo que empareja `--xml-dir`). El nombre no acaba en número a
propósito: guitar-visualizer toma el número final del nombre del audio como BPM base y pisaría
el del XML.

- Cada tramo sale del ancla más cercana y de los **mismos compases** que ocuparía en ella, así
  el patrón y los redobles siguen donde los puso BiaB. Las anclas tienen que ser renders del
  mismo tema, con `--intro-compases` de claqueta (2 por defecto), y durar tantos compases como
  la progresión; si no, el tramo vuelve al principio del ancla y el script lo avisa.
- Los tramos se unen con un fundido cruzado de 10 ms que acaba 20 ms antes de la barra de compás.
- El XML es mínimo: un `<sound tempo>` donde cambia el tempo y un acorde de referencia (C) en el
  primer compás tras la claqueta. No lleva las notas de batería.
- Tras el último compás el ritmo sigue `--fade` segundos (2 por defecto) fundiéndose, para que
  el vídeo no acabe en seco.

## Opciones

| Opción | Qué hace |
|---|---|
| `--desde / --hasta / --paso` | Rango de tempos (paso por defecto: 5) |
| `--tempos 70,85,100` | Lista explícita en vez de rango |
| `--bpm-origen 60 90` | BPM de cada ancla, en orden, si el nombre no lo lleva |
| `--nombre`, `--salida` | Nombre base de los archivos y carpeta de salida |
| `--modo bateria\|mezcla` | `bateria` (por defecto) conserva mejor los golpes; `mezcla` si el bajo o la armonía suenan "fasosos" |
| `--progresivo` | Un único audio con todos los tempos + XML con el mapa de tempo (ver arriba) |
| `--compases-por-tempo 8` | Con `--progresivo`: compases en cada tempo |
| `--compases N` | Recorta cada audio a N compases (contando la claqueta) |
| `--tiempos 4` | Tiempos por compás |
| `--fade S` | Fundido final (por defecto 2 s con `--compases`, 0 sin él) |
| `--lufs -16`, `--pico -1` | Sonoridad objetivo y pico real máximo |
| `--sin-normalizar` | No tocar el volumen |
| `--bitrate 192k` | Bitrate AAC |
| `--estilo`, `--subestilo`, `--color`, `--intro-compases` | Escriben `config.json` para el generador de vídeos |

## Cuántas anclas hacen falta

Lo que degrada el audio es el factor de estirado, así que la regla es **no pasar de ±15%**
(el script avisa si se supera). Pérdida de pico de los golpes, medida con
`BateríaJazzFunkGroovin` estirando el render de 60 bpm (16 compases, `--modo bateria`):

| Estirado | Golpes del groove (mediana) | Claqueta (baquetas) |
|---|---|---|
| +8% (60→65) | −0,3 dB | −0,3 a −0,6 dB |
| +15% (60→69) | −0,5 dB | −0,7 a −1,3 dB |
| +25% (60→75) | −0,9 dB | −1,4 a −2,7 dB |
| +33% (60→80) | −1,1 dB | −2,0 a −4,1 dB |
| +50% (60→90) | −1,7 dB | −3,0 a −6,9 dB |
| +128% (60→137) | −4,1 dB | −7 a −16 dB |

Con `--modo mezcla` el groove sale igual, pero la claqueta pierde más o menos el doble
(−1,4 a −2,4 dB ya a +8%). Son medidas de pico, no una prueba de escucha: sirven para comparar
ajustes, la decisión final es de oído.

Para cubrir 60–120 sin pasar de ±15% bastan tres anclas: **69, 91 y 120** cubren 60–138;
con números redondos, **65, 85 y 110** cubren 57–126. Estirar hacia abajo (ralentizar) conserva
mejor los golpes que acelerar, así que ante la duda el ancla mejor un poco por encima.

El tempo no deriva: la duración sale exacta y la desviación respecto a la rejilla es la misma
al principio y al final de un ancla de 8 minutos. El filtro desplaza todos los golpes una
cantidad fija que depende del estirado (+4 ms a +15%, +10 ms a +50%, −12 ms a −27%); en modo
`bateria` el script la compensa y los golpes quedan a ±2 ms de donde estaban en el ancla. En
modo `mezcla` el desvío varía de un golpe a otro (unos ms) y no se corrige.

## Cómo funciona

1. Elige para cada tempo el ancla más cercana en proporción.
2. Estira con el filtro `rubberband` de ffmpeg (transitorios `crisp`, fase independiente en
   modo `bateria`). Antes añade 1 s de
   silencio y lo recorta después: el filtro se come el ataque de lo que suena en t=0, que es
   justo donde empieza la claqueta de BiaB.
3. Recorta a N compases y funde, si se pide.
4. Mide la sonoridad (LUFS) y el pico real de todos los resultados y aplica a cada uno una
   ganancia fija para que el lote entero quede igual de fuerte. No usa limitador: si `--lufs`
   no se alcanza sin saturar picos (lo normal con batería sola), baja el objetivo común al
   máximo alcanzable y lo dice.
5. Codifica en AAC con `aac_at` (el codificador de Apple) si está disponible.

Los intermedios son WAV de coma flotante en una carpeta temporal (~180 MB por cada 8 minutos
de audio, se borran al acabar).

## Descartado

- `atempo` de ffmpeg: pierde golpes enteros con estirados grandes.
- `rubberband` de línea de comandos (motor R3): desvío de tiempo menor (<1 ms de media) pero ablanda
  todos los golpes ~2 dB incluso a +8%; a estirados pequeños el filtro de ffmpeg da mejor resultado.
  Además Homebrew ya no lo instala en macOS 13 sin compilar.
- Automatizar BiaB por interfaz: es la única vía a calidad nativa en cada tempo, pero frágil.
  Con tres anclas no compensa.
