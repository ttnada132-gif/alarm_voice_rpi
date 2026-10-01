import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import wifi_portal as portal


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.manager = portal.Manager({'admin_password': 'test-password', 'connect_timeout_seconds': 0,
                                      'ap_ip': '192.168.4.1', 'ap_ssid': 'test-setup'})
        self.client = portal.create_app(self.manager).test_client()
        self.auth = {'Authorization': 'Basic YWRtaW46dGVzdC1wYXNzd29yZA=='}

    def form_token(self):
        import re
        response = self.client.get('/', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'test-password', response.data)
        return re.search(rb'name="csrf" value="([^"]+)"', response.data)[1].decode()

    def test_auth_and_csrf_required(self):
        self.assertEqual(self.client.get('/').status_code, 401)
        self.assertEqual(self.client.get('/status').status_code, 401)
        self.assertEqual(self.client.post('/save', headers=self.auth).status_code, 403)

    def test_validation_and_single_pending_change(self):
        csrf = self.form_token()
        self.assertEqual(self.client.post('/save', headers=self.auth,
                         data={'csrf': csrf, 'ssid': 'router', 'password': 'short'}).status_code, 400)
        data = {'csrf': csrf, 'ssid': 'router', 'password': '12345678'}
        self.assertEqual(self.client.post('/save', headers=self.auth, data=data).status_code, 200)
        self.assertEqual(self.client.post('/save', headers=self.auth, data=data).status_code, 409)
        self.assertEqual(self.manager.jobs.get_nowait(), ('router', '12345678'))

    def test_failed_connection_removes_unsaved_profile(self):
        with patch.object(self.manager, 'create_network', return_value='123'), \
             patch.object(self.manager, 'update'), patch.object(self.manager, 'note'), \
             patch.object(self.manager, 'save_network') as save, \
             patch.object(portal, 'command'), patch.object(portal, 'wpa') as wpa:
            self.assertFalse(self.manager.try_station('bad', '12345678', persist=True))
            wpa.assert_any_call('remove_network', '123')
            save.assert_not_called()

    def test_success_saves_and_resumes_agent(self):
        self.manager.cfg['connect_timeout_seconds'] = 10
        with patch.object(self.manager, 'create_network', return_value='123'), \
             patch.object(self.manager, 'station', return_value=(True, 'good', '192.168.1.4')), \
             patch.object(self.manager, 'update'), patch.object(self.manager, 'note'), \
             patch.object(self.manager, 'save_network') as save, \
             patch.object(portal, 'command') as command, patch.object(portal, 'wpa') as wpa:
            self.assertTrue(self.manager.try_station('good', '12345678', persist=True))
            save.assert_called_once_with('good', '12345678')
            command.assert_any_call(['systemctl', 'start', portal.SERVICE], timeout=30)
            self.assertNotIn(unittest.mock.call('remove_network', '123'), wpa.call_args_list)

    def test_static_address_block_is_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dhcpcd.conf'
            original = '# existing config\n'
            path.write_text(original)
            with patch.object(portal, 'DHCP', path), patch.object(portal, 'command'):
                self.manager.static_ap_address(True)
                self.manager.static_ap_address(True)
                self.assertEqual(path.read_text().count(portal.BEGIN), 1)
                self.manager.static_ap_address(False)
                self.assertEqual(path.read_text().strip(), original.strip())

    def test_wpa_fail_text_is_not_success(self):
        with patch.object(portal, 'command', return_value='FAIL'):
            with self.assertRaises(RuntimeError):
                portal.wpa('select_network', '0')

    def test_connection_saved_without_wifi_change(self):
        csrf = self.form_token()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            with patch.object(portal, 'CONFIG', config):
                data = {'csrf': csrf, 'device_id': 'SH_VOICE_ALARM_004',
                        'server_url': 'https://call.raycom.co.kr:45001/',
                        'stream_token': 'test-token'}
                response = self.client.post('/connection', headers=self.auth, data=data)
                self.assertEqual(response.status_code, 200)
                saved = json.loads(config.read_text())
                self.assertEqual(saved['device_id'], data['device_id'])
                self.assertEqual(saved['stream_token'], 'test-token')
                self.assertEqual(saved['server_test_url'],
                                 'https://call.raycom.co.kr:45001/api/pi/wifi-check')
                self.assertEqual(saved['admin_password'], 'test-password')
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.manager.jobs.get_nowait(), (None, None))
                self.assertEqual(self.client.post('/connection', headers=self.auth,
                                                 data=data).status_code, 409)
                self.assertIn(b'SH_VOICE_ALARM_004', self.client.get('/', headers=self.auth).data)

    def test_connection_auth_validation_and_write_failure(self):
        self.assertEqual(self.client.post('/connection').status_code, 401)
        self.assertEqual(self.client.post('/connection', headers=self.auth).status_code, 403)
        data = {'csrf': self.form_token(), 'device_id': 'DEVICE_004',
                'server_url': 'https://example.com', 'stream_token': 'token'}
        for key, value in [('device_id', 'bad\nID'), ('server_url', 'file:///etc/passwd'),
                           ('server_url', 'https://example.com:bad'),
                           ('server_url', 'https://example.com?token=abc'),
                           ('stream_token', 'bad\ntoken')]:
            with patch.object(portal, 'atomic_write') as write:
                self.assertEqual(self.client.post('/connection', headers=self.auth,
                                                 data={**data, key: value}).status_code, 400)
                write.assert_not_called()
        with patch.object(portal, 'atomic_write', side_effect=OSError):
            self.assertEqual(self.client.post('/connection', headers=self.auth, data=data).status_code, 500)
            self.assertFalse(self.manager.busy)
            self.assertTrue(self.manager.jobs.empty())

    def test_apply_connection_restarts_only_agent(self):
        with patch.object(portal, 'command') as command, \
             patch.object(self.manager, 'update'), patch.object(self.manager, 'note'), \
             patch.object(self.manager, 'stop_ap') as stop_ap:
            self.manager.apply_connection()
            command.assert_called_once_with(['systemctl', 'restart', portal.SERVICE], timeout=30)
            stop_ap.assert_not_called()
            command.reset_mock()
            self.manager.ap_id = '5'
            self.manager.apply_connection()
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
