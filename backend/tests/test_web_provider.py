"""Provider format/URL/privacy checks using MockTransport, never live DNS/API."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from app.web_provider import PublicTarget, VolcWebClient, WebProviderError, WebSettings, public_url, public_url_shape


async def public_resolver(_host):
    return ['93.184.216.34']


def search_response(text='岗位职责：接口开发；岗位要求：SQL', url='https://example.com/jobs/1'):
    return {'ResponseMetadata': {'RequestId': 'synthetic'}, 'Result': {'ErrorCode': 0, 'Documents': [
        {'Title':'构造公司工程实习', 'Url':url, 'Snippet':[{'Type':'text','Text':text}], 'DocumentInfo':{}}]}}


def fetch_response(url='https://example.com/jobs/1', text=None, code=200, redirect=None):
    return {'ResponseMetadata': {'RequestId':'synthetic'}, 'Result': {'Data': {
        'StatusCode':code, 'Url':url, 'RedirectedUrl':redirect, 'Title':'构造岗位',
        'ContentText': text if text is not None else ('岗位职责：负责接口开发和需求整理。岗位要求：熟悉SQL和HTTP。' * 8)}}}


class PublicQueryTests(unittest.TestCase):
    def test_environment_defaults_off_and_key_alone_does_not_enable_search(self):
        with patch.dict('os.environ',{},clear=True):
            self.assertFalse(WebSettings.from_env().available)
        with patch.dict('os.environ',{'VOLC_SEARCH_API_KEY':'unit-key','WEB_SEARCH_ENABLED':'0','AGENT_BUDGET_CONFIRMED':'1'}):
            self.assertFalse(WebSettings.from_env().available)
        with patch.dict('os.environ',{'VOLC_SEARCH_API_KEY':'unit-key','WEB_SEARCH_ENABLED':'1','AGENT_BUDGET_CONFIRMED':'1'}):
            self.assertTrue(WebSettings.from_env().available)
    def test_queries_only_contain_bounded_public_fields_and_key_is_stable(self):
        target = PublicTarget('SAP','工程实习','上海','实习','REF2624S')
        self.assertEqual(target.query(), 'SAP 工程实习 上海 实习 REF2624S')
        self.assertEqual(target.query(short=True), 'SAP 工程实习 REF2624S')
        self.assertEqual(target.key, PublicTarget('SAP','工程实习','上海','实习','REF2624S').key)
        self.assertNotEqual(target.key, PublicTarget('SAP','工程实习','广州','实习','REF2624S').key)

    def test_contact_information_multiline_and_overlong_queries_rejected(self):
        for company in ('用户@example.com','联系电话13800138000','第一行\n第二行','https://example.com'):
            with self.subTest(company=company), self.assertRaises(WebProviderError):
                PublicTarget(company,'工程实习')
        with self.assertRaises(WebProviderError):
            PublicTarget('公'*60,'岗'*80).query()
        self.assertNotIn('unit-key',repr(WebSettings('unit-key',True,True)))

    def test_private_credentials_protocols_and_nonstandard_ports_rejected(self):
        for url in ('http://localhost/x','http://127.0.0.1','http://10.0.0.1','http://169.254.169.254',
                    'http://[::1]','http://[::ffff:127.0.0.1]','https://a.internal',
                    'file:///tmp/a','ftp://example.com','https://user:pass@example.com',
                    'https://example.com:8443/x','https://example.com?api_key=x','https://example.com\\@127.0.0.1'):
            with self.subTest(url=url), self.assertRaises(WebProviderError):
                public_url_shape(url)
        self.assertEqual(public_url_shape('https://example.com/jobs/1#part'), 'https://example.com/jobs/1')
        self.assertEqual(public_url_shape('https://93.184.216.34/x'), 'https://93.184.216.34/x')


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, resolver=public_resolver):
        return VolcWebClient(WebSettings('unit-key',True,True), transport=httpx.MockTransport(handler), resolver=resolver)

    async def test_global_parameters_and_text_snippets_not_full_body(self):
        sent=[]
        def handler(request):
            sent.append(json.loads(request.content))
            self.assertTrue(str(request.url).endswith('/global_search'))
            return httpx.Response(200,json=search_response('unit-key\n'+('岗位职责：开发。岗位要求：SQL。'*20)))
        dispatched=[]
        refs,warnings=await self.client(handler).search('构造公司 工程实习',lambda:dispatched.append(True))
        self.assertEqual(len(dispatched),1)
        self.assertEqual(sent[0]['DocCount'],5)
        self.assertEqual(sent[0]['MaxSnippetLength'],1000)
        self.assertNotIn('Count',sent[0])
        self.assertEqual(refs[0]['contentStatus'],'partial')
        self.assertNotIn('unit-key',json.dumps(refs))
        self.assertEqual(warnings,[])

    async def test_dns_mixed_public_private_and_rebinding_redirects_rejected(self):
        async def mixed(_host): return ['93.184.216.34','127.0.0.1']
        with self.assertRaises(WebProviderError):
            await public_url('https://example.com/x',mixed)
        sent=[]
        def handler(request):
            sent.append(request)
            return httpx.Response(200,json=fetch_response(redirect='http://127.0.0.1/private'))
        with self.assertRaises(WebProviderError):
            await self.client(handler).fetch('https://example.com/jobs/1',lambda:None)
        self.assertEqual(len(sent),1)

    async def test_fetch_target_failure_empty_body_and_explicit_offline(self):
        for text,code,expected in (('登录',403,'unavailable'),('',200,'unavailable'),('该职位已下线',200,'unavailable'),(None,200,'sufficient')):
            with self.subTest(text=text,code=code):
                refs,warnings=await self.client(lambda _:httpx.Response(200,json=fetch_response(text=text,code=code))).fetch('https://example.com/jobs/1',lambda:None)
                self.assertEqual(refs[0]['contentStatus'],expected)
                if text=='该职位已下线':self.assertEqual(refs[0]['hiringStatus'],'closed')
                if code!=200:self.assertIn('source_unavailable',warnings)

    async def test_bad_json_shape_quota_rate_limit_and_redirect_not_retried(self):
        responses=[httpx.Response(200,content=b'not json'), httpx.Response(200,json=[]),
                   httpx.Response(200,json={'ResponseMetadata':{},'Result':{'Documents':'wrong'}}),
                   httpx.Response(200,json={'ResponseMetadata':{'Error':{'Code':'10406','Message':'unit-key'}},'Result':None}),
                   httpx.Response(429,json={}),httpx.Response(302,headers={'location':'https://example.com'})]
        for response in responses:
            sent=[]
            def handler(_request):sent.append(True);return response
            with self.subTest(status=response.status_code), self.assertRaises(WebProviderError) as error:
                await self.client(handler).search('构造查询',lambda:None)
            self.assertEqual(len(sent),1)
            self.assertNotIn('unit-key',str(error.exception))

    async def test_null_unsafe_and_key_bearing_candidates_dropped(self):
        data=search_response()
        documents=data['Result']['Documents']
        documents.extend([{'Title':'无链接','Url':None,'Snippet':[]},
                          {'Title':'内网','Url':'http://192.168.1.1','Snippet':[]},
                          {'Title':'泄漏','Url':'https://example.com/unit-key','Snippet':[]}])
        refs,warnings=await self.client(lambda _:httpx.Response(200,json=data)).search('构造查询',lambda:None)
        self.assertEqual(len(refs),1)
        self.assertEqual(len(warnings),3)
