"""Immutable external citations, goal changes and original personal before/refs."""
import asyncio
import unittest
import sqlite3

from app.db import database, transaction
from app.repository import RequestContext, RepositoryError
from app.web_citations import citation_view
from fakes import seed_test_suggestion
import test_agent_web


class WebCitationTests(unittest.TestCase):
    setUp=test_agent_web.AgentWebTests.setUp
    settings=test_agent_web.AgentWebTests.settings
    run_context=test_agent_web.AgentWebTests.run_context
    prepare=test_agent_web.AgentWebTests.prepare
    lookup=test_agent_web.AgentWebTests.lookup
    execute=test_agent_web.AgentWebTests.execute

    def ready(self):
        self.prepare()
        result=self.execute(self.lookup())
        self.reference_id=result['references'][0]['referenceId']
        current=self.repo.get_session_context(self.context)
        block=current['draft']['blocks'][0]
        self.proposal={'operation':'replace','target':{'blockId':block['blockId'],'expectedBlockRevision':block['blockRevision']},
            'before':block['markdown'],'after':'# '+block['markdown'],'reason':'保留个人原句并参考岗位关注点调整排版',
            'sourceRefs':block['sourceRefs'],'gaps':[],'externalReferenceIds':[self.reference_id]}

    def save(self):return seed_test_suggestion(self.repo,self.context,self.proposal)

    def test_suggestion_has_separate_external_links_without_changing_personal_refs(self):
        self.ready();result=self.save()
        self.assertEqual(result['sourceRefs'],self.proposal['sourceRefs'])
        self.assertEqual(result['externalReferences'][0]['referenceId'],self.reference_id)
        self.assertNotIn('external',str(result['sourceRefs']))
        with database(self.path,read_only=True) as c:
            self.assertEqual(len(citation_view(c,self.sid,suggestion_id=result['suggestionId'])),1)

    def test_unknown_external_id_rolls_back_proposal(self):
        self.ready();self.proposal['externalReferenceIds']=['external-forged']
        with database(self.path,read_only=True) as c:before=c.execute('SELECT count(*) FROM suggestions').fetchone()[0]
        with self.assertRaises(RepositoryError):self.save()
        with database(self.path,read_only=True) as c:self.assertEqual(c.execute('SELECT count(*) FROM suggestions').fetchone()[0],before)

    def test_external_ref_cannot_replace_required_personal_source(self):
        self.ready();self.proposal['sourceRefs']=[{'sourceId':self.reference_id,'sourceRevision':1,'quote':'熟悉SQL'}]
        with self.assertRaises(RepositoryError):self.save()

    def test_closed_permission_rejects_new_proposal_but_preserves_existing_history(self):
        self.ready();proposal=self.save();self.settings(False,2)
        self.assertEqual(self.repo.get_suggestion(self.user,proposal['suggestionId'])['externalReferences'][0]['referenceId'],self.reference_id)
        with self.assertRaises(RepositoryError):self.save()

    def test_goal_changed_without_business_revision_rejects_old_adoption(self):
        self.ready();proposal=self.save()
        before=self.repo.get_draft(self.user)
        with database(self.path) as c,transaction(c):c.execute('UPDATE sessions SET direction=? WHERE session_id=?',('新公司 广州 工程实习',self.sid))
        current=self.repo.get_session(self.user)
        with self.assertRaises(RepositoryError) as error:
            self.repo.apply_suggestion_decision(RequestContext(self.sid,current['stateRevision']),proposal['suggestionId'],
                {'decision':'accept','expectedBlockRevision':proposal['target']['expectedBlockRevision'],'feedback':''})
        self.assertEqual(error.exception.code,'stale_state')
        self.assertEqual(self.repo.get_draft(self.user),before)

    def test_turning_off_web_does_not_block_explicit_adoption_of_published_suggestion(self):
        self.ready();proposal=self.save();self.settings(False,2)
        current=self.repo.get_session(self.user)
        result=self.repo.apply_suggestion_decision(RequestContext(self.sid,current['stateRevision']),proposal['suggestionId'],
            {'decision':'accept','expectedBlockRevision':proposal['target']['expectedBlockRevision'],'feedback':''})
        self.assertEqual(result['suggestion']['status'],'accepted')

    def test_existing_assessment_and_citations_cannot_cross_sessions(self):
        self.ready();other=self.repo.create_session('构造公司 广州 工程实习')
        with database(self.path) as c,transaction(c):
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO suggestion_external_references VALUES (?,? ,?)",(other['sessionId'],'missing',self.reference_id))

    def test_same_url_known_closed_cannot_be_bypassed_by_different_target_key(self):
        self.ready()
        with database(self.path) as c,transaction(c):
            c.execute('''INSERT INTO web_requests(session_id,request_id,run_id,resume_generation,web_settings_revision,target_key,operation,provider,request_hash,input_json,status,created_at)
                         VALUES (?,'historical','run-test-001',1,2,'different-previous-target','fetch','volcengine','hash','{}','succeeded','2026-10-28')''',(self.sid,))
            c.execute('''INSERT INTO external_references(reference_id,session_id,request_id,title,url,content,retrieved_at,match_status,hiring_status,content_status)
                         VALUES ('old-closed',?,'historical','old','https://example.com/jobs/1','当前职位已下线','2026-10-28','matched','closed','unavailable')''',(self.sid,))
        with self.assertRaises(RepositoryError) as error:self.save()
        self.assertEqual(error.exception.code,'stale_state')
