"""Direct HA visual worker with bounded, independent ChatGPT subscription reviews."""
from __future__ import annotations
import asyncio
import contextlib
import fcntl
import json
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import httpx
import websockets
from codexon import MemoryStore
from site_profile import SiteProfile
from services.chatgpt_image_analysis import _run, completed_answer, completed_usage
from services.codex_subscription import subscription_environment
from services.multimedia_ha_tools import connection
from services.security_image_storage import put_setting
from services.security_visual import (
    SecurityVisualSupervisor, VisualVerdict, VISUAL_SYSTEM_PROMPT, utc_now, timestamp,
    inconclusive, validate_verdict, model_setting,
)
from tools.homeassistant import ha_websocket_url

PRIMARY_MODEL = 'gpt-6-astra'
SECOND_MODEL = 'gpt-6.1-sol'


async def review_with_subscription(*, case_id, candidate, frames, model, effort, timeout):
    executable = shutil.which('codex')
    if not executable:
        raise RuntimeError('Codex no está instalado')
    with tempfile.TemporaryDirectory(prefix='security-visual-') as folder:
        directory = Path(folder)
        schema = directory/'verdict.schema.json'
        schema.write_text(json.dumps(VisualVerdict.model_json_schema()))
        args = [executable, 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
                '--json', '--sandbox', 'read-only', '--cd', folder, '--model', model, '--output-schema', str(schema),
                '-c', 'web_search="disabled"', '-c', 'project_doc_max_bytes=0', '-c', 'skills.include_instructions=false',
                '-c', 'model_reasoning_effort='+json.dumps(effort)]
        for feature in ('shell_tool','unified_exec','code_mode','apps','plugins','multi_agent','image_generation',
                        'view_image','memories','js_repl','search_tool','tool_search'):
            args.extend(['-c',f'features.{feature}=false'])
        for index, frame in enumerate(frames):
            image = directory/f'frame_{index}.jpg'
            image.write_bytes(frame['bytes'])
            args.extend(['--image',str(image)])
        args.append('-')
        evidence = {'candidate_id': str(case_id), 'camera': candidate.get('camera_entity'),
                    'detected_at': candidate['detected_at'], 'detector_box': candidate.get('box'),
                    'frames': [{k:v for k,v in f.items() if k not in {'bytes','path'}} for f in frames]}
        prompt = (VISUAL_SYSTEM_PROMPT+' Sé conciso: hasta tres observaciones y dos limitaciones. '
                  'No intentes ejecutar herramientas. No presupongas que la etiqueta del detector es correcta. '
                  'La caja puede usar coordenadas de una resolución distinta a la imagen. '
                  'Los frames llevan roles: event_anchor es la captura original; temporal_context son momentos próximos del mismo evento; '
                  'detector_crop es un recorte del original, no otro momento ni más resolución real. '
                  'Respeta time_uncertainty_seconds: los fotogramas de grabación pueden variar hasta un segundo. '
                  'La secuencia puede estar incompleta. La ausencia en otros momentos o en el recorte no permite descartar una persona en el original.\n'+json.dumps(evidence,ensure_ascii=False))
        code, output = await _run(args, env=subscription_environment(), timeout=timeout, prompt=prompt.encode())
        if code:
            raise RuntimeError('Falló la consulta a ChatGPT')
        return validate_verdict(completed_answer(output), str(case_id), frames), completed_usage(output)


def combine_reviews(case_id, results):
    for value in results:
        if value['visual_verdict'] == 'person_visible':
            return value
    if len(results) == 2 and all(v['visual_verdict'] == 'supported_false_positive' for v in results):
        return results[0]
    return inconclusive(str(case_id), 'Las dos revisiones no confirman la falsa detección; se mantiene el aviso.')


