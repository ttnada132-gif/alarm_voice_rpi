import argparse
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from raspi_agent import build_sos_payload, post_sos, SOS_URL


class SosTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(device_id='pi-001', sos_aid=None, sos_nid=None)

    def test_payload_uses_real_coordinates_and_missing_battery(self):
        payload = build_sos_payload(self.args, {'latitude': 37.1, 'longitude': 127.2})
        self.assertEqual(payload['aid'], 'pi-001')
        self.assertEqual(payload['node']['nid'], 'pi-001')
        self.assertEqual(payload['state'], 'normal')
        self.assertEqual(payload['node']['state'], 'normal')
        self.assertEqual(payload['time'], payload['node']['time'])
        self.assertTrue(payload['time'].endswith('+09:00'))
        self.assertEqual(payload['node']['measurements'], {
            'signal': [{'type': 'help', 'value': 'on'}],
            'gps': {'lat': 37.1, 'lon': 127.2},
            'battery': {'voltage': None, 'percent': None},
        })

    def test_configured_ids_and_missing_gps(self):
        self.args.sos_aid = 'SH_GW_0001'
        self.args.sos_nid = 'SH_GAS_0001'
        payload = build_sos_payload(self.args, None)
        self.assertEqual(payload['aid'], self.args.sos_aid)
        self.assertEqual(payload['node']['nid'], self.args.sos_nid)
        self.assertEqual(payload['node']['measurements']['gps'], {'lat': None, 'lon': None})

    @patch('raspi_agent.urlopen')
    def test_http_url_headers_body_and_plain_response(self, urlopen):
        response = MagicMock()
        response.read.return_value = b'OK'
        urlopen.return_value.__enter__.return_value = response
        payload = build_sos_payload(self.args, {'latitude': 0.0, 'longitude': 0.0})
        post_sos('test-token', payload)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, SOS_URL)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Token'), 'test-token')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 10)

    @patch('raspi_agent.urlopen')
    def test_missing_token_does_not_send_request(self, urlopen):
        with self.assertRaisesRegex(ValueError, 'SOS_TOKEN'):
            post_sos('', build_sos_payload(self.args, None))
        urlopen.assert_not_called()

    @patch('raspi_agent.urlopen')
    def test_http_failure_is_not_treated_as_success(self, urlopen):
        urlopen.side_effect = HTTPError(SOS_URL, 500, 'error', {}, None)
        with self.assertRaises(HTTPError):
            post_sos('test-token', build_sos_payload(self.args, None))


if __name__ == '__main__':
    unittest.main()
