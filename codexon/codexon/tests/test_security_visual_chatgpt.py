import asyncio
import json
import unittest
from unittest.mock import patch
from services.security_visual_chatgpt import ChatGPTVisualSupervisor, combine_reviews
from services.security_visual import recent_cases, inconclusive
from tests.test_security_visual import VisualWorkerTests, frigate_event, verdict

class SubscriptionReviewTests(VisualWorkerTests):
    async def test_parallel_models_and_usage_without_openrouter(self):
        self.worker=ChatGPTVisualSupervisor(self.agent,configs=(self.config,),log=lambda *a,**k:None)
        self.worker.capture = self.old_capture = __import__('unittest.mock',fromlist=['AsyncMock']).AsyncMock(return_value=self.frames)
        self.worker.offer(frigate_event())
        entered=set()
        async def review(**kwargs):
            entered.add(kwargs['model'])
            for _ in range(20):
                if len(entered)==2:break
                await asyncio.sleep(.001)
            self.assertEqual(len(entered),2)
            return verdict(visual_verdict='supported_false_positive',person_count=0,alternative_explanation='Rama visible'), {'input_tokens':10,'output_tokens':5,'total_tokens':15}
        with patch('services.security_visual_chatgpt.review_with_subscription',side_effect=review):
            await self.worker.process_one()
        self.assertEqual(entered,{'gpt-6-astra','gpt-6.1-sol'})
        self.assertEqual(recent_cases(self.memory.conn)[0]['result']['visual_verdict'],'supported_false_positive')
        self.create.assert_not_called()
        self.assertEqual(self.memory.conn.execute("SELECT count(*) FROM usage_events WHERE provider='chatgpt_subscription'").fetchone()[0],2)

    async def test_timeout_keeps_alert_and_records_error(self):
        self.worker=ChatGPTVisualSupervisor(self.agent,configs=(self.config,),log=lambda *a,**k:None)
        self.worker.capture=__import__('unittest.mock',fromlist=['AsyncMock']).AsyncMock(return_value=self.frames)
        self.worker.offer(frigate_event())
        with patch('services.security_visual_chatgpt.review_with_subscription',side_effect=asyncio.TimeoutError):
            await self.worker.process_one()
        self.assertEqual(recent_cases(self.memory.conn)[0]['result']['visual_verdict'],'inconclusive')
        self.assertEqual(self.memory.conn.execute("SELECT count(*) FROM security_visual_reviews WHERE error_code='TimeoutError'").fetchone()[0],2)
        self.create.assert_not_called()

    async def test_historical_case_remains_visible_when_inconclusive(self):
        cid=self.worker.offer(frigate_event())
        self.worker._finish(cid,inconclusive(str(cid),'Sin confirmar'))
        self.assertEqual(recent_cases(self.memory.conn,exclude_inconclusive=True),[])
        self.memory.conn.execute("UPDATE security_visual_cases SET candidate_json=json_set(candidate_json,'$.kind','historical_check') WHERE id=?",(cid,));self.memory.conn.commit()
        self.assertEqual(recent_cases(self.memory.conn,exclude_inconclusive=True)[0]['id'],cid)

class CombineTests(unittest.TestCase):
    def test_person_veto_and_uncertainty(self):
        false=verdict(visual_verdict='supported_false_positive',person_count=0,alternative_explanation='Rama')
        self.assertEqual(combine_reviews('1',[false,verdict()])['visual_verdict'],'person_visible')
        self.assertEqual(combine_reviews('1',[false,inconclusive('1','Sin imagen')])['visual_verdict'],'inconclusive')
        self.assertEqual(combine_reviews('1',[false,false])['visual_verdict'],'supported_false_positive')