class ChatGPTVisualSupervisor(SecurityVisualSupervisor):
    def __init__(self, agent, **kwargs):
        super().__init__(agent, **kwargs)
        config = agent.site_profile.binding('security.visual_filter') or {}
        self.primary = config.get('primary_model', PRIMARY_MODEL)
        self.secondary = config.get('secondary_model', SECOND_MODEL)
        self.effort = config.get('reasoning_effort', 'high')
        if self.primary == self.secondary:
            raise ValueError('El filtro necesita dos modelos distintos')
        self.selected_model()
        put_setting(self.conn, 'security_visual.filter_config', {'enabled': self.filter_enabled, 'timeout_seconds': 30,
            'scope':'frigate_person_event','requires':'two_distinct_models_matched_frame','provider':'chatgpt',
            'primary_model':self.primary,'secondary_model':self.secondary,'reasoning_effort':self.effort})

    def selected_model(self):
        primary = getattr(self, 'primary', PRIMARY_MODEL)
        put_setting(self.conn, 'security_visual.model_selection', {'requested_model':primary,'effective_model':primary,
            'fallback_reason':None,'provider':'chatgpt','reasoning_effort':getattr(self,'effort','high')})
        return primary

    async def model_watchdog(self, **kwargs):
        return  # Subscription failures are recorded explicitly; never fall back to a billed API.

    async def capture(self, case_id, candidate, config):
        if not candidate.get('event_id') or not config.frigate_base_url:
            return await super().capture(case_id,candidate,config)
        from services.security_visual_sequence import capture_sequence
        return await capture_sequence(self,case_id,candidate,config)

    async def _review(self, case_id, candidate, frames, model, budget):
        metadata=[{k:v for k,v in f.items() if k!='bytes'} for f in frames]
        review_id=self.conn.execute('INSERT INTO security_visual_reviews(case_id,started_at,model,llm_called,frames_json) VALUES(?,?,?,?,?)',
            (case_id,utc_now(),model,1,json.dumps(metadata))).lastrowid
        self.conn.commit();started=time.monotonic();error=None
        result=inconclusive(str(case_id),'No llegó una respuesta válida del modelo dentro del plazo.')
        try:
            result,usage=await review_with_subscription(case_id=case_id,candidate=candidate,frames=frames,model=model,effort=self.effort,timeout=budget)
            if usage:
                self.memory.add_usage_event(model=model,prompt_tokens=usage.get('input_tokens'),completion_tokens=usage.get('output_tokens'),
                    total_tokens=usage.get('total_tokens'),estimated_cost_usd=None,context='security_visual',provider='chatgpt_subscription',
                    duration_ms=int((time.monotonic()-started)*1000),router_reason='Filtro visual ChatGPT; razonamiento '+self.effort)
        except asyncio.CancelledError:
            error='Cancelled';raise
        except Exception as exc:
            error=type(exc).__name__
        finally:
            self.conn.execute('UPDATE security_visual_reviews SET finished_at=?,result_json=?,duration_ms=?,error_code=? WHERE id=?',
                (utc_now(),json.dumps(result,ensure_ascii=False),int((time.monotonic()-started)*1000),error,review_id));self.conn.commit()
        return result

    async def process_one(self):
        row=self.conn.execute("SELECT * FROM security_visual_cases WHERE status='pending' ORDER BY CASE WHEN EXISTS(SELECT 1 FROM security_visual_filter_requests f WHERE f.case_id=security_visual_cases.id AND f.status='pending') THEN 0 ELSE 1 END,id LIMIT 1").fetchone()
        if row is None:return False
        case_id=row['id'];candidate=json.loads(row['candidate_json'])
        config=next((c for c in self.configs if c.name==row['camera']),None)
        if config is None:
            self._finish(case_id,inconclusive(str(case_id),'Cámara no configurada'));return True
        kind=candidate.get('kind')
        drill=isinstance(candidate.get('simulacro'),dict)
        if drill and not self.simulacro_case_allowed(candidate) or self.simulacro_active() and not drill:
            self._finish(case_id,inconclusive(str(case_id),'Revisión fuera del simulacro activo.'));return True
        if self.arming.enabled and kind not in {'installation_check','historical_check','simulacro'}:
            decision=candidate.get('arming') or self.arming.decision(config.name,candidate.get('frame_time') or candidate['detected_at'])
            if not decision.get('allowed',True):
                self._finish(case_id,inconclusive(str(case_id),'La cámara no estaba armada para este evento.'));return True
        age=time.time()-(timestamp(candidate.get('received_at')) or 0)
        if not candidate.get('event_id') and age>config.max_event_age_seconds:
            self._finish(case_id,inconclusive(str(case_id),'Evento antiguo sin imagen correspondiente.'));return True
        requests=self.conn.execute("SELECT deadline FROM security_visual_filter_requests WHERE case_id=? AND status='pending'",(case_id,)).fetchall()
        deadline=min([r['deadline'] for r in requests],default=time.time()+30)-1.5
        if deadline-time.time()<3:
            self._finish(case_id,inconclusive(str(case_id),'Plazo visual agotado antes de iniciar la revisión.'));return True
        calls=self.conn.execute("SELECT count(*) FROM security_visual_reviews r JOIN security_visual_cases c ON c.id=r.case_id WHERE c.camera=? AND r.llm_called=1 AND r.started_at>=?",(config.name,utc_now()[:10])).fetchone()[0]
        if config.daily_call_limit > 0 and calls+2>config.daily_call_limit:
            self._finish(case_id,inconclusive(str(case_id),'Límite diario de revisiones alcanzado; aviso conservado.'));return True
        last=self.conn.execute("SELECT r.case_id,r.started_at FROM security_visual_reviews r JOIN security_visual_cases c ON c.id=r.case_id WHERE c.camera=? AND r.llm_called=1 ORDER BY r.id DESC LIMIT 1",(config.name,)).fetchone()
        if last and last['case_id']!=case_id and kind not in {'historical_check','installation_check'} and time.time()-(timestamp(last['started_at']) or 0)<config.cooldown_seconds:
            self._finish(case_id,inconclusive(str(case_id),'Intervalo mínimo entre revisiones; aviso conservado.'));return True
        self.conn.execute("UPDATE security_visual_cases SET status='reviewing',review_count=review_count+1 WHERE id=?",(case_id,));self.conn.commit()
        try:
            frames=await asyncio.wait_for(self.capture(case_id,candidate,config),timeout=min(5,max(.1,deadline-time.time()-2)))
            if not frames:raise ValueError('Falta una imagen del evento')
            budget=min(26,max(.1,deadline-time.time()))
            values=await asyncio.gather(*(self._review(case_id,candidate,frames,model,budget) for model in (self.primary,self.secondary)))
            result=combine_reviews(case_id,values)
            errors=self.conn.execute('SELECT error_code FROM security_visual_reviews WHERE case_id=? AND llm_called=1 ORDER BY id DESC LIMIT 2',(case_id,)).fetchall()
            error=next((r['error_code'] for r in errors if r['error_code']),None)
            put_setting(self.conn,'security_visual.ai_status',{'at':utc_now(),'requested_model':self.primary,'model':self.primary,
                'provider':'chatgpt','reasoning_effort':self.effort,'state':'error' if error else 'ok','error_code':error})
        except asyncio.CancelledError:
            self._finish(case_id,inconclusive(str(case_id),'Revisión interrumpida; aviso conservado.'));raise
        except Exception as exc:
            result=inconclusive(str(case_id),'Captura o revisión no disponible; aviso conservado.')
            put_setting(self.conn,'security_visual.ai_status',{'at':utc_now(),'model':self.primary,'state':'error','error_code':type(exc).__name__})
        self._finish(case_id,result)
        return True


