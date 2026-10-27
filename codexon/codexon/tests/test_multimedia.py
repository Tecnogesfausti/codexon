import asyncio
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from starlette.datastructures import UploadFile, Headers
from services import multimedia as media
from services import multimedia_ha_tools as tools
from services import multimedia_inference as inference


class MediaTest(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg not installed')
    async def test_image_and_video_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            image = p/'input.png'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=320x240', '-frames:v', '1', '-threads', '1', str(image)], check=True)
            result = await media.prepare_media(image, 'image/png', 4, lambda m: None)
            self.assertEqual(result['metadata']['kind'], 'image')
            self.assertEqual(len(result['images']), 1)
            video = p/'input.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=320x240:r=4', '-t', '2', '-threads', '1', str(video)], check=True)
            result = await media.prepare_media(video, 'video/mp4', 4, lambda m: None)
            self.assertEqual(result['metadata']['kind'], 'video')
            self.assertEqual(len(result['images']), 4)
            self.assertFalse(result['metadata']['has_audio'])
            self.assertTrue(all(x.exists() for x in result['images']))

    async def test_invalid_duration_rejected(self):
        raw = {'streams': [{'codec_type': 'audio'}], 'format': {'duration': '601'}}
        with patch.object(media, 'run_media', AsyncMock(return_value=json.dumps(raw).encode())), patch.object(media.shutil, 'which', return_value='ffmpeg'):
            with self.assertRaisesRegex(ValueError, '10 minutos'):
                await media.probe_media(Path('/tmp/test'), 'audio/mp3')

    async def test_upload_limit_and_empty_file_cleaned(self):
        with tempfile.TemporaryDirectory() as d:
            jobs = media.MultimediaJobs(Path(d))
            for content in (b'', b'abcdef'):
                upload = UploadFile(io.BytesIO(content), filename='audio.mp3')
                with patch.object(media, 'MAX_UPLOAD_BYTES', 3):
                    with self.assertRaises(ValueError):
                        await jobs.submit(upload, question='', model=None, execute=False, frames=4)
                self.assertEqual(list(Path(d).iterdir()), [])

    async def test_job_records_results_and_deletes_original(self):
        with tempfile.TemporaryDirectory() as d:
            jobs = media.MultimediaJobs(Path(d))
            upload = UploadFile(io.BytesIO(b'media'), filename='audio.mp3', headers=Headers({'content-type': 'audio/mpeg'}))
            prepared = {'images': [], 'transcript': 'Una petición', 'context': 'texto', 'metadata': {'kind': 'audio'}}
            with patch.object(media, 'prepare_media', AsyncMock(return_value=prepared)), patch.object(media, 'infer_multimedia', AsyncMock(return_value={'answer': 'Hecho', 'actions': []})):
                state = await jobs.submit(upload, question='Describe', model='test', execute=False, frames=4)
                await jobs.active[state['id']]
            finished = jobs.get(state['id'])
            self.assertEqual(finished['status'], 'completed')
            self.assertEqual(finished['result']['transcript'], 'Una petición')
            self.assertFalse((Path(d)/state['id']).exists())
            with self.assertRaises(FileNotFoundError):
                jobs.get('../../auth')

    async def test_action_without_written_or_spoken_request_is_not_run(self):
        with tempfile.TemporaryDirectory() as d:
            jobs = media.MultimediaJobs(Path(d))
            prepared = {'images': [], 'transcript': '', 'context': '', 'metadata': {}}
            with patch.object(media, 'prepare_media', AsyncMock(return_value=prepared)), patch.object(media, 'infer_multimedia', AsyncMock()) as infer:
                state = await jobs.submit(UploadFile(io.BytesIO(b'x'), filename='x'), question='', model=None, execute=True, frames=4)
                await jobs.active[state['id']]
                self.assertEqual(jobs.get(state['id'])['status'], 'failed')
                infer.assert_not_awaited()

    async def test_interrupted_job_preserves_partial_action_without_rerunning(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            jobs = media.MultimediaJobs(root)
            job_id = 'a'*32
            folder = root/job_id
            folder.mkdir()
            (folder/'upload').write_bytes(b'private')
            (folder/'actions.jsonl').write_text(json.dumps({'id': '1', 'status': 'accepted'})+'\n')
            jobs.write(job_id, {'id': job_id, 'status': 'running'})
            result = jobs.get(job_id)
            self.assertEqual(result['status'], 'interrupted')
            self.assertEqual(result['result']['actions'][0]['status'], 'accepted')
            self.assertFalse(folder.exists())


class ActionTest(unittest.IsolatedAsyncioTestCase):
    async def test_calls_known_entity_and_logs_actual_result(self):
        entity = {'entity_id': 'switch.test', 'state': 'on', 'attributes': {}}
        request = AsyncMock(side_effect=[[{'domain': 'switch', 'services': {'turn_on': {'target': {}}}}], [entity], [], entity])
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ', {'CODEXON_MULTIMEDIA_ACTION_LOG': str(Path(d)/'actions.jsonl')}), patch.object(tools, 'ha_request', request):
            result = await tools.call_service('switch', 'turn_on', ['switch.test'], 'Petición explícita del usuario')
            self.assertEqual(result['status'], 'accepted')
            self.assertEqual(result['observed_states'][0]['state'], 'on')
            self.assertEqual(inference.read_actions(Path(d)/'actions.jsonl')[0]['status'], 'accepted')
            self.assertEqual(request.await_args_list[2].args, ('POST', '/services/switch/turn_on', {'entity_id': ['switch.test']}))

    async def test_unknown_target_and_empty_target_are_not_executed(self):
        request = AsyncMock(side_effect=[[{'domain': 'light', 'services': {'turn_on': {'target': {}}}}], []]*2)
        with patch.object(tools, 'ha_request', request):
            for entities in (['light.unknown'], []):
                with self.assertRaises(ValueError):
                    await tools.call_service('light', 'turn_on', entities, 'petición')
        self.assertTrue(all(call.args[0] == 'GET' for call in request.await_args_list))

    async def test_missing_audit_prevents_execution(self):
        request = AsyncMock(side_effect=[[{'domain': 'logbook', 'services': {'log': {}}}], []])
        with patch.dict('os.environ', {}, clear=True), patch.object(tools, 'ha_request', request):
            with self.assertRaisesRegex(RuntimeError, 'registro'):
                await tools.call_service('logbook', 'log', [], 'petición', {'name': 'Prueba', 'message': 'Prueba'})
        self.assertTrue(all(call.args[0] == 'GET' for call in request.await_args_list))

    async def test_timed_out_action_is_not_retried_and_has_uncertain_record(self):
        entity = {'entity_id': 'switch.test', 'state': 'off', 'attributes': {}}
        request = AsyncMock(side_effect=[[{'domain': 'switch', 'services': {'turn_on': {'target': {}}}}], [entity], TimeoutError('timeout')])
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ', {'CODEXON_MULTIMEDIA_ACTION_LOG': str(Path(d)/'actions.jsonl')}), patch.object(tools, 'ha_request', request):
            with self.assertRaises(RuntimeError):
                await tools.call_service('switch', 'turn_on', ['switch.test'], 'petición')
            self.assertEqual(inference.read_actions(Path(d)/'actions.jsonl')[0]['status'], 'error_or_unknown')
            self.assertEqual(request.await_count, 3)

class InferenceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = {'models': [{'id': 'test', 'reasoning_effort': 'low'}], 'default_model': 'test', 'quota': None}
        self.output = b'{"type":"item.completed","item":{"type":"agent_message","text":"Respuesta"}}\n{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}'

    async def test_only_execution_mode_enables_home_assistant_tools(self):
        with tempfile.TemporaryDirectory() as d, patch.object(inference, 'read_subscription_state', AsyncMock(return_value=self.state)), patch.object(inference.shutil, 'which', return_value='codex'):
            for execute in (False, True):
                run = AsyncMock(side_effect=[(0, b'Logged in using ChatGPT'), (0, self.output)])
                with patch.object(inference, '_run', run):
                    result = await inference.infer_multimedia(images=[], context='Transcripción', question='Prueba', model='test', execute=execute, directory=Path(d))
                args = run.await_args_list[1].args[0]
                self.assertEqual(any('mcp_servers.multimedia.command=' in a for a in args), execute)
                self.assertIn('features.shell_tool=false', args)
                self.assertEqual('--approve-for-me' in args, execute)
                self.assertEqual('--sandbox' in args, not execute)
                self.assertEqual(Path(args[args.index('--cd') + 1]).parent, Path(d))
                if execute:
                    self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
                self.assertEqual(result['usage']['total_tokens'], 12)

    async def test_timeout_after_action_returns_partial_result_without_retry(self):
        with tempfile.TemporaryDirectory() as d, patch.object(inference, 'read_subscription_state', AsyncMock(return_value=self.state)), patch.object(inference.shutil, 'which', return_value='codex'):
            count = 0
            async def run(args, **kwargs):
                nonlocal count
                count += 1
                if args[1] == 'login':
                    return 0, b'Logged in using ChatGPT'
                (Path(d)/'actions.jsonl').write_text('{"id":"1","status":"accepted"}\n')
                raise asyncio.TimeoutError
            with patch.object(inference, '_run', side_effect=run):
                result = await inference.infer_multimedia(images=[], context='Voz', question='Prueba', model='test', execute=True, directory=Path(d))
            self.assertTrue(result['error'])
            self.assertEqual(result['actions'][0]['status'], 'accepted')
            self.assertEqual(count, 2)

    def test_tool_approval_failures_are_exposed_as_errors(self):
        output = b'{"type":"item.completed","item":{"type":"mcp_tool_call","tool":"call_service","status":"failed","error":{"message":"requires approval"}}}'
        self.assertIn('requires approval', inference.tool_errors(output)[0])
        self.assertEqual(inference.tool_errors(self.output), [])

    async def test_read_and_action_tool_annotations_are_accurate(self):
        listed = {tool.name: tool for tool in await tools.mcp.list_tools()}
        self.assertTrue(listed['get_services'].annotations.readOnlyHint)
        self.assertTrue(listed['find_entities'].annotations.readOnlyHint)
        self.assertFalse(listed['call_service'].annotations.readOnlyHint)
        self.assertTrue(listed['call_service'].annotations.destructiveHint)

class RenderedWebTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required to check browser JavaScript')
    def test_rendered_javascript_is_valid(self):
        import re
        import codexon_web
        html = codexon_web.index()
        script = '\n'.join(re.findall(r'<script>(.*?)</script>', html, re.S))
        result = subprocess.run(['node', '--check'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('image/*,audio/*,video/*', html)
        self.assertIn('Analizar y ejecutar mi petición', html)
