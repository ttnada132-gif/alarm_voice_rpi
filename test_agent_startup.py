import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class AgentStartupTests(unittest.TestCase):
    def run_audio_setup(self, card_id):
        script = Path(__file__).with_name("run_raspi_agent.sh").read_text()
        setup = script.split('HANDSET_CARD_ID=', 1)[1].split('if [ "$(id -u)"', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            detector = "raise SystemExit(1)\n" if card_id is None else f"print({card_id!r})\n"
            (root / "find_handset.py").write_text(detector)
            mixer = root / "amixer"
            mixer.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SCRIPT_DIR/mixer_calls"\nexit 1\n')
            mixer.chmod(0o755)
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail\nlog_message() { echo "$*"; }\n'
                 + 'HANDSET_CARD_ID=' + setup + '\nprintf "DEVICE=%s\\n" "$HANDSET_DEVICE"'],
                env={**os.environ, "SCRIPT_DIR": directory,
                     "PATH": directory + os.pathsep + os.environ["PATH"]},
                capture_output=True, text=True, timeout=3, check=True,
            )
            calls = root / "mixer_calls"
            return result.stdout, calls.read_text() if calls.exists() else ""

    def test_missing_usb_does_not_block_broadcast_startup(self):
        output, calls = self.run_audio_setup(None)
        self.assertIn("starting AUX broadcast reception without USB", output)
        self.assertIn("DEVICE=plughw:CARD=Device,DEV=0", output)
        self.assertEqual(calls, "")

    def test_connected_usb_keeps_detected_card_even_if_mixer_fails(self):
        output, calls = self.run_audio_setup("Device_1")
        self.assertIn("DEVICE=plughw:CARD=Device_1,DEV=0", output)
        self.assertIn("-c Device_1 sset PCM 90% unmute", calls)
        self.assertIn("-c Device_1 sset Mic 100% cap", calls)
        self.assertNotIn("unavailable", output)


class ConnectionNoticeTests(unittest.TestCase):
    def test_dns_wait_is_reported_once_and_resets_after_recovery(self):
        import io
        import socket
        from contextlib import redirect_stderr
        from urllib.error import URLError
        from raspi_agent import ConnectionNotice
        notice = ConnectionNotice()
        error = URLError(socket.gaierror(socket.EAI_AGAIN, 'temporary DNS failure'))
        output = io.StringIO()
        with redirect_stderr(output):
            notice.failed('Heartbeat', error)
            notice.failed('Heartbeat', error)
            notice.connected()
            notice.failed('Heartbeat', error)
        self.assertEqual(output.getvalue().count('자동 재시도 대기'), 2)
        self.assertNotIn('failed', output.getvalue())
        self.assertNotIn('DNS failure', output.getvalue())

    def test_other_errors_remain_visible(self):
        import io
        from contextlib import redirect_stderr
        from urllib.error import HTTPError
        from raspi_agent import ConnectionNotice
        output = io.StringIO()
        with redirect_stderr(output):
            ConnectionNotice().failed('Registration', HTTPError('http://test', 403, 'Forbidden', {}, None))
        self.assertIn('Registration failed:', output.getvalue())
        self.assertIn('403', output.getvalue())

    def test_websocket_dns_error_is_recognized(self):
        import websocket
        from raspi_agent import is_dns_error
        self.assertTrue(is_dns_error(websocket.WebSocketAddressException('DNS unavailable')))
        self.assertFalse(is_dns_error(websocket.WebSocketTimeoutException('timeout')))


if __name__ == "__main__":
    unittest.main()