async def event_reader(supervisor, stop):
    url=ha_websocket_url(supervisor.agent)
    while not stop.is_set():
        try:
            async with websockets.connect(url,max_size=8*1024*1024,ping_interval=None) as ws:
                await ws.recv();await ws.send(json.dumps({'type':'auth','access_token':supervisor.agent.ha_token}))
                auth=json.loads(await ws.recv())
                if auth.get('type')!='auth_ok':raise RuntimeError('No se pudo autenticar con Home Assistant')
                for ident,event_type in enumerate(('state_changed','codexon_security_review_requested','codexon_security_filter_outcome','codexon_security_filter_probe_result'),1):
                    await ws.send(json.dumps({'id':ident,'type':'subscribe_events','event_type':event_type}))
                    while True:
                        item=json.loads(await ws.recv())
                        if item.get('type')=='event':supervisor.offer(item['event']);continue
                        if item.get('id')==ident:
                            if not item.get('success'):raise RuntimeError('No se pudo suscribir al evento')
                            break
                supervisor.connection_status(True)
                await supervisor.refresh_arming()
                while not stop.is_set():
                    try:item=json.loads(await asyncio.wait_for(ws.recv(),timeout=5))
                    except asyncio.TimeoutError:continue
                    if item.get('type')=='event':supervisor.offer(item['event'])
        except asyncio.CancelledError:raise
        except Exception as exc:
            supervisor.connection_status(False)
            put_setting(supervisor.conn,'security_visual.worker_error',{'at':utc_now(),'error_code':type(exc).__name__})
            with contextlib.suppress(asyncio.TimeoutError):await asyncio.wait_for(stop.wait(),timeout=3)


async def run_visual_worker(db_path: Path, profile_path: Path, stop: asyncio.Event):
    lock_path=db_path.parent/'security_visual.worker.lock'
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open('a') as lock:
        try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        memory=MemoryStore(db_path)
        tasks=[]
        worker_error=None
        try:
            profile=SiteProfile.load(profile_path)
            base,token=connection()
            agent=SimpleNamespace(memory=memory,site_profile=profile,ha_base_url=base,ha_token=token,httpx=httpx)
            supervisor=ChatGPTVisualSupervisor(agent,log=lambda *a,**k:None)
            async def heartbeat():
                while not stop.is_set():
                    put_setting(memory.conn,'security_visual.heartbeat',utc_now())
                    put_setting(memory.conn,'security_visual.worker',{'at':utc_now(),'state':'running','host':'web','provider':'chatgpt','primary_model':supervisor.primary,'secondary_model':supervisor.secondary,'reasoning_effort':supervisor.effort,'timeout_seconds':30})
                    with contextlib.suppress(asyncio.TimeoutError):await asyncio.wait_for(stop.wait(),timeout=5)
            tasks=[asyncio.create_task(event_reader(supervisor,stop)),asyncio.create_task(heartbeat()),asyncio.create_task(supervisor.run(stop))]
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:raise
        except Exception as exc:
            worker_error=type(exc).__name__
            put_setting(memory.conn,'security_visual.worker',{'at':utc_now(),'state':'error','error_code':type(exc).__name__})
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            put_setting(memory.conn,'security_visual.connection',{'connected':False,'updated_at':utc_now()})
            put_setting(memory.conn,'security_visual.worker',{'at':utc_now(),'state':'error' if worker_error else 'stopped','host':'web','error_code':worker_error})
            memory.conn.close()
