#!/usr/bin/env python3
"""
Genera un lote de .m4a del mismo ritmo a distintos tempos (p.ej. de 60 a 120 bpm de 5 en 5)
estirando uno o varios audios "ancla" exportados de BiaB, sin cambiar la afinación.

Cada tempo se genera desde el ancla MÁS CERCANA, así que con 2-3 anclas (p.ej. 60, 90 y 120)
ningún estirado pasa de ~±15% y la calidad es prácticamente la del render nativo.

Uso:
  python3 tempos.py Ritmo_Render_90.aiff --desde 60 --hasta 120 --paso 5
  python3 tempos.py Ritmo_60.aiff Ritmo_90.aiff Ritmo_120.aiff --desde 60 --hasta 120 --paso 5
  python3 tempos.py ritmo.aiff --bpm-origen 90 --tempos 70,85,100 --nombre JazzFunk

Los nombres de salida (<Nombre>-<bpm>.m4a) son los que espera render-rhythm-video.js --dir de
guitar-visualizer (BPM = número final del nombre). Ver README.md para el resto de opciones.

Solo necesita ffmpeg compilado con librubberband (el de evermeet.cx lo trae). Sin dependencias
de Python: funciona con el python3 del sistema (3.7).
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

# El filtro rubberband de ffmpeg trabaja en modo "tiempo real" y se come el ataque de lo que
# suena justo en t=0 — y las claquetas de BiaB empiezan exactamente ahí. Se antepone este
# silencio antes de estirar y se recorta después (medido: sin él desaparece el primer golpe).
RELLENO_SEG = 1.0

# Opciones de Rubber Band por tipo de material. "bateria" usa fase independiente, que conserva
# mucho mejor los golpes secos (a +15%, las baquetas de la claqueta pierden ~1 dB de pico frente
# a 2-4 dB con la fase laminar de "mezcla"), pero puede sonar "fasoso" en lo tonal (bajo, piano):
# si el ritmo lleva armonía y se nota, usar "mezcla".
# La ventana corta (window=short, el --crisp 6 del rubberband de línea de comandos) se probó
# y se descartó: con la batería de BiaB atenuaba MÁS los golpes de claqueta, no menos.
MODOS = {
    'mezcla': 'transients=crisp',
    'bateria': 'transients=crisp:phase=independent',
}

# Estirado a partir del cual se avisa. Medido con una batería de BiaB en modo "bateria": hasta
# +15% los golpes pierden ≤1 dB de pico; a +33% la claqueta ya cae 2-4 dB y a +50%, 3-7 dB.
AVISO_ESTIRADO = 0.15


def error(msg):
    sys.exit('✗ ' + msg)


def ffmpeg(args, capturar=False):
    cmd = ['ffmpeg', '-hide_banner', '-nostdin'] + args
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode != 0 and not capturar:
        error('ffmpeg falló:\n  %s\n%s' % (' '.join(cmd), r.stderr.strip()[-1500:]))
    return r.stderr


def comprobar_ffmpeg():
    if not shutil.which('ffmpeg'):
        error('no encuentro ffmpeg en el PATH.')
    r = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if not re.search(r'\brubberband\b', r.stdout):
        error('este ffmpeg no trae el filtro rubberband (hace falta --enable-librubberband).')
    r = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    # aac_at (AudioToolbox, el codificador de Apple) suena mejor que el aac nativo de ffmpeg.
    return 'aac_at' if re.search(r'\baac_at\b', r.stdout) else 'aac'


def bpm_del_nombre(ruta):
    """Mismo criterio que bpmFromFilename() de render-rhythm-video.js: número final del nombre,
    ignorando el sufijo _Render que añade BiaB (Ritmo-100_Render.m4a → 100)."""
    nombre = os.path.splitext(os.path.basename(ruta))[0]
    nombre = re.sub(r'[_\-\s]*render$', '', nombre, flags=re.I)
    nombre = re.sub(r'\s*bpm$', '', nombre, flags=re.I)
    m = re.search(r'(\d+)\s*$', nombre)
    return int(m.group(1)) if m else None


def nombre_base(ruta):
    """Nombre del ritmo sin BPM ni _Render: 'BateríaJazzFunk_Render_60.aiff' → 'BateríaJazzFunk'."""
    nombre = os.path.splitext(os.path.basename(ruta))[0]
    anterior = None
    while anterior != nombre:
        anterior = nombre
        nombre = re.sub(r'[_\-\s]*(render|\d+\s*(bpm)?)$', '', nombre, flags=re.I)
    return nombre or 'Ritmo'


def info_audio(ruta):
    """(duración en segundos, frecuencia de muestreo) leídas con ffmpeg -i (no hay ffprobe)."""
    err = ffmpeg(['-i', ruta], capturar=True)
    d = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', err)
    f = re.search(r'Audio:.*?(\d+) Hz', err)
    if not d or not f:
        error('no puedo leer el audio de %s' % ruta)
    return int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)), int(f.group(1))


def lista_tempos(args):
    if args.tempos:
        try:
            tempos = [int(t) for t in args.tempos.split(',') if t.strip()]
        except ValueError:
            error('--tempos debe ser una lista de enteros separados por comas (70,85,100).')
    else:
        if args.desde is None or args.hasta is None:
            error('indica --desde y --hasta (o una lista con --tempos).')
        if args.paso <= 0 or args.hasta < args.desde:
            error('rango de tempos no válido.')
        tempos = list(range(args.desde, args.hasta + 1, args.paso))
    if not tempos or min(tempos) <= 0:
        error('lista de tempos vacía o no válida.')
    return sorted(set(tempos))


def medir_sonoridad(ruta):
    """Sonoridad integrada (LUFS) y pico real (dBTP) del archivo, vía el análisis de loudnorm."""
    err = ffmpeg(['-i', ruta, '-af', 'loudnorm=print_format=json', '-f', 'null', '-'])
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', err)
    if not m:
        error('no pude medir la sonoridad de %s' % ruta)
    datos = json.loads(m.group(0))
    return float(datos['input_i']), float(datos['input_tp'])


def estirar(ancla, bpm, args, tmp):
    """Estira el ancla hasta `bpm` y recorta/funde, dejando un WAV temporal. Devuelve sus datos
    (ruta, duración y, si se normaliza, sonoridad y pico medidos) para codificar() después."""
    ratio = bpm / float(ancla['bpm'])
    sr = ancla['sr']
    filtros = []
    if abs(ratio - 1.0) > 1e-9:
        relleno = int(round(RELLENO_SEG * sr / ratio))
        filtros += [
            'adelay=%d:all=1' % int(RELLENO_SEG * 1000),
            'rubberband=tempo=%r:%s' % (ratio, MODOS[args.modo]),
            'atrim=start_sample=%d' % relleno,
            'asetpts=PTS-STARTPTS',
        ]
    dur = ancla['dur'] / ratio
    if args.compases:
        dur_compases = args.compases * args.tiempos * 60.0 / bpm
        if dur_compases > dur + 0.05:
            print('  ⚠ %d bpm: el ancla solo da para %.1f compases; no se recorta.'
                  % (bpm, dur * bpm / 60.0 / args.tiempos))
        else:
            dur = dur_compases
            filtros.append('atrim=end=%.6f' % dur)
    fade = args.fade if args.fade is not None else (2.0 if args.compases else 0.0)
    if fade > 0:
        filtros.append('afade=t=out:st=%.6f:d=%.6f' % (max(0.0, dur - fade), fade))

    # Paso intermedio en WAV de coma flotante: permite medir la sonoridad del resultado ya
    # estirado/recortado y aplicar después una ganancia FIJA (no el loudnorm dinámico, que
    # comprime y cambia el carácter de la batería).
    wav = os.path.join(tmp, '%d.wav' % bpm)
    ffmpeg(['-y', '-i', ancla['ruta'], '-map', '0:a:0'] + (['-af', ','.join(filtros)] if filtros else [])
           + ['-c:a', 'pcm_f32le', wav])
    pista = {'bpm': bpm, 'wav': wav, 'dur': dur, 'ancla': ancla['bpm'], 'ratio': ratio}
    if args.lufs is not None:
        pista['lufs'], pista['pico'] = medir_sonoridad(wav)
    return pista


def codificar(pista, destino, objetivo, args, codec):
    ganancia = 0.0 if objetivo is None else objetivo - pista['lufs']
    ffmpeg(['-y', '-i', pista['wav'], '-af', 'volume=%.3fdB' % ganancia, '-c:a', codec, '-b:a', args.bitrate,
            '-movflags', '+faststart', destino])
    os.remove(pista['wav'])
    return ganancia


def main():
    p = argparse.ArgumentParser(
        description='Genera .m4a del mismo ritmo a distintos tempos a partir de uno o varios audios ancla.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('anclas', nargs='+', help='audio(s) de origen (aiff/wav/m4a…). El BPM de cada uno '
                   'se lee del número final del nombre (Ritmo_Render_60.aiff → 60)')
    p.add_argument('--bpm-origen', type=float, nargs='+', metavar='BPM',
                   help='BPM de cada ancla, en el mismo orden (si el nombre no lo lleva)')
    p.add_argument('--desde', type=int, help='primer tempo a generar')
    p.add_argument('--hasta', type=int, help='último tempo a generar (incluido)')
    p.add_argument('--paso', type=int, default=5, help='incremento entre tempos')
    p.add_argument('--tempos', help='lista explícita en vez de un rango: 70,85,100')
    p.add_argument('--nombre', help='nombre base de los archivos (por defecto: el del ancla sin BPM ni _Render)')
    p.add_argument('--salida', help='carpeta de salida (por defecto: <Nombre>_<desde>_<hasta>_<paso> junto al ancla)')
    p.add_argument('--modo', choices=sorted(MODOS), default='bateria',
                   help='tipo de material: "bateria" para percusión, "mezcla" si el bajo/armonía suena raro')
    p.add_argument('--compases', type=int, help='recorta cada audio a este nº de compases (contando la claqueta)')
    p.add_argument('--tiempos', type=int, default=4, help='tiempos por compás (para --compases y config.json)')
    p.add_argument('--fade', type=float, help='fundido final en segundos (por defecto: 2 si se usa --compases, 0 si no)')
    p.add_argument('--lufs', type=float, default=-16.0, help='sonoridad objetivo de todos los archivos')
    p.add_argument('--sin-normalizar', action='store_true', help='no tocar el volumen')
    p.add_argument('--pico', type=float, default=-1.0, help='pico real máximo (dBTP) al normalizar')
    p.add_argument('--bitrate', default='192k', help='bitrate AAC')
    p.add_argument('--estilo', help='si se indica, escribe también un config.json para render-rhythm-video.js --dir')
    p.add_argument('--subestilo', default='', help='config.json: subestilo')
    p.add_argument('--color', help='config.json: colorPreset (rock, funk, jazz, blues, metal, pop, reggae, afrocubano…)')
    p.add_argument('--intro-compases', type=int, default=2, help='config.json: compases de claqueta (introBars)')
    args = p.parse_args()
    if args.sin_normalizar:
        args.lufs = None

    codec = comprobar_ffmpeg()
    tempos = lista_tempos(args)

    if args.bpm_origen and len(args.bpm_origen) != len(args.anclas):
        error('--bpm-origen necesita un valor por cada ancla (%d).' % len(args.anclas))
    anclas = []
    for i, ruta in enumerate(args.anclas):
        if not os.path.isfile(ruta):
            error('no existe el archivo %s' % ruta)
        bpm = args.bpm_origen[i] if args.bpm_origen else bpm_del_nombre(ruta)
        if not bpm:
            error('no sé el BPM de %s: no acaba en número; indícalo con --bpm-origen.' % os.path.basename(ruta))
        dur, sr = info_audio(ruta)
        anclas.append({'ruta': os.path.abspath(ruta), 'bpm': bpm, 'dur': dur, 'sr': sr})
    if len(set(a['bpm'] for a in anclas)) != len(anclas):
        error('hay dos anclas con el mismo BPM.')

    nombre = args.nombre or nombre_base(anclas[0]['ruta'])
    salida = args.salida or os.path.join(
        os.path.dirname(anclas[0]['ruta']),
        '%s_%d_%d_%d' % (nombre, tempos[0], tempos[-1], args.paso) if not args.tempos else '%s_tempos' % nombre)
    os.makedirs(salida, exist_ok=True)

    print('Ritmo: %s · %d tempos (%d–%d bpm) · modo %s · %s' % (nombre, len(tempos), tempos[0], tempos[-1], args.modo, codec))
    for a in anclas:
        print('  ancla %g bpm: %s (%.1fs ≈ %.1f compases de %d)' % (
            a['bpm'], os.path.basename(a['ruta']), a['dur'], a['dur'] * a['bpm'] / 60.0 / args.tiempos, args.tiempos))
    print('Salida: %s' % salida)

    rutas_ancla = set(a['ruta'] for a in anclas)
    destinos = {}
    for bpm in tempos:
        destinos[bpm] = os.path.abspath(os.path.join(salida, '%s-%d.m4a' % (nombre, bpm)))
        if destinos[bpm] in rutas_ancla:
            error('la salida %s pisaría un ancla; usa otra --salida o --nombre.' % destinos[bpm])

    tmp = tempfile.mkdtemp(prefix='ritmos-tempo-')
    try:
        print('Estirando…')
        pistas = []
        for bpm in tempos:
            # Ancla más cercana en proporción, no en BPM absolutos: lo que degrada el audio es el
            # factor de estirado, así que entre anclas a 60 y 120 el corte está en ~85, no en 90.
            ancla = min(anclas, key=lambda a: abs(math.log(bpm / float(a['bpm']))))
            pista = estirar(ancla, bpm, args, tmp)
            pistas.append(pista)
            aviso = '  ⚠ estirado grande: añade un ancla más cercana' if abs(pista['ratio'] - 1) > AVISO_ESTIRADO + 1e-9 else ''
            print('  · %d bpm  ancla %g (%+.0f%%) · %.1fs%s%s' % (
                bpm, ancla['bpm'], (pista['ratio'] - 1) * 100, pista['dur'],
                ' · %.1f LUFS' % pista['lufs'] if args.lufs is not None else '', aviso))

        # Sonoridad común: se aplica solo ganancia (sin limitador), así que el objetivo real es
        # el más alto que TODOS los archivos alcanzan sin que su pico pase de --pico. Una batería
        # sola tiene picos muy por encima de su sonoridad media y rara vez llega a -16 LUFS;
        # lo que importa es que todo el lote quede igual de fuerte entre sí.
        objetivo = None
        if args.lufs is not None:
            alcanzable = min(p['lufs'] + (args.pico - p['pico']) for p in pistas)
            objetivo = min(args.lufs, alcanzable)
            if objetivo < args.lufs - 0.05:
                print('Sonoridad: %.1f LUFS no se alcanza sin saturar picos; todo el lote va a %.1f LUFS.'
                      % (args.lufs, objetivo))
        print('Codificando…')
        for pista in pistas:
            ganancia = codificar(pista, destinos[pista['bpm']], objetivo, args, codec)
            print('  ✓ %s  %+.1f dB' % (os.path.basename(destinos[pista['bpm']]), ganancia))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if args.estilo:
        cfg_path = os.path.join(salida, 'config.json')
        cfg = {'style': args.estilo, 'substyle': args.subestilo, 'beats': args.tiempos,
               'introBars': args.intro_compases, 'offsetMs': 0, 'extraSec': 2}
        if args.color:
            cfg['colorPreset'] = args.color
        with open(cfg_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write('\n')
        print('config.json escrito: %s' % cfg_path)
    print('Hecho: %d archivos.' % len(tempos))


if __name__ == '__main__':
    main()
