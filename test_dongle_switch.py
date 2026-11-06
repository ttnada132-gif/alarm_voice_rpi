"""Exercise the switch and rollback with fake devices; never change host networking."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).parent / 'scripts' / 'switch-alarm-dongle.sh'
MOCK = '''#!/usr/bin/python3
import os, pathlib, sys
root = pathlib.Path(os.environ['DONGLE_TEST_ROOT'])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / 'commands').open('a') as f:
    f.write(name + ' ' + ' '.join(args) + '\\n')
if name == 'id':
    print('0')
elif name == 'ip':
    if args[:2] == ['link', 'set'] and args[3] == 'name':
        (root / 'sys/class/net' / args[2]).rename(root / 'sys/class/net' / args[4])
    elif 'address' in args and os.environ['DONGLE_TEST_MODE'] != 'timeout':
        addr = '169.254.1.2' if os.environ['DONGLE_TEST_MODE'] == 'linklocal' else '192.0.2.2'
        print('4: wlan0 inet ' + addr + '/24 scope global wlan0')
elif name == 'wpa_cli':
    print('wpa_state=COMPLETED' if args[-1] == 'status' else 'OK')
elif name == 'systemctl' and args == ['start', 'dhcpcd.service']:
    marker = root / 'failed-once'
    if os.environ['DONGLE_TEST_MODE'] == 'command_failure' and not marker.exists():
        marker.touch()
        sys.exit(1)
'''


class DongleSwitchTest(unittest.TestCase):
    def run_switch(self, mode='success', existing=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ('bin', 'etc/systemd/network', 'sys/class/net', 'drivers'):
                (root / relative).mkdir(parents=True, exist_ok=True)
            for interface, driver in (('wlan0', 'brcmfmac'), ('wlan1', 'rtl88x2bu')):
                device = root / 'sys/class/net' / interface
                (device / 'device').mkdir(parents=True)
                (root / 'drivers' / driver).mkdir()
                (device / 'device/driver').symlink_to(root / 'drivers' / driver)
                (device / 'address').write_text('02:00:00:00:00:' + ('01' if interface == 'wlan0' else '02'))
            original = 'hostname\nclientid\n'
            (root / 'etc/dhcpcd.conf').write_text(original)
            if existing:
                (root / 'backup').mkdir()
            for name in ('id', 'ip', 'systemctl', 'wpa_cli', 'udevadm', 'sleep'):
                mock = root / 'bin' / name
                mock.write_text(MOCK)
                mock.chmod(0o755)
            script = SCRIPT.read_text().replace('/home/pi/dongle-config-backup', str(root / 'backup'))
            script = script.replace('/etc/', str(root / 'etc') + '/')
            script = script.replace('/sys/class/net', str(root / 'sys/class/net'))
            # Shorten only the retry count; mock sleep never waits.
            script = script.replace('{1..90}', '{1..2}')
            result = subprocess.run(['bash', '-c', script], text=True, capture_output=True,
                                    env={**os.environ, 'PATH': str(root / 'bin') + ':' + os.environ['PATH'],
                                         'DONGLE_TEST_ROOT': str(root), 'DONGLE_TEST_MODE': mode})
            names = sorted(p.name for p in (root / 'sys/class/net').iterdir())
            config = (root / 'etc/dhcpcd.conf').read_text()
            links = list((root / 'etc/systemd/network').iterdir())
            if existing:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Backup already exists', result.stderr)
                self.assertEqual(config, original)
                self.assertEqual(names, ['wlan0', 'wlan1'])
                self.assertFalse(links)
            elif mode == 'success':
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('SWITCH_COMPLETE', result.stdout)
                self.assertEqual(names, ['wlan-internal', 'wlan0'])
                self.assertIn('metric 100', config)
                self.assertEqual(len(links), 2)
                self.assertEqual((root / 'backup/dhcpcd.conf').read_text(), original)
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('ROLLED_BACK', result.stdout)
                self.assertEqual(config, original)
                self.assertEqual(names, ['wlan0', 'wlan1'])
                self.assertFalse(links)
                self.assertIn('systemctl start shinwhatech-wifi-portal.service',
                              (root / 'commands').read_text())

    def test_success(self):
        self.run_switch()

    def test_no_dhcp_rolls_back(self):
        self.run_switch('timeout')

    def test_link_local_only_rolls_back(self):
        self.run_switch('linklocal')

    def test_command_failure_rolls_back(self):
        self.run_switch('command_failure')

    def test_existing_backup_is_preserved(self):
        self.run_switch(existing=True)


if __name__ == '__main__':
    unittest.main()
