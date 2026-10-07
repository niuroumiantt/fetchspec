"""Exercise the SSH launcher with the real manage.py CLI and an isolated runtime."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fetchspec.legacy_catalog import export
from test_legacy_catalog import fixture


MOCK_SSH = r'''#!/usr/bin/env python3
import json,os,subprocess,sys
from pathlib import Path
with open(os.environ['LAUNCHER_COMMAND_LOG'],'a') as out:
    out.write(json.dumps(sys.argv[1:])+'\n')
host=sys.argv[1]
command=' '.join(sys.argv[2:])
root=Path(os.environ['LAUNCHER_RECEIVER'])
env={**os.environ,'PYTHONPATH':str(root/'src')}
if host=='mini' and command.startswith('bash -s'):
    sys.stdin.buffer.read()
elif host=='mini' and command.startswith('cat '):
    sys.stdout.buffer.write(Path(os.environ['LAUNCHER_BUNDLE']).read_bytes())
elif host=='inews' and 'systemctl start' in command:
    if 'docker exec -w /app -e PYTHONPATH=/app/src' not in command:
        raise SystemExit('module check must resolve the container source package')
    subprocess.run([sys.executable,'-c','import inresearch.workflow.catalog_bundle'],cwd=root,env=env,check=True)
    print('HEALTHY: TEST_COMMIT')
elif host=='inews' and 'import-bundle' in command:
    if 'docker exec -i -w /app ' not in command or 'python3 manage.py product-catalog import-bundle' not in command:
        raise SystemExit('import must use the executable source-checkout CLI')
    if os.environ.get('LAUNCHER_EMPTY_RECEIPT'):
        sys.stdin.buffer.read()
    else:
        result=subprocess.run([sys.executable,'manage.py','product-catalog','import-bundle','--company','supermicro','--input','-'],cwd=root,env=env)
        raise SystemExit(result.returncode)
elif host=='inews' and 'curl -fsS' in command:
    print(json.dumps({'verification_requested':True}))
else:
    raise SystemExit('unexpected SSH command')
'''


@unittest.skipUnless(os.environ.get('FETCHSPEC_LEGACY_INRESEARCH_ROOT'),
                     'set FETCHSPEC_LEGACY_INRESEARCH_ROOT for real CLI launcher tests')
class LegacyLauncherTests(unittest.TestCase):
    def launch(self, directory, empty_receipt=False):
        root=Path(directory)
        fixture(root/'legacy')
        report=export(root/'legacy',root/'output')
        commands=root/'commands.jsonl'
        mock=root/'bin/ssh';mock.parent.mkdir()
        mock.write_text(MOCK_SSH);mock.chmod(0o755)
        receiver=Path(os.environ['FETCHSPEC_LEGACY_INRESEARCH_ROOT']).resolve()
        env={**os.environ,'HOME':str(root/'user'),
             'PATH':str(mock.parent)+os.pathsep+os.environ['PATH'],
             'PYTHONPATH':'',
             'INRESEARCH_RUNTIME_ROOT':str(root/'runtime'),
             'LAUNCHER_COMMAND_LOG':str(commands),
             'LAUNCHER_RECEIVER':str(receiver),
             'LAUNCHER_BUNDLE':report['bundle']}
        if empty_receipt: env['LAUNCHER_EMPTY_RECEIPT']='1'
        script=Path(__file__).resolve().parents[1]/'scripts/publish_supermicro_legacy_from_m5.sh'
        result=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,timeout=30)
        calls=[json.loads(line) for line in commands.read_text().splitlines()]
        return result,calls,root

    def test_launcher_invokes_real_cli_and_writes_persistent_database_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            result,calls,root=self.launch(temp)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            receipts=list((root/'user/.local/state/fetchspec').glob('*receipt.json'))
            self.assertEqual(len(receipts),1)
            receipt=json.loads(receipts[0].read_text())
            self.assertTrue(receipt['ok'])
            self.assertEqual((receipt['added_products'],receipt['indexed_materials']),(3,2))
            self.assertTrue((root/'runtime/data/raw/product-catalog/supermicro.sqlite3').is_file())
            self.assertIn('verification_requested',result.stdout)

    def test_empty_successful_process_output_does_not_pass_as_import_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            result,calls,root=self.launch(temp,empty_receipt=True)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(any('curl -fsS' in ' '.join(call) for call in calls))
            self.assertFalse((root/'runtime/data/raw/product-catalog/supermicro.sqlite3').exists())
