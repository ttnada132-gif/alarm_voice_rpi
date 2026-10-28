import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import wifi_portal as portal


class StopManager(BaseException):
    """Stop the background loop without being handled as a connection failure."""


class WifiTimingTests(unittest.TestCase):
    def test_legacy_and_missing_waits_upgrade_without_rewriting_device_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wifi_portal.json'
            for timing in ({}, {'fail_seconds': 60, 'connect_timeout_seconds': 25}):
                original = json.dumps({**timing, 'ap_ssid': 'custom-setup', 'stream_token': 'test-token'})
                path.write_text(original)
                cfg = portal.load_config(path)
                self.assertEqual(cfg['fail_seconds'], 180)
                self.assertEqual(cfg['connect_timeout_seconds'], 180)
                self.assertEqual(cfg['ap_ssid'], 'custom-setup')
                self.assertEqual(cfg['stream_token'], 'test-token')
                self.assertEqual(path.read_text(), original)

    def test_longer_device_waits_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wifi_portal.json'
            cfg = {'fail_seconds': 300, 'connect_timeout_seconds': 240}
            path.write_text(json.dumps(cfg))
            self.assertEqual(portal.load_config(path), cfg)

    def test_station_attempt_allows_late_success_and_times_out_at_three_minutes(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                manager = portal.Manager({'connect_timeout_seconds': 180})
                now = [0]

                def sleep(seconds):
                    now[0] += seconds

                def station():
                    return (succeeds and now[0] >= 120, 'router', '192.168.1.4')

                with patch.object(portal.time, 'monotonic', side_effect=lambda: now[0]), \
                     patch.object(portal.time, 'sleep', side_effect=sleep), \
                     patch.object(manager, 'station', side_effect=station), \
                     patch.object(manager, 'create_network', return_value='123'), \
                     patch.object(manager, 'update'), patch.object(manager, 'note'), \
                     patch.object(manager, 'save_network') as save, \
                     patch.object(portal, 'command') as command, patch.object(portal, 'wpa') as wpa:
                    self.assertEqual(manager.try_station('router', 'password', persist=True), succeeds)
                    self.assertEqual(now[0], 120 if succeeds else 180)
                    if succeeds:
                        save.assert_called_once_with('router', 'password')
                        command.assert_any_call(['systemctl', 'start', portal.SERVICE], timeout=30)
                        self.assertNotIn(unittest.mock.call('remove_network', '123'), wpa.call_args_list)
                    else:
                        save.assert_not_called()
                        wpa.assert_any_call('remove_network', '123')

    def test_saved_network_retry_waits_three_minutes_and_resets_after_recovery(self):
        for recovers in (False, True):
            with self.subTest(recovers=recovers):
                manager = portal.Manager({'fail_seconds': 180, 'server_test_url': 'https://example.com',
                                          'stream_token': 'test-token'})
                now = [0]

                def sleep(seconds):
                    now[0] += seconds

                def station():
                    return (recovers and now[0] == 60, 'router', '192.168.1.4')

                with patch.object(portal, 'DHCP', Mock(read_text=Mock(return_value=''))), \
                     patch.object(portal.time, 'monotonic', side_effect=lambda: now[0]), \
                     patch.object(portal.time, 'sleep', side_effect=sleep), \
                     patch.object(manager, 'station', side_effect=station), \
                     patch.object(manager, 'update'), patch.object(manager, 'note') as note, \
                     patch.object(manager, 'start_ap', side_effect=StopManager), \
                     patch.object(portal, 'server_is_reachable', return_value=True), \
                     patch.object(portal, 'load_wifi_networks', return_value=[]) as networks, \
                     patch.object(portal, 'command'):
                    with self.assertRaises(StopManager):
                        manager.run()
                    self.assertEqual(now[0], 245 if recovers else 180)
                    networks.assert_called_once()
                    note.assert_any_call('Wi-Fi 연결 대기 (180초 후 저장된 목록 재시도)')


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.manager = portal.Manager({'admin_password': 'test-password', 'connect_timeout_seconds': 0,
                                      'ap_ip': '192.168.4.1', 'ap_ssid': 'test-setup'})
        self.client = portal.create_app(self.manager).test_client()

    def form_token(self):
        import re
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'test-password', response.data)
        return re.search(rb'name="csrf" value="([^"]+)"', response.data)[1].decode()

    def test_direct_access_and_csrf_required(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('WWW-Authenticate', response.headers)
        self.assertEqual(self.client.get('/status').status_code, 200)
        self.assertEqual(self.client.post('/save').status_code, 403)

    def test_validation_and_single_pending_change(self):
        csrf = self.form_token()
        self.assertEqual(self.client.post('/save',
                         data={'csrf': csrf, 'ssid': 'router', 'password': 'short'}).status_code, 400)
        data = {'csrf': csrf, 'ssid': 'router', 'password': '12345678'}
        self.assertEqual(self.client.post('/save', data=data).status_code, 200)
        self.assertEqual(self.client.post('/save', data=data).status_code, 409)
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

    def test_setup_ap_ignores_legacy_password(self):
        self.manager.cfg['ap_password'] = 'legacy-password'
        with patch.object(self.manager, 'create_network', return_value='123') as create, \
             patch.object(self.manager, 'update'), patch.object(self.manager, 'note'), \
             patch.object(self.manager, 'stop_ap'), patch.object(self.manager, 'static_ap_address'), \
             patch.object(portal, 'command', return_value='192.168.4.1'), \
             patch.object(portal, 'wpa', return_value='mode=AP'), \
             patch.object(portal.time, 'sleep'), patch.object(portal.subprocess, 'Popen') as process:
            process.return_value.poll.return_value = None
            self.manager.start_ap()
            create.assert_called_once_with('test-setup', '', ap=True)

    def test_open_ap_profile_has_no_psk(self):
        with patch.object(portal, 'wpa', return_value='123') as wpa:
            self.manager.create_network('test-setup', '', ap=True)
        wpa.assert_any_call('set_network', '123', 'key_mgmt', 'NONE')
        self.assertFalse(any('psk' in call.args for call in wpa.call_args_list))

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
                response = self.client.post('/connection', data=data)
                self.assertEqual(response.status_code, 200)
                saved = json.loads(config.read_text())
                self.assertEqual(saved['device_id'], data['device_id'])
                self.assertEqual(saved['stream_token'], 'test-token')
                self.assertEqual(saved['server_test_url'],
                                 'https://call.raycom.co.kr:45001/api/pi/wifi-check')
                self.assertEqual(saved['admin_password'], 'test-password')
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.manager.jobs.get_nowait(), (None, None))
                self.assertEqual(self.client.post('/connection',
                                                 data=data).status_code, 409)
                self.assertIn(b'SH_VOICE_ALARM_004', self.client.get('/').data)

    def test_connection_csrf_validation_and_write_failure(self):
        self.assertEqual(self.client.post('/connection').status_code, 403)
        data = {'csrf': self.form_token(), 'device_id': 'DEVICE_004',
                'server_url': 'https://example.com', 'stream_token': 'token'}
        for key, value in [('device_id', 'bad\nID'), ('server_url', 'file:///etc/passwd'),
                           ('server_url', 'https://example.com:bad'),
                           ('server_url', 'https://example.com?token=abc'),
                           ('stream_token', 'bad\ntoken')]:
            with patch.object(portal, 'atomic_write') as write:
                self.assertEqual(self.client.post('/connection',
                                                 data={**data, key: value}).status_code, 400)
                write.assert_not_called()
        with patch.object(portal, 'atomic_write', side_effect=OSError):
            self.assertEqual(self.client.post('/connection', data=data).status_code, 500)
            self.assertFalse(self.manager.busy)
            self.assertTrue(self.manager.jobs.empty())

    def test_ap_name_persists_without_disconnecting(self):
        csrf = self.form_token()
        self.manager.ap_id = '5'
        original = dict(self.manager.cfg)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            with patch.object(portal, 'CONFIG', config), patch.object(portal, 'command') as command:
                response = self.client.post('/setup-wifi', data={'csrf': csrf, 'ap_ssid': 'shth_setup_005'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(config.read_text()), {**original, 'ap_ssid': 'shth_setup_005'})
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.manager.ap_id, '5')
                self.assertTrue(self.manager.jobs.empty())
                self.assertFalse(self.manager.busy)
                command.assert_not_called()
                self.assertIn(b'shth_setup_005', self.client.get('/').data)

    def test_ap_name_rejects_invalid_busy_and_failed_writes(self):
        self.assertEqual(self.client.post('/setup-wifi').status_code, 403)
        data = {'csrf': self.form_token(), 'ap_ssid': 'setup'}
        with patch.object(portal, 'atomic_write') as write:
            for name in ('', 'x' * 33, '가' * 11, 'bad\nname', 'bad\x7fname'):
                self.assertEqual(self.client.post('/setup-wifi', data={**data, 'ap_ssid': name}).status_code, 400)
            self.manager.busy = True
            self.assertEqual(self.client.post('/setup-wifi', data=data).status_code, 409)
            write.assert_not_called()
        self.manager.busy = False
        with patch.object(portal, 'atomic_write', side_effect=OSError):
            self.assertEqual(self.client.post('/setup-wifi', data=data).status_code, 500)
            self.assertEqual(self.manager.cfg['ap_ssid'], 'test-setup')

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
