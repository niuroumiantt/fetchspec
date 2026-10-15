"""Frozen collection demand reaches plans without rewriting legacy source/receipt facts."""
import copy
import csv
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

from fetchspec.coverage import build, load_backflow, plan, scope_fingerprint, write_csv
from fetchspec.delivery_v2 import build_package, import_receipt
from fetchspec.targets import SOURCE_FILES, sync_targets
from test_delivery_v2 import item, receipt
from test_targets import commit, upstream


SCOPE = {
    'collection_contract': {'version': 1, 'fields': ['source_sha256', 'configuration', 'rack_power', 'test_condition'],
                            'scope_fields': ['hardware', 'configuration'], 'does_not_prove': 'site operation or adoption'},
    'request': {'version': 2, 'required_fields': ['source_sha256', 'configuration', 'rack_power', 'test_condition'],
                'requirements': [{'id': 'configuration-review', 'scope_fields': ['hardware', 'configuration']}]},
    'requirements': [{'id': 'native-locator', 'required': ['source_sha256', 'locator']}]}


class DemandScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.upstream = upstream(self.base / 'upstream')
        self.state = self.base / 'state'
        self.snapshot = sync_targets(self.upstream, self.state)

    def revise(self, scope=None, status='sourced'):
        path = self.upstream / SOURCE_FILES[1]
        doc = json.loads(path.read_text())
        doc['targets'][0].update(status=status)
        if scope is not None:
            doc['targets'][0].update(copy.deepcopy(scope))
        path.write_text(json.dumps(doc))
        commit(self.upstream)
        self.snapshot = sync_targets(self.upstream, self.state)

    def deliver(self, environment='production'):
        summary = build_package(self.state, self.snapshot, 'nvidia', [item(self.base)], collector_revision='test')
        import_receipt(self.state, receipt(summary), environment=environment)
        return summary

    def row(self):
        return next(r for r in build(self.state, self.snapshot)['records'] if r['target_id'] == 'P.gpu.spec')

    def queue_row(self, **kwargs):
        return next(r for r in plan(build(self.state, self.snapshot), **kwargs)['queue'] if r['target_id'] == 'P.gpu.spec')

    def test_sourced_new_scope_stays_sourced_and_becomes_a_review_followup(self):
        self.revise(SCOPE)
        original = copy.deepcopy(self.snapshot)
        row = self.queue_row()
        self.assertEqual(row['upstream_status'], 'sourced')
        self.assertTrue(row['scope_followup']['required'])
        self.assertEqual(row['group'], 4)
        single = plan({**build(self.state, self.snapshot), 'records': [self.row()]})
        self.assertEqual(single['groups']['review_scope'], 1)
        self.assertEqual(single['groups']['in_flight'], 0)
        self.assertEqual(single['groups']['collect_now'], 0)
        self.assertIn('review current collection requirements', row['action'])
        for key in SCOPE:
            self.assertEqual(row[key], SCOPE[key])
        row['request']['requirements'][0]['scope_fields'].append('mutation')
        self.assertEqual(self.snapshot, original)
        self.assertEqual(self.queue_row()['request'], SCOPE['request'])
        report = build(self.state, self.snapshot)
        path = self.base / 'coverage.csv'
        write_csv(report, path)
        with path.open() as stream:
            records = list(csv.DictReader(stream))
        exported = next(r for r in records if r['target_id'] == 'P.gpu.spec')
        self.assertEqual(json.loads(exported['request']), SCOPE['request'])
        self.assertEqual(json.loads(exported['collection_contract']), SCOPE['collection_contract'])
        cli = subprocess.run([sys.executable, '-m', 'fetchspec.pipeline', '--root', str(self.state),
                              'coverage', '--stage', 'adapter_ready'], capture_output=True, text=True, check=True)
        projected = next(r for r in json.loads(cli.stdout)['records'] if r['target_id'] == 'P.gpu.spec')
        self.assertEqual(projected['request'], SCOPE['request'])
        self.assertEqual(projected['collection_contract'], SCOPE['collection_contract'])

    def test_old_production_receipt_cannot_suppress_new_scope(self):
        self.deliver()
        self.revise(SCOPE)
        row = self.row()
        self.assertEqual((row['stage'], row['upstream_status']), ('received', 'sourced'))
        self.assertEqual(row['received_scope_sha256'], [])
        self.assertTrue(self.queue_row()['scope_followup']['required'])
        self.deliver()
        row = self.row()
        self.assertIn(row['scope_sha256'], row['received_scope_sha256'])
        self.assertEqual(row['upstream_status'], 'sourced')
        self.assertNotIn('P.gpu.spec', {r['target_id'] for r in plan(build(self.state, self.snapshot))['queue']})
        self.assertIn('not verification', build(self.state, self.snapshot)['scope_receipt_boundary'])
        # Unrelated commit/status changes keep the same scope: no repeated collection solely for a new commit.
        self.revise(SCOPE, status='delivered')
        self.assertIn(self.row()['scope_sha256'], self.row()['received_scope_sha256'])
        self.assertNotIn('P.gpu.spec', {r['target_id'] for r in plan(build(self.state, self.snapshot))['queue']})

    def test_review_status_and_candidate_metadata_do_not_reopen_delivered_scope(self):
        scoped = copy.deepcopy(SCOPE)
        scoped['request'].update(profile='vendor_spec', node='root', question_ids=['M06-Q01'],
                                 question_links=[{'question_id': 'M06-Q01', 'relation': 'supports', 'reason': 'configuration'}])
        self.revise(scoped)
        self.deliver()
        fingerprint = self.row()['scope_sha256']
        revised = copy.deepcopy(scoped)
        revised['request']['mapping_review'] = {'reviewed_at': '2026-10-17', 'owner': 'research', 'review_version': 'new'}
        revised['request']['candidate_question_ids'] = ['candidate-only']
        revised['request']['requirements'][0].update(status='sourced', evidence_status='reviewed',
                                                    priority=1, host='m5', team_state='connected')
        revised['requirements'][0]['status'] = 'delivered'
        self.revise(revised, status='delivered')
        self.assertEqual(self.row()['scope_sha256'], fingerprint)
        self.assertEqual(self.row()['request'], revised['request'])  # metadata is retained in frozen exports
        self.assertNotIn('P.gpu.spec', {r['target_id'] for r in plan(build(self.state, self.snapshot))['queue']})
        # A different source-file SHA caused only by metadata also cannot reopen a scope via backflow.
        flow = self.backflow([{'id': 'P.gpu.spec', **revised}], 'b' * 64)
        self.assertNotIn('P.gpu.spec', {r['target_id'] for r in plan(build(self.state, self.snapshot), backflow=flow)['queue']})

    def test_substantive_fields_configuration_acceptance_and_formal_links_reopen_scope(self):
        self.revise(SCOPE)
        self.deliver()
        fingerprint = self.row()['scope_sha256']
        variants = []
        fields = copy.deepcopy(SCOPE)
        fields['collection_contract']['fields'].append('network_storage_boundary')
        variants.append(fields)
        configuration = copy.deepcopy(SCOPE)
        configuration['request']['requirements'][0]['configuration'] = {'gpu_count': 72, 'precision': 'FP8'}
        variants.append(configuration)
        acceptance = copy.deepcopy(SCOPE)
        acceptance['request']['requirements'][0]['acceptance'] = 'vendor test report with stated workload'
        variants.append(acceptance)
        questions = copy.deepcopy(SCOPE)
        questions['request']['question_ids'] = ['M06-Q02']
        questions['request']['question_links'] = [{'question_id': 'M06-Q02', 'relation': 'supports'}]
        variants.append(questions)
        profile = copy.deepcopy(SCOPE)
        profile['request'].update(profile='vendor_test_report', node='hardware')
        variants.append(profile)
        for changed in variants:
            with self.subTest(changed=changed):
                self.revise(changed)
                self.assertNotEqual(self.row()['scope_sha256'], fingerprint)
                self.assertTrue(self.queue_row()['scope_followup']['required'])
                self.assertEqual(self.queue_row()['group'], 4)

    def test_validation_or_unspecified_receipt_does_not_acknowledge_new_scope(self):
        self.revise(SCOPE)
        self.deliver('local_receiver_validation')
        self.assertTrue(self.queue_row()['scope_followup']['required'])
        self.deliver('receiver')
        self.assertEqual(self.row()['stage'], 'received')  # preserve the existing stage semantics
        self.assertEqual(self.row()['received_scope_sha256'], [])
        self.assertTrue(self.queue_row()['scope_followup']['required'])

    def test_missing_or_corrupt_frozen_snapshot_does_not_certify_scope(self):
        self.revise(SCOPE)
        self.deliver()
        path = self.state / 'targets/snapshots' / self.snapshot['snapshot_id'] / 'metadata.json'
        path.write_text('{}')
        self.assertEqual(self.row()['received_scope_sha256'], [])
        self.assertTrue(self.queue_row()['scope_followup']['required'])

    def backflow(self, records, sha=None):
        path = self.base / 'backflow.json'
        path.write_text(json.dumps({'schema_version': 1, 'team': 'fetchspec', 'records': records,
                                    'targets_sha256': sha, 'by_target': {'P.gpu.spec': {'status': 'sourced'}}}))
        return load_backflow(path, self.snapshot)

    def test_backflow_preserves_full_requirements_and_hashes_without_mutation(self):
        self.revise(SCOPE)
        target_sha = self.snapshot['upstream']['files'][SOURCE_FILES[1]]['sha256']
        record = {'id': 'P.gpu.spec', 'team': 'fetchspec', **copy.deepcopy(SCOPE)}
        flow = self.backflow([record], target_sha)
        original = copy.deepcopy(flow)
        row = self.queue_row(backflow=flow)
        self.assertEqual(row['scope_followup']['backflow_targets_sha256'], target_sha)
        self.assertFalse(row['scope_followup']['snapshot_sync_required'])
        for key in SCOPE:
            self.assertEqual(row[key], SCOPE[key])
            self.assertEqual(row['scope_followup']['backflow_demand'][key], SCOPE[key])
        row['scope_followup']['backflow_demand']['request']['requirements'].clear()
        self.assertEqual(flow, original)

    def test_backflow_scope_drift_is_visible_and_requires_snapshot_sync(self):
        self.revise(status='sourced')
        record = {'id': 'P.gpu.spec', 'team': 'fetchspec', **copy.deepcopy(SCOPE)}
        flow = self.backflow([record], 'a' * 64)
        row = self.queue_row(backflow=flow)
        self.assertTrue(row['scope_followup']['required'])
        self.assertTrue(row['scope_followup']['snapshot_sync_required'])
        self.assertEqual(row['group'], 4)
        single = plan({**build(self.state, self.snapshot), 'records': [self.row()]}, backflow=flow)
        self.assertEqual(single['groups']['review_scope'], 1)
        self.assertEqual(single['groups']['in_flight'], 0)
        self.assertEqual(single['groups']['collect_now'], 0)
        self.assertIn('sync-targets', row['action'])
        self.assertEqual(row['scope_followup']['backflow_demand']['collection_contract'], SCOPE['collection_contract'])
        self.assertEqual(row['scope_followup']['snapshot_demand'], {})
        self.assertEqual(row['upstream_status'], 'sourced')
        self.assertNotIn('collection_contract', self.snapshot['targets'][0])

    def test_bad_or_ambiguous_backflow_requirements_do_not_replace_scope(self):
        self.revise(SCOPE)
        flow = self.backflow([{'id': 'P.gpu.price', **SCOPE}, {'id': 'P.gpu.spec', 'request': 'bad'}])
        self.assertEqual(flow['requirements_by_target'], {})
        self.assertEqual(flow['dropped'], 2)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.backflow([{'id': 'P.gpu.spec', **SCOPE}, {'id': 'P.gpu.spec', **SCOPE}])
        with self.assertRaisesRegex(ValueError, 'requirements'):
            scope_fingerprint({'request': {'requirements': 'bad'}})
        with self.assertRaisesRegex(ValueError, 'scope field'):
            scope_fingerprint({'request': 'bad'})
        flow = self.backflow([{'id': ['not-a-string'], **SCOPE}])
        self.assertEqual(flow['dropped'], 1)


if __name__ == '__main__':
    unittest.main()
