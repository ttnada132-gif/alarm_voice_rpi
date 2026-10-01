import hashlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
import os
import unittest
from unittest.mock import patch
import zipfile

import auto_update as updater
from build_release import build


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'VERSION').write_text('1.0.0\n')
        (self.root / 'wifi_portal.json').write_text('private settings')
        (self.root / 'wifi_networks.csv').write_text('private wifi')
        (self.root / 'last_location.json').write_text('saved GPS')
        (self.root / 'raspi_agent.py').write_text('old code')

    def release(self):
        payload = {name: b'# release file\n' for name in updater.FILES}
        payload['VERSION'] = b'1.0.1\n'
        manifest = {'version': '1.0.1', 'files': {
            name: hashlib.sha256(data).hexdigest() for name, data in payload.items()
        }}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            for name, data in payload.items():
                bundle.writestr('repo-sha/' + name, data)
        return manifest, archive.getvalue()

    def test_numeric_versions(self):
        self.assertGreater(updater.version('1.10.0'), updater.version('1.9.9'))
        for invalid in ('../1', '1.0', 'v1.0.0', '01.0.0'):
            with self.assertRaises(ValueError):
                updater.version(invalid)

    def test_same_or_older_version_never_downloads_archive(self):
        for remote in (b'1.0.0', b'0.9.9'):
            with patch.object(updater, 'download', side_effect=[b'{"sha":"' + b'a'*40 + b'"}', remote]) as get:
                self.assertFalse(updater.check_update(self.root))
                self.assertEqual(get.call_count, 2)

    def test_new_version_installs_and_preserves_data(self):
        manifest, archive = self.release()
        with patch.object(updater, 'download', side_effect=[
            b'{"sha":"' + b'a'*40 + b'"}', b'1.0.1', json.dumps(manifest).encode(), archive
        ]) as get:
            self.assertTrue(updater.check_update(self.root))
            self.assertIn('a'*40, get.call_args.args[0])
        self.assertEqual((self.root / 'VERSION').read_text(), '1.0.1\n')
        self.assertEqual((self.root / 'wifi_portal.json').read_text(), 'private settings')
        self.assertEqual((self.root / 'wifi_networks.csv').read_text(), 'private wifi')
        self.assertEqual((self.root / 'last_location.json').read_text(), 'saved GPS')
        self.assertEqual((self.root / '.update-backup/raspi_agent.py').read_text(), 'old code')
        self.assertTrue((self.root / 'run_raspi_agent.sh').stat().st_mode & 0o111)
        self.assertFalse((self.root / '.update-transaction').exists())

    def test_bad_hash_and_extra_path_rejected(self):
        manifest, archive = self.release()
        manifest['files']['raspi_agent.py'] = '0'*64
        with self.assertRaisesRegex(ValueError, '검증 실패'):
            updater.validate_payload(manifest, archive, '1.0.1')
        manifest['files']['../wifi_portal.json'] = '0'*64
        with self.assertRaisesRegex(ValueError, '파일 목록'):
            updater.validate_payload(manifest, archive, '1.0.1')
        self.assertEqual((self.root / 'VERSION').read_text(), '1.0.0\n')

    def test_network_failure_leaves_code_unchanged(self):
        with patch.object(updater, 'download', side_effect=OSError('offline')):
            with patch('sys.argv', ['auto_update.py', '--root', str(self.root)]):
                self.assertEqual(updater.main(), 0)
        self.assertEqual((self.root / 'raspi_agent.py').read_text(), 'old code')

    def test_failure_mid_install_restores_old_files_and_removes_new_files(self):
        original = updater.atomic_write
        failed = False
        def fail_once(path, data, mode=0o644):
            nonlocal failed
            if path == self.root / 'VERSION' and not failed:
                failed = True
                raise OSError('disk failure')
            return original(path, data, mode)
        with patch.object(updater, 'atomic_write', side_effect=fail_once):
            with self.assertRaises(OSError):
                updater.install(self.root, {'raspi_agent.py': b'new', 'auto_update.py': b'new', 'VERSION': b'1.0.1'})
        self.assertEqual((self.root / 'raspi_agent.py').read_text(), 'old code')
        self.assertEqual((self.root / 'VERSION').read_text(), '1.0.0\n')
        self.assertFalse((self.root / 'auto_update.py').exists())

    def test_interrupted_update_recovers_without_network(self):
        tx = self.root / '.update-transaction'
        tx.mkdir()
        (tx / 'VERSION').write_text('1.0.0\n')
        (tx / 'journal.json').write_text(json.dumps({'VERSION': {'mode': 0o644}, 'auto_update.py': None}))
        (self.root / 'VERSION').write_text('1.0.1\n')
        (self.root / 'auto_update.py').write_text('partial update')
        with patch.object(updater, 'download') as get:
            with patch('sys.argv', ['auto_update.py', '--root', str(self.root), '--recover-only']):
                self.assertEqual(updater.main(), 0)
            get.assert_not_called()
        self.assertEqual((self.root / 'VERSION').read_text(), '1.0.0\n')
        self.assertFalse((self.root / 'auto_update.py').exists())

    def test_symlink_target_rejected(self):
        (self.root / 'auto_update.py').symlink_to(self.root / 'wifi_portal.json')
        with self.assertRaises(ValueError):
            updater.install(self.root, {'auto_update.py': b'new'})
        self.assertEqual((self.root / 'wifi_portal.json').read_text(), 'private settings')

    def test_launcher_reexecutes_only_after_successful_install(self):
        script = Path(__file__).with_name('run_raspi_agent.sh').read_text()
        block = script.split('# Check only once', 1)[1].split('pkill -x aplay', 1)[0]
        block = '# Check only once' + block
        (self.root / 'auto_update.py').write_text('raise SystemExit(10)\n')
        (self.root / 'run_raspi_agent.sh').write_text(
            'test "$ALARM_UPDATE_RESTARTED" = 1 || exit 99\nprintf restarted\n')
        prefix = 'set -euo pipefail\nsystemctl() { :; }\nsudo() { :; }\n'
        env = {**os.environ, 'SCRIPT_DIR': str(self.root)}
        env.pop('ALARM_UPDATE_RESTARTED', None)
        result = subprocess.run(['bash', '-c', prefix + block + 'echo old-code'],
                                env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), 'restarted')
        env['ALARM_UPDATE_RESTARTED'] = '1'
        result = subprocess.run(['bash', '-c', prefix + block + 'echo current-code'],
                                env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), 'current-code')
        (self.root / 'auto_update.py').write_text('raise SystemExit(20)\n')
        env.pop('ALARM_UPDATE_RESTARTED')
        result = subprocess.run(['bash', '-c', prefix + block + 'echo unsafe-start'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 20)
        self.assertNotIn('unsafe-start', result.stdout)

    def test_checked_in_release_hashes_match_runtime_files(self):
        root = Path(__file__).resolve().parent
        manifest = json.loads((root / 'release.json').read_text())
        self.assertEqual(manifest['version'], (root / 'VERSION').read_text().strip())
        self.assertEqual(set(manifest['files']), set(updater.FILES))
        for name, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest, name)


if __name__ == '__main__':
    unittest.main()
