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
  python3 tempos.py Ritmo_60.aiff Ritmo_90.aiff --progresivo --desde 60 --hasta 100 --paso 2

Con --progresivo sale un único audio que recorre todos los tempos, más el XML con el mapa de
tempo que pide render-rhythm-video.js --xml.

Los nombres de salida (<Nombre>-<bpm>.m4a) son los que espera render-rhythm-video.js --dir de
guitar-visualizer (BPM = número final del nombre). Ver README.md para el resto de opciones.

Solo necesita ffmpeg compilado con librubberband (el de evermeet.cx lo trae). Sin dependencias
de Python: funciona con el python3 del sistema (3.7).
"""
import argparse
import array
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

# El filtro rubberband entrega cada golpe desplazado LATENCIA·(1 − 1/ratio) muestras: tarde al
# acelerar, pronto al ralentizar (medido con un tren de golpes sintéticos: +4 ms a +15%, +10 ms
# a +50%, −12 ms a −27%; son las mismas muestras a 44,1 y a 48 kHz). Constante a lo largo del
# audio, así que se compensa al recortar el relleno. En modo "mezcla" el desvío depende de cada
# golpe (unos ms, no sigue esta ley) y no se corrige.
LATENCIA = {'bateria': 1389, 'mezcla': 0}

# Modo progresivo: los tramos de cada tempo se unen con un fundido cruzado muy corto que acaba un
# poco ANTES de la barra de compás, para que el golpe del primer tiempo pertenezca entero al
# tramo nuevo (un corte justo en la barra parte los golpes que el batería adelanta unos ms).
# Cada tramo trae como "entrada" el audio real que precede a ese compás en su ancla, así que lo
# que se funde son dos versiones de la misma cola de platos, no un corte a silencio.
ADELANTO_CORTE = 0.020
FUNDIDO_CORTE = 0.010
# Compases finales del ancla que no se usan como material (final de BiaB y cola del render).
COLA_ANCLA = 4


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


def fin_del_audio(ruta, dur):
    """Segundo en el que acaba el sonido del archivo: si termina con un silencio largo, donde
    empieza ese silencio; si no, su duración."""
    err = ffmpeg(['-i', ruta, '-map', '0:a:0', '-af', 'silencedetect=noise=-60dB:d=5', '-f', 'null', '-'])
    inicios = re.findall(r'silence_start:\s*(-?[\d.]+)', err)
    finales = re.findall(r'silence_end:\s*(-?[\d.]+)', err)
    # El último silencio llega hasta el final si no tiene silence_end o si acaba con el archivo.
    if inicios and (len(finales) < len(inicios) or float(finales[-1]) > dur - 0.5):
        return max(0.0, float(inicios[-1]))
    return dur


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
    if not args.progresivo:
        return sorted(set(tempos))
    # En progresivo el orden ES el recorrido: una lista de --tempos se respeta tal cual, y un
    # rango se recorre según --forma.
    if args.tempos or args.forma == 'sube':
        return tempos
    if args.forma == 'baja':
        return tempos[::-1]
    if args.forma == 'piramide':
        return tempos + tempos[-2::-1]
    # zigzag: dos pasos adelante y uno atrás (60, 70, 65, 75, 70, 80…) hasta llegar arriba.
    zigzag = [tempos[0]]
    i = 0
    while i < len(tempos) - 1:
        i = min(i + 2, len(tempos) - 1)
        zigzag.append(tempos[i])
        if i < len(tempos) - 1:
            i -= 1
            zigzag.append(tempos[i])
    return zigzag


def medir_sonoridad(ruta):
    """Sonoridad integrada (LUFS) y pico real (dBTP) del archivo, vía el análisis de loudnorm."""
    err = ffmpeg(['-i', ruta, '-af', 'loudnorm=print_format=json', '-f', 'null', '-'])
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', err)
    if not m:
        error('no pude medir la sonoridad de %s' % ruta)
    datos = json.loads(m.group(0))
    return float(datos['input_i']), float(datos['input_tp'])


def muestras_relleno(ratio, sr, modo):
    """Muestras que ocupa el relleno de RELLENO_SEG una vez estirado, con la latencia del filtro."""
    if abs(ratio - 1.0) < 1e-9:
        return int(round(RELLENO_SEG * sr))
    return int(round(RELLENO_SEG * sr / ratio + LATENCIA[modo] * (1 - 1 / ratio)))


def compases_a_generar(bpm, args):
    """Compases (contando la claqueta) a los que se recorta el audio de `bpm`, o None si va entero.
    Con --minutos son los que caben en ese tiempo, redondeados a frases de 4 compases tras la
    claqueta para que todos los tempos acaben en final de frase."""
    if args.minutos:
        frases = int(round((args.minutos * bpm / float(args.tiempos) - args.intro_compases) / 4.0))
        return args.intro_compases + 4 * max(1, frases)
    return args.compases


def estirar(ancla, bpm, args, tmp):
    """Estira el ancla hasta `bpm` y recorta/funde, dejando un WAV temporal. Devuelve sus datos
    (ruta, duración y, si se normaliza, sonoridad y pico medidos) para codificar() después."""
    ratio = bpm / float(ancla['bpm'])
    sr = ancla['sr']
    dur = ancla['dur'] / ratio
    compases = compases_a_generar(bpm, args)
    recorte = False
    if compases:
        dur_compases = compases * args.tiempos * 60.0 / bpm
        if dur_compases > dur + 0.05:
            print('  ⚠ %d bpm: el ancla solo da para %.1f compases de los %d pedidos; no se recorta.'
                  % (bpm, dur * bpm / 60.0 / args.tiempos, compases))
        else:
            dur = dur_compases
            recorte = True
    filtros = []
    if recorte:
        # Se recorta ya el ancla (con 1 s de margen) para no estirar minutos que luego se tiran.
        filtros.append('atrim=end=%.6f' % (dur * ratio + 1.0))
    if abs(ratio - 1.0) > 1e-9:
        relleno = muestras_relleno(ratio, sr, args.modo)
        filtros += [
            'adelay=%d:all=1' % int(RELLENO_SEG * 1000),
            'rubberband=tempo=%r:%s' % (ratio, MODOS[args.modo]),
            'atrim=start_sample=%d' % relleno,
            'asetpts=PTS-STARTPTS',
        ]
    if recorte:
        filtros.append('atrim=end=%.6f' % dur)
    fade = args.fade if args.fade is not None else (2.0 if compases else 0.0)
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


def compas_de_origen(ancla, compas, n, intro, compases_tramo, tiempos):
    """Compás del ancla (contando la claqueta) del que sale un tramo que empieza en el compás
    `compas` del resultado: el mismo, para que el patrón y los redobles sigan donde los puso BiaB.
    Si el ancla no da para tanto, se vuelve a su primer compás de ritmo. Devuelve (compás, volvió)."""
    enteros = int(ancla['dur'] * ancla['bpm'] / 60.0 / tiempos + 1e-6)
    bucle = enteros - COLA_ANCLA - intro
    if bucle < compases_tramo:
        error('el ancla de %g bpm es demasiado corta para tramos de %d compases.' % (ancla['bpm'], compases_tramo))
    if compas + n <= intro + bucle:
        return compas, False
    pos = (compas - intro) % bucle
    if pos + n > bucle:
        pos = 0
    return intro + pos, True


def estirar_tramo(tr, ini, n_muestras, sr, args, ruta):
    """Deja en `ruta` (PCM float estéreo sin cabecera) las muestras [ini, ini+n_muestras) del
    resultado, sacadas del ancla del tramo de forma que su barra de compás caiga en tr['pulso']."""
    a = tr['ancla']
    ratio = tr['bpm'] / float(a['bpm'])
    s0 = tr['origen'] * args.tiempos * 60.0 / a['bpm'] + (ini - tr['pulso']) / float(sr) * ratio
    # Mismo relleno de RELLENO_SEG que en estirar(), pero con audio real del ancla hasta donde lo
    # haya (y silencio el resto): el filtro arranca "en caliente" y no se come el primer ataque.
    real = min(RELLENO_SEG, max(0.0, s0))
    i0 = int(round((s0 - real) * a['sr']))
    i1 = int(math.ceil((s0 + n_muestras / float(sr) * ratio + 0.5) * a['sr']))
    silencio = int(round((RELLENO_SEG - real) * a['sr']))
    filtros = ['atrim=start_sample=%d:end_sample=%d' % (i0, i1), 'asetpts=PTS-STARTPTS']
    if silencio > 0:
        filtros.append('adelay=%dS:all=1' % silencio)
    if abs(ratio - 1.0) > 1e-9:
        filtros.append('rubberband=tempo=%r:%s' % (ratio, MODOS[args.modo]))
    k = muestras_relleno(ratio, sr, args.modo)
    filtros += ['aformat=sample_fmts=flt:sample_rates=%d:channel_layouts=stereo' % sr, 'apad',
                'atrim=start_sample=%d:end_sample=%d' % (k, k + n_muestras), 'asetpts=PTS-STARTPTS']
    ffmpeg(['-y', '-i', a['ruta'], '-map', '0:a:0', '-af', ','.join(filtros), '-f', 'f32le', ruta])
    if os.path.getsize(ruta) != n_muestras * 8:
        error('el tramo de %d bpm no tiene la duración esperada.' % tr['bpm'])


def escribir_xml(ruta, titulo, tramos, intro, tiempos):
    """MusicXML mínimo con lo que lee guitar-visualizer (loadXML y tempo-progression.js): un
    <sound tempo> en el compás donde cambia el tempo y un acorde de referencia en el primer compás
    tras la claqueta, que es lo que marca cuántos compases de intro hay. Sin notas de batería."""
    div = 120
    l = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 3.0 Partwise//EN"',
         '"http://www.musicxml.org/dtds/partwise.dtd">',
         '<score-partwise version="3.0">',
         '<work>', '  <work-title>%s</work-title>' % titulo, '</work>',
         '<identification>', ' <encoding>', '  <software>ritmos-tempo (tempos.py --progresivo)</software>',
         ' </encoding>', '</identification>',
         '  <part-list>', '   <score-part id="P1">', '      <part-name>Drums</part-name>', '   </score-part>',
         '  </part-list>', '  <part id="P1">']
    num = 0
    for tr in tramos:
        for j in range(tr['n']):
            num += 1
            l.append('    <measure number="%d">' % num)
            if num == 1:
                l += ['      <attributes>', '        <divisions>%d</divisions>' % div,
                      '        <key>', '          <fifths>0</fifths>', '        </key>',
                      '        <time>', '           <beats>%d</beats>' % tiempos,
                      '           <beat-type>4</beat-type>', '        </time>',
                      '        <clef>', '          <sign>percussion</sign>', '        </clef>',
                      '      </attributes>']
            if j == 0:
                l.append('      <sound tempo="%d"/>' % tr['bpm'])
            if num == intro + 1:
                l += ['      <harmony>', '        <root>', '          <root-step>C</root-step>', '        </root>',
                      '        <kind>major</kind>', '      </harmony>']
            l += ['      <note>', '        <rest measure="yes"/>', '        <duration>%d</duration>' % (div * tiempos),
                  '      </note>', '    </measure>']
    l += ['  </part>', '</score-partwise>', '']
    with open(ruta, 'w', encoding='utf-8') as f:
        f.write('\n'.join(l))


def progresivo(anclas, tempos, base, salida, args, codec):
    """Un único audio que recorre `tempos` (N compases en cada uno, tras la claqueta al primer
    tempo) más el XML con el mapa de tempo: el par que pide render-rhythm-video.js --xml."""
    sr = anclas[0]['sr']
    intro = args.intro_compases
    fade = args.fade if args.fade is not None else 2.0
    adelanto = int(round(ADELANTO_CORTE * sr))
    xf = int(round(FUNDIDO_CORTE * sr))

    tramos = []
    t = 0.0
    compas = 0
    for i, bpm in enumerate(tempos):
        n = args.compases_por_tempo + (intro if i == 0 else 0)
        ancla = min(anclas, key=lambda a: abs(math.log(bpm / float(a['bpm']))))
        origen, vuelta = compas_de_origen(ancla, compas, n, intro, args.compases_por_tempo, args.tiempos)
        tramos.append({'bpm': bpm, 'ancla': ancla, 'n': n, 'compas': compas, 't': t,
                       'pulso': int(round(t * sr)), 'origen': origen, 'vuelta': vuelta})
        t += n * args.tiempos * 60.0 / bpm
        compas += n
    fin = int(round(t * sr))
    total = fin + int(round(fade * sr))

    destino = os.path.abspath(os.path.join(salida, base + '.m4a'))
    if destino in set(a['ruta'] for a in anclas):
        error('la salida %s pisaría un ancla; usa otra --salida o --nombre.' % destino)

    tmp = tempfile.mkdtemp(prefix='ritmos-tempo-')
    try:
        print('Estirando…')
        crudo = os.path.join(tmp, 'total.f32')
        cola = None
        with open(crudo, 'wb') as out:
            for i, tr in enumerate(tramos):
                ini = 0 if i == 0 else tr['pulso'] - adelanto - xf
                hasta = tramos[i + 1]['pulso'] - adelanto if i + 1 < len(tramos) else total
                ruta = os.path.join(tmp, '%d.f32' % i)
                estirar_tramo(tr, ini, hasta - ini, sr, args, ruta)
                with open(ruta, 'rb') as f:
                    datos = f.read()
                os.remove(ruta)
                if cola is not None:
                    # Fundido cruzado lineal entre la cola del tramo anterior y la entrada de este.
                    a = array.array('f')
                    a.frombytes(cola)
                    b = array.array('f')
                    b.frombytes(datos[:xf * 8])
                    for j in range(xf):
                        g = (j + 0.5) / xf
                        a[2 * j] = a[2 * j] * (1 - g) + b[2 * j] * g
                        a[2 * j + 1] = a[2 * j + 1] * (1 - g) + b[2 * j + 1] * g
                    out.write(a.tobytes())
                    datos = datos[xf * 8:]
                if i + 1 < len(tramos):
                    out.write(datos[:-xf * 8])
                    cola = datos[-xf * 8:]
                else:
                    out.write(datos)
                ratio = tr['bpm'] / float(tr['ancla']['bpm'])
                aviso = '  ⚠ estirado grande: añade un ancla más cercana' if abs(ratio - 1) > AVISO_ESTIRADO + 1e-9 else ''
                if tr['vuelta']:
                    aviso += '  ⚠ el ancla no da para tanto: vuelve a su compás %d' % (tr['origen'] + 1)
                print('  · %d bpm  compases %d–%d (desde %d:%04.1f)  ancla %g (%+.0f%%)%s' % (
                    tr['bpm'], tr['compas'] + 1, tr['compas'] + tr['n'], int(tr['t'] // 60), tr['t'] % 60,
                    tr['ancla']['bpm'], (ratio - 1) * 100, aviso))

        wav = os.path.join(tmp, 'total.wav')
        ffmpeg(['-y', '-f', 'f32le', '-ar', str(sr), '-ac', '2', '-i', crudo]
               + (['-af', 'afade=t=out:st=%.6f:d=%.6f' % (fin / float(sr), fade)] if fade > 0 else [])
               + ['-c:a', 'pcm_f32le', wav])
        os.remove(crudo)
        pista = {'wav': wav}
        objetivo = None
        if args.lufs is not None:
            pista['lufs'], pista['pico'] = medir_sonoridad(wav)
            objetivo = min(args.lufs, pista['lufs'] + args.pico - pista['pico'])
            if objetivo < args.lufs - 0.05:
                print('Sonoridad: %.1f LUFS no se alcanza sin saturar picos; va a %.1f LUFS.' % (args.lufs, objetivo))
        print('Codificando…')
        ganancia = codificar(pista, destino, objetivo, args, codec)
        print('  ✓ %s  %+.1f dB · %d compases · %d:%04.1f' % (
            os.path.basename(destino), ganancia, compas, int(total / sr // 60), total / float(sr) % 60))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    xml = os.path.join(salida, base + '.xml')
    escribir_xml(xml, base, tramos, intro, args.tiempos)
    print('  ✓ %s  (mapa de tempo: %s)' % (os.path.basename(xml), ' → '.join(str(tr['bpm']) for tr in tramos)))


def escribir_config(salida, args):
    if not args.estilo:
        return
    cfg_path = os.path.join(salida, 'config.json')
    cfg = {'style': args.estilo, 'substyle': args.subestilo, 'beats': args.tiempos,
           'introBars': args.intro_compases, 'offsetMs': 0, 'extraSec': 2}
    if args.color:
        cfg['colorPreset'] = args.color
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print('config.json escrito: %s' % cfg_path)


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
    p.add_argument('--progresivo', action='store_true',
                   help='un único audio que recorre todos los tempos, más el XML con el mapa de tempo '
                   '(para render-rhythm-video.js --xml)')
    p.add_argument('--compases-por-tempo', type=int, default=8, help='--progresivo: compases en cada tempo')
    p.add_argument('--forma', choices=['sube', 'baja', 'piramide', 'zigzag'], default='sube',
                   help='--progresivo: recorrido del rango. piramide sube y vuelve a bajar; zigzag avanza '
                   'dos pasos y retrocede uno. Con --tempos se respeta el orden de la lista')
    p.add_argument('--minutos', type=float, help='recorta cada audio a esta duración aproximada (compases enteros)')
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
    p.add_argument('--intro-compases', type=int, default=2,
                   help='compases de claqueta del ancla (introBars del config.json y, con --progresivo, del XML)')
    args = p.parse_args()
    if args.sin_normalizar:
        args.lufs = None

    if args.compases and args.minutos:
        error('usa --compases o --minutos, no los dos.')
    codec = comprobar_ffmpeg()
    tempos = lista_tempos(args)
    if args.progresivo:
        if args.compases or args.minutos:
            error('--compases y --minutos no se usan con --progresivo; el largo lo da --compases-por-tempo.')
        if len(tempos) < 2 or args.compases_por_tempo <= 0 or args.intro_compases < 0:
            error('--progresivo necesita al menos dos tempos y --compases-por-tempo mayor que 0.')

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
        # Un export de BiaB puede durar los compases pedidos y llevar batería solo en los primeros
        # (caso real: 36 compases de 262). La sonoridad no lo delata, porque LUFS ignora el silencio.
        # Unos pocos compases de cola en silencio son normales; más de 8 es un export incompleto.
        fin = fin_del_audio(ruta, dur)
        if (dur - fin) * bpm / 60.0 / args.tiempos > 8:
            error('%s solo tiene sonido hasta %d:%02d de %d:%02d (%.0f compases de %.0f); el resto es '
                  'silencio. Revisa el export de BiaB.' % (
                      os.path.basename(ruta), fin // 60, fin % 60, dur // 60, dur % 60,
                      fin * bpm / 60.0 / args.tiempos, dur * bpm / 60.0 / args.tiempos))
        anclas.append({'ruta': os.path.abspath(ruta), 'bpm': bpm, 'dur': dur, 'sr': sr})
    if len(set(a['bpm'] for a in anclas)) != len(anclas):
        error('hay dos anclas con el mismo BPM.')

    nombre = args.nombre or nombre_base(anclas[0]['ruta'])
    if args.progresivo:
        carpeta = '%s_progresivo' % nombre
    else:
        carpeta = '%s_%d_%d_%d' % (nombre, tempos[0], tempos[-1], args.paso) if not args.tempos else '%s_tempos' % nombre
    salida = args.salida or os.path.join(os.path.dirname(anclas[0]['ruta']), carpeta)
    os.makedirs(salida, exist_ok=True)

    print('Ritmo: %s · %d tempos (%d–%d bpm) · modo %s · %s' % (nombre, len(tempos), min(tempos), max(tempos), args.modo, codec))
    for a in anclas:
        print('  ancla %g bpm: %s (%.1fs ≈ %.1f compases de %d)' % (
            a['bpm'], os.path.basename(a['ruta']), a['dur'], a['dur'] * a['bpm'] / 60.0 / args.tiempos, args.tiempos))
    print('Salida: %s' % salida)

    if args.progresivo:
        # El nombre NO puede acabar en número: en el módulo Ritmo, guitar-visualizer toma el número
        # final del nombre del audio como BPM base y pisa el del XML (los de BiaB se libran porque
        # acaban en _Render).
        if args.tempos:
            base = '%s-%d-%d-progresivo' % (nombre, min(tempos), max(tempos))
        else:
            base = '%s-%d-%d-incr%d-%s' % (nombre, args.desde, args.hasta, args.paso,
                                           'progresivo' if args.forma == 'sube' else args.forma)
        progresivo(anclas, tempos, base, salida, args, codec)
        escribir_config(salida, args)
        print('Hecho: %s.m4a + %s.xml' % (base, base))
        return

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

    escribir_config(salida, args)
    print('Hecho: %d archivos.' % len(tempos))


if __name__ == '__main__':
    main()
