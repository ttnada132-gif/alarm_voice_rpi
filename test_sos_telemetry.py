import argparse
import json
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from battery_sensor import battery_from_raw, read_battery
from raspi_agent import GpsLocationProvider, build_sos_payload, post_sos


class SosTelemetryTests(unittest.TestCase):
    def test_battery_endpoints_midpoint_and_clamping(self):
        calibration = {'full_raw': 8914, 'full_voltage': 12.7, 'empty_voltage': 10}
        for voltage, percent in [(10, 0), (12.7, 100), (11.35, 50), (9, 0), (13, 100)]:
            result = battery_from_raw(voltage / 12.7 * 8914, calibration)
            self.assertEqual(result, {'voltage': voltage, 'percent': percent})

    def test_gps_cache_is_excluded_and_failed_fix_clears_live_location(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = directory + '/gps.json'
            fix = {'latitude': 37.5, 'longitude': 127.1, 'read_at': time.time()}
            with open(cache, 'w') as f:
                json.dump(fix, f)
            provider = GpsLocationProvider(port='unused', baudrate=9600, interval_seconds=10,
                                           read_timeout_seconds=10, cache_file=cache)
            self.assertIsNone(provider.get_current_location())
            with patch.object(provider, '_read_once', return_value=fix):
                provider.get_location(force=True)
            self.assertEqual(provider.get_current_location(), fix)
            provider._current_fix_at -= 21
            self.assertIsNone(provider.get_current_location())
            with patch.object(provider, '_read_once', return_value=None):
                provider.get_location(force=True)
            self.assertIsNone(provider.get_current_location())

    def test_payload_and_sos_header_without_network(self):
        args = argparse.Namespace(sos_aid=None, sos_nid=None, device_id='test')
        battery = {'voltage': 12.7, 'percent': 100}
        payload = build_sos_payload(args, None, battery)
        self.assertEqual(payload['node']['measurements']['gps'], {'lat': None, 'lon': None})
        self.assertEqual(payload['node']['measurements']['battery'], battery)
        live = build_sos_payload(args, {'latitude': 37.5, 'longitude': 127.1}, battery)
        self.assertEqual(live['node']['measurements']['gps'], {'lat': 37.5, 'lon': 127.1})
        with patch('raspi_agent.urlopen', return_value=MagicMock()) as send:
            post_sos('test-sos-token', payload)
        request = send.call_args.args[0]
        self.assertEqual(request.get_header('Token'), 'test-sos-token')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(json.loads(request.data), payload)


if __name__ == '__main__':
    unittest.main()
