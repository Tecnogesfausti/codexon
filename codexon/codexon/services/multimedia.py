"""Bounded multimedia uploads, local STT, sampled video and background jobs."""
from __future__ import annotations
import asyncio
from array import array
import json
import math
from pathlib import Path
import re
import shutil
import time
import uuid
import httpx
from services.multimedia_ha_tools import connection
from services.multimedia_inference import infer_multimedia, read_actions

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_DURATION_SECONDS = 600
MAX_FRAMES = 12
SAMPLE_BYTES_PER_SECOND = 16000 * 2


async def run_media(args: list[str], *, timeout: int = 60) -> bytes:
    process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise ValueError('No se pudo leer este archivo multimedia. El formato está dañado o no es compatible.')
    return output


async def probe_media(path: Path, content_type: str) -> dict:
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('Falta instalar FFmpeg para procesar multimedia')
    metadata = json.loads(await run_media(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_streams', '-show_format', '-of', 'json', str(path)], timeout=20))
    streams = metadata.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    format_name = metadata.get('format', {}).get('format_name', '')
    if format_name in {'hls', 'dash', 'concat', 'ffmetadata'}:
        raise ValueError('Sube el archivo de audio o vídeo, no una lista de reproducción')
    duration = metadata.get('format', {}).get('duration')
    if duration is None:
        duration = next((s.get('duration') for s in streams if s.get('duration')), None)
    try:
        duration = float(duration or 0)
    except (ValueError, TypeError):
        duration = 0
    if not math.isfinite(duration) or duration > MAX_DURATION_SECONDS:
        raise ValueError('El archivo supera el límite de 10 minutos')
    is_image = bool(video and not audio and (content_type.startswith('image/') or format_name.endswith('_pipe') or format_name in {'image2', 'gif', 'apng', 'ico'}))
    if not video and not audio:
        raise ValueError('El archivo no contiene imagen, audio o vídeo compatible')
    if video and int(video.get('width') or 0) * int(video.get('height') or 0) > 40_000_000:
        raise ValueError('La imagen o el vídeo supera los 40 megapíxeles')
    if not is_image and duration <= 0:
        raise ValueError('No se pudo determinar la duración del audio o vídeo')
    return {'kind': 'image' if is_image else 'video' if video else 'audio', 'duration_seconds': round(duration, 2),
            'has_audio': bool(audio), 'has_video': bool(video)}


async def transcribe_pcm(path: Path, progress) -> str:
    base, token = connection()
    provider = 'stt.faster_whisper'
    transcript = []
    data = path.read_bytes()
    chunk_bytes = SAMPLE_BYTES_PER_SECOND * 30
    count = math.ceil(len(data) / chunk_bytes)
    async with httpx.AsyncClient(timeout=120) as client:
        for index in range(count):
            progress(f'Transcribiendo audio {index + 1}/{count} con Whisper local…')
            chunk = data[index * chunk_bytes:(index + 1) * chunk_bytes]
            samples = array('h', chunk[:len(chunk) - len(chunk) % 2])
            if not samples or sum(sample * sample for sample in samples) / len(samples) < 2500:
                continue
            response = await client.post(base + '/api/stt/' + provider, content=chunk,
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'audio/wav',
                         'X-Speech-Content': 'format=wav; codec=pcm; sample_rate=16000; bit_rate=16; channel=1; language=es'})
            if response.status_code != 200:
                raise RuntimeError(f'Whisper local no pudo transcribir el audio (HTTP {response.status_code})')
            result = response.json()
            if result.get('result') != 'success':
                raise RuntimeError('Whisper local no pudo transcribir el audio')
            text = str(result.get('text') or '').strip()
            if text:
                transcript.append(f'[{index * 30:02d}s] {text}')
    return '\n'.join(transcript)


async def prepare_media(source: Path, content_type: str, frames: int, progress) -> dict:
    metadata = await probe_media(source, content_type)
    directory = source.parent
    images, timestamps = [], []
    common = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '2', '-protocol_whitelist', 'file,pipe']
    scale = "scale='min(1280,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease"
    if metadata['has_video']:
        count = 1 if metadata['kind'] == 'image' else min(frames, max(1, math.ceil(metadata['duration_seconds'] * 2)))
        for index in range(count):
            progress(f'Preparando imagen {index + 1}/{count}…')
            timestamp = 0 if metadata['kind'] == 'image' else metadata['duration_seconds'] * (index + .5) / count
            image = directory / f'frame_{index:02d}.jpg'
            await run_media([*common, '-ss', str(timestamp), '-i', str(source), '-an', '-frames:v', '1',
                             '-vf', scale, '-q:v', '3', '-y', str(image)])
            if not image.exists() or not image.stat().st_size:
                raise ValueError('No se pudo extraer un fotograma del vídeo')
            images.append(image)
            timestamps.append(round(timestamp, 2))
    transcript = ''
    if metadata['has_audio']:
        progress('Preparando el audio para Whisper local…')
        pcm = directory / 'audio.pcm'
        await run_media([*common, '-i', str(source), '-vn', '-t', str(MAX_DURATION_SECONDS + 1),
                         '-ar', '16000', '-ac', '1', '-f', 's16le', '-acodec', 'pcm_s16le', '-y', str(pcm)])
        if pcm.stat().st_size > SAMPLE_BYTES_PER_SECOND * MAX_DURATION_SECONDS:
            raise ValueError('El audio supera el límite de 10 minutos')
        transcript = await transcribe_pcm(pcm, progress)
    context = f'Tipo: {metadata["kind"]}. Duración: {metadata["duration_seconds"]} segundos.\n'
    if timestamps:
        context += 'Imágenes adjuntas, en orden, tomadas en los segundos: ' + ', '.join(map(str, timestamps)) + '.\n'
    context += 'TRANSCRIPCIÓN DEL AUDIO:\n' + (transcript or '(no hay voz reconocible)')
    return {'images': images, 'transcript': transcript, 'metadata': {**metadata, 'frames': len(images), 'frame_timestamps': timestamps}, 'context': context}


