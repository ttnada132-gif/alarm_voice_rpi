import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import battery_sensor as sensor


class BatteryCalibrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'battery_calibration.json'
        setting = patch.object(sensor, 'CALIBRATION_FILE', self.path)
        setting.start()
        self.addCleanup(setting.stop)

    def test_missing_file_is_created_and_used_for_measurement(self):
        with patch.object(sensor, 'read_raw', return_value=8914) as read:
            self.assertEqual(sensor.read_battery(), {'voltage': 12.7, 'percent': 100})
        self.assertEqual(json.loads(self.path.read_text()), sensor.DEFAULT_CALIBRATION)
        read.assert_called_once_with(bus_number=1, address=72)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_existing_device_calibration_is_preserved(self):
        existing = b'{"bus": 1, "address": 73, "full_raw": 8000, "full_voltage": 12, "empty_voltage": 9}\n'
        self.path.write_bytes(existing)
        with patch.object(sensor, 'read_raw', return_value=8000) as read:
            self.assertEqual(sensor.read_battery(), {'voltage': 12, 'percent': 100})
        read.assert_called_once_with(bus_number=1, address=73)
        self.assertEqual(self.path.read_bytes(), existing)

    def test_malformed_existing_file_is_reported_without_overwrite(self):
        self.path.write_text('invalid json')
        with self.assertRaises(json.JSONDecodeError):
            sensor.load_calibration()
        self.assertEqual(self.path.read_text(), 'invalid json')

    def test_concurrently_saved_calibration_is_preserved(self):
        custom = {**sensor.DEFAULT_CALIBRATION, 'full_raw': 7000}
        def save_concurrently(source, target):
            Path(target).write_text(json.dumps(custom))
            raise FileExistsError()
        with patch.object(sensor.os, 'link', side_effect=save_concurrently):
            self.assertEqual(sensor.load_calibration(), custom)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_publish_leaves_no_partial_file(self):
        with patch.object(sensor.os, 'link', side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                sensor.load_calibration()
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_embedded_defaults_match_repository_calibration(self):
        reference = Path(__file__).with_name('battery_calibration.json')
        self.assertEqual(sensor.DEFAULT_CALIBRATION, json.loads(reference.read_text()))


if __name__ == '__main__':
    unittest.main()
