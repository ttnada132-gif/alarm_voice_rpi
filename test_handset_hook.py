import unittest
from unittest.mock import Mock
from handset_hook import HookController


class HookTests(unittest.TestCase):
    def setUp(self):
        self.dial = Mock(return_value=True)
        self.end = Mock(return_value=True)
        self.silence = Mock()
        self.hook = HookController(self.dial, self.end, self.silence)

    def test_initial_on_hook_does_not_call(self):
        self.hook.changed(False)
        self.hook.sync()
        self.dial.assert_not_called()
        self.end.assert_not_called()

    def test_lift_once_and_replace_ends(self):
        self.hook.changed(True)
        self.hook.sync()
        self.hook.changed(True)
        self.hook.sync()
        self.dial.assert_called_once()
        self.hook.changed(False)
        self.silence.assert_called_once()
        self.hook.sync()
        self.hook.sync()
        self.end.assert_called_once()

    def test_offline_lift_cancelled_by_replacement(self):
        self.dial.return_value = False
        self.hook.changed(True)
        self.hook.sync()
        self.hook.changed(False)
        self.hook.sync()
        self.dial.return_value = True
        self.hook.sync()
        self.assertEqual(self.dial.call_count, 1)

    def test_failed_end_retries_before_next_dial(self):
        self.hook.changed(True)
        self.hook.sync()
        self.hook.changed(False)
        self.end.side_effect = [OSError('offline'), True]
        with self.assertRaises(OSError):
            self.hook.sync()
        self.hook.changed(True)
        self.hook.sync()
        self.assertEqual(self.dial.call_count, 1)
        self.hook.sync()
        self.assertEqual(self.dial.call_count, 2)

    def test_replacement_during_dial_is_not_lost(self):
        def dial():
            self.hook.changed(False)
            return True
        self.dial.side_effect = dial
        self.hook.changed(True)
        self.hook.sync()
        self.hook.sync()
        self.end.assert_called_once()


if __name__ == '__main__':
    unittest.main()