class MultimediaJobs:
    def __init__(self, root: Path):
        self.root = root
        self.active: dict[str, asyncio.Task] = {}

    def write(self, job_id: str, state: dict):
        path = self.root / (job_id + '.json')
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)

    def get(self, job_id: str) -> dict:
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise FileNotFoundError
        path = self.root / (job_id + '.json')
        state = json.loads(path.read_text(encoding='utf-8'))
        if state['status'] in {'queued', 'running'} and job_id not in self.active:
            directory = self.root / job_id
            actions = read_actions(directory / 'actions.jsonl')
            state.update(status='interrupted', message='El proceso se interrumpió. Revisa las acciones antes de repetir la petición.',
                         result={'actions': actions, 'answer': 'Proceso interrumpido.'})
            self.write(job_id, state)
            shutil.rmtree(directory, ignore_errors=True)
        return state

    async def submit(self, upload, *, question: str, model: str | None, execute: bool, frames: int) -> dict:
        question = question.strip()
        if len(question) > 4000:
            raise ValueError('La petición supera los 4000 caracteres')
        if frames not in {4, 8, 12}:
            raise ValueError('Selecciona 4, 8 o 12 fotogramas')
        if len(self.active) >= 2:
            raise ValueError('Ya hay dos peticiones multimedia en curso; espera a que terminen')
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        # Only diagnostic results are retained. Remove abandoned uploads and old results after 24h.
        for path in self.root.iterdir():
            if path.stem in self.active or time.time() - path.stat().st_mtime < 86400:
                continue
            if path.is_dir() and re.fullmatch(r'[0-9a-f]{32}', path.name):
                shutil.rmtree(path, ignore_errors=True)
            elif path.suffix == '.json' and re.fullmatch(r'[0-9a-f]{32}', path.stem):
                path.unlink(missing_ok=True)
        job_id = uuid.uuid4().hex
        directory = self.root / job_id
        directory.mkdir(mode=0o700)
        source = directory / 'upload'
        try:
            size = 0
            with source.open('wb') as stream:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError('El archivo supera el límite de 100 MB')
                    stream.write(chunk)
            if not size:
                raise ValueError('El archivo está vacío')
            if len(self.active) >= 2:
                raise ValueError('Ya hay dos peticiones multimedia en curso; espera a que terminen')
            state = {'id': job_id, 'status': 'queued', 'message': 'Preparando multimedia…', 'created_at': time.time(),
                     'filename': Path(upload.filename or 'multimedia').name[:200], 'execute': execute}
            self.write(job_id, state)
            task = asyncio.create_task(self._worker(state, source, str(upload.content_type or ''), question, model, execute, frames))
            self.active[job_id] = task
            return state
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    async def _worker(self, state, source, content_type, question, model, execute, frames):
        job_id = state['id']
        prepared = None
        def progress(message):
            state.update(status='running', message=message)
            self.write(job_id, state)
        try:
            async with asyncio.timeout(1200):
                prepared = await prepare_media(source, content_type, frames, progress)
                if execute and not question and not prepared['transcript']:
                    raise ValueError('Escribe qué quieres ejecutar; el archivo no contiene una orden de voz reconocible')
                progress('Analizando y ejecutando la petición…' if execute else 'Analizando multimedia…')
                result = await infer_multimedia(images=prepared['images'], context=prepared['context'], question=question,
                    model=model, execute=execute, directory=source.parent)
                result.update(transcript=prepared['transcript'], media=prepared['metadata'])
                state.update(status='partial' if result.get('error') else 'completed', message='Petición terminada.', result=result)
        except (Exception, asyncio.CancelledError) as exc:
            actions = read_actions(source.parent / 'actions.jsonl')
            cancelled = isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError))
            message = 'El proceso se interrumpió o tardó demasiado. Revisa las acciones antes de repetirlo.' if cancelled else str(exc)
            if not isinstance(exc, (ValueError, RuntimeError, httpx.HTTPError)) and not cancelled:
                message = 'No se pudo procesar el archivo multimedia.'
            state.update(status='interrupted' if cancelled else 'failed', message=message,
                result={'answer': message, 'error': message, 'actions': actions,
                        'transcript': prepared['transcript'] if prepared else ''})
        finally:
            try:
                self.write(job_id, state)
            finally:
                shutil.rmtree(source.parent, ignore_errors=True)
                self.active.pop(job_id, None)
