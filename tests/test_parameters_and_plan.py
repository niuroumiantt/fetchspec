"""整理 → 输出: reviewed cell mappings become contract parameter observations; 需求: plan queue."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fetchspec.coverage import build as coverage, instance_adapters, load_backflow, plan
from fetchspec.delivery_v2 import OBSERVATION_FIELDS, build_package
from fetchspec.products import ProductStore
from fetchspec.targets import SOURCE_FILES, sync_targets
from test_targets import commit, documents

URL = 'https://www.nvidia.com/en-us/data-center/h200/'


def upstream_with_operation(root):
    """Current contract shape plus the P.gpu.operation row that TDP evidence answers."""
    import subprocess
    root.mkdir()
    subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
    contract, document = documents()
    operation = copy.deepcopy(document['targets'][0])
    operation.update(id='P.gpu.operation', variable_class=2, instances=['NVIDIA H200 datasheet TDP'])
    vertiv = copy.deepcopy(document['targets'][0])
    vertiv.update(id='P.ups.spec', part_id='ups', instances=['Vertiv Liebert EXL', 'Eaton 9395'], next_due='2026-10-05')
    orphan = copy.deepcopy(document['targets'][0])
    orphan.update(id='P.transformer.spec', part_id='transformer', instances=['Hitachi Energy'], next_due='2026-10-02')
    document['targets'][2:2] = [operation, vertiv, orphan]
    (root / 'framework').mkdir()
    for relative, value in zip(SOURCE_FILES, (contract, document)):
        (root / relative).write_text(json.dumps(value))
    commit(root)
    return root


class ParameterObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / 'state'
        self.snapshot = sync_targets(upstream_with_operation(self.base / 'upstream'), self.state)
        archive = self.base / 'archive'
        archive.mkdir()
        body = b'<html><h1>NVIDIA H200 GPU</h1><table><tr><td>TDP</td><td>Up to 700W</td></tr><tr><td>Memory</td><td>141GB</td></tr></table></html>'
        sha = hashlib.sha256(body).hexdigest()
        (archive / (sha + '.html')).write_bytes(body)
        self.sha = sha
        rows = [[{'text': 'TDP', 'header': True, 'colspan': 1, 'rowspan': 1}, {'text': 'Up to 700W', 'header': False, 'colspan': 1, 'rowspan': 1}],
                [{'text': 'Memory', 'header': True, 'colspan': 1, 'rowspan': 1}, {'text': '141GB', 'header': False, 'colspan': 1, 'rowspan': 1}]]
        self.catalog = {'company_id': 'nvidia',
                        'sources': [{'source_url': URL, 'sha256': sha, 'snapshot_path': sha + '.html', 'observed_at': '2026-09-29T00:00:00+00:00',
                                     'format': 'html', 'language': 'en'}],
                        'products': [{'id': 'nvidia-h200', 'name': 'NVIDIA H200 GPU', 'kind': 'named_product', 'parent_id': None,
                                      'source_url': URL, 'source_sha256': sha, 'observed_at': '2026-09-29T00:00:00+00:00',
                                      'categories': ['Data Center'],
                                      'tables': [{'section': 'Specifications', 'rows': rows, 'source_refs': [{'url': URL, 'sha256': sha}]}]}]}
        self.store = ProductStore(self.state)
        self.addCleanup(self.store.close)
        self.store.ingest_catalog(self.catalog, archive)
        self.store.bind(self.snapshot, 'nvidia', ['nvidia-h200'], ['P.gpu.spec', 'P.gpu.operation'], 'official H200 page')

    def package(self):
        items = self.store.delivery_items(self.snapshot, 'nvidia', ['nvidia-h200'])
        summary = build_package(self.state, self.snapshot, 'nvidia', items, collector_revision='test')
        return json.loads((Path(summary['package']) / 'manifest.json').read_text())

    def test_without_mappings_the_manifest_has_no_observation_section(self):
        manifest = self.package()
        self.assertNotIn('parameter_observations', manifest)
        self.assertNotIn('parameter_observations', manifest['items'][0]['product_evidence'][0])

    def test_target_scoped_mapping_keeps_original_text_and_contract_fields(self):
        with self.assertRaisesRegex(ValueError, 'snapshot is required'):
            self.store.map_field('nvidia', 'nvidia-h200', 1, 1, 2, 'gpu.tdp.max', 'W', 'up to', 'reviewer', target_ids=['P.gpu.operation'])
        with self.assertRaisesRegex(ValueError, 'unknown target'):
            self.store.map_field('nvidia', 'nvidia-h200', 1, 1, 2, 'gpu.tdp.max', 'W', 'up to', 'reviewer',
                                 target_ids=['P.nope.spec'], snapshot=self.snapshot)
        self.store.map_field('nvidia', 'nvidia-h200', 1, 1, 2, 'gpu.tdp.max', 'W', 'up to; configurable', 'reviewer',
                             target_ids=['P.gpu.operation'], snapshot=self.snapshot)
        self.store.map_field('nvidia', 'nvidia-h200', 1, 2, 2, 'gpu.memory.capacity', 'GB', '', 'reviewer')
        manifest = self.package()
        self.assertEqual(manifest['parameter_observations']['count'], 3)
        observed = manifest['items'][0]['product_evidence'][0]['parameter_observations']
        pairs = sorted((o['target_id'], o['parameter_name'], o['value']) for o in observed)
        self.assertEqual(pairs, [('P.gpu.operation', 'gpu.memory.capacity', '141GB'),
                                 ('P.gpu.operation', 'gpu.tdp.max', 'Up to 700W'),
                                 ('P.gpu.spec', 'gpu.memory.capacity', '141GB')])  # TDP stays off the spec row
        for row in observed:
            self.assertTrue(set(OBSERVATION_FIELDS) <= set(row))
            self.assertEqual((row['source_url'], row['source_sha256'], row['part_id']), (URL, self.sha, 'gpu'))

    def test_remapping_replaces_the_target_scope(self):
        for scope in (['P.gpu.operation'], ['P.gpu.spec']):
            self.store.map_field('nvidia', 'nvidia-h200', 1, 1, 2, 'gpu.tdp.max', 'W', '', 'reviewer',
                                 target_ids=scope, snapshot=self.snapshot)
        observed = self.package()['items'][0]['product_evidence'][0]['parameter_observations']
        self.assertEqual([o['target_id'] for o in observed], ['P.gpu.spec'])

    def test_plan_ranks_in_flight_then_collectable_then_missing_adapters(self):
        self.assertEqual(instance_adapters({'instances': ['Vertiv Liebert EXL', 'Eaton 9395']}), ['vertiv'])
        self.assertEqual(instance_adapters({'instances': ['Hitachi Energy']}), [])
        result = plan(coverage(self.state, self.snapshot))
        order = [(row['target_id'], row['group']) for row in result['queue']]
        self.assertEqual(order[0][1], 0)  # bound rows are already moving
        self.assertEqual({t for t, g in order if g == 0}, {'P.gpu.spec', 'P.gpu.operation'})
        self.assertIn(('P.ups.spec', 1), order)
        self.assertIn(('P.transformer.spec', 3), order)
        self.assertEqual(result['groups']['new_adapter'], 1)
        ups = next(row for row in result['queue'] if row['target_id'] == 'P.ups.spec')
        self.assertTrue(ups['action'].startswith('collect-seeds --target P.ups.spec --bind'))  # reviewed seed in seeds/vertiv.json
        self.assertTrue(ups['seeds'])
        self.assertIn('rated_power', ups['parameter_hints'])
        unseeded = plan(coverage(self.state, self.snapshot, seed_dir=self.base / 'no-seeds'))
        ups = next(row for row in unseeded['queue'] if row['target_id'] == 'P.ups.spec')
        self.assertIn('collect --company vertiv', ups['action'])
        self.assertEqual((ups['seeds'], unseeded['groups']), ([], result['groups']))


    def backflow(self, rows, **extra):
        path = self.base / 'backflow.json'
        path.write_text(json.dumps({'schema_version': 1, 'team': 'fetchspec', 'generated_at': '2026-10-01T00:00:00Z',
                                    'by_target': rows, **extra}))
        return load_backflow(path, self.snapshot)

    def test_backflow_rejects_other_teams_and_drops_bad_rows(self):
        with self.assertRaisesRegex(ValueError, 'team fetchspec'):
            self.backflow({}, team='inews')
        flow = self.backflow({'P.ups.spec': {'status': 'needed', 'received_items': 2, 'companies': ['vertiv']},
                              'P.gpu.price': {'status': 'needed'},            # not a Fetchspec row
                              'P.transformer.spec': {'status': 'bogus'},       # bad status
                              'P.gpu.spec': {'status': 'needed', 'received_items': -1}})
        self.assertEqual(set(flow['by_target']), {'P.ups.spec'})
        self.assertEqual(flow['dropped'], 3)
        with self.assertRaisesRegex(ValueError, 'https'):
            load_backflow('http://inresearch.ai/api/targets/backflow', self.snapshot)

    def test_backflow_moves_received_rows_up_and_drops_closed_rows(self):
        flow = self.backflow({'P.ups.spec': {'status': 'needed', 'received_items': 2, 'companies': ['vertiv']},
                              'P.transformer.spec': {'status': 'delivered', 'received_items': 1}})
        result = plan(coverage(self.state, self.snapshot), backflow=flow)
        ids = [row['target_id'] for row in result['queue']]
        self.assertNotIn('P.transformer.spec', ids)  # registered upstream: nothing left for us
        ups = next(row for row in result['queue'] if row['target_id'] == 'P.ups.spec')
        self.assertEqual(ups['group'], 0)
        self.assertIn('deliveries import', ups['action'])
        self.assertEqual(result['backflow']['delivered_upstream'], 1)

if __name__ == '__main__':
    unittest.main()
