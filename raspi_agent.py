from __future__ import annotations

import argparse
import base64
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import websocket
from handset_hook import GpioHookWatcher, HookController


def is_dns_error(exc):
    """Recognize urllib and websocket DNS failures without matching log text."""
    pending = [exc]
    seen = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        if isinstance(error, (socket.gaierror, websocket.WebSocketAddressException)):
            return True
        for nested in (getattr(error, 'reason', None),
                       getattr(error, '__cause__', None), getattr(error, '__context__', None)):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


class ConnectionNotice:
    def __init__(self):
        self.waiting = False

    def failed(self, label, exc):
        if is_dns_error(exc):
            if not self.waiting:
                print(f'[연결] {label}: 네트워크 연결 준비 중 · 자동 재시도 대기', file=sys.stderr)
            self.waiting = True
        else:
            self.waiting = False
            print(f'{label} failed: {exc}', file=sys.stderr)

    def connected(self):
        self.waiting = False


class TeeStderr:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self):
        for stream in self._streams:
            stream.flush()


class TimestampedLineWriter:
    def __init__(self, stream):
        self._stream = stream
        self._at_line_start = True

    def write(self, data):
        if not data:
            return 0

        chunks = []
        for char in data:
            if self._at_line_start:
                chunks.append(time.strftime("%Y-%m-%d %H:%M:%S "))
                self._at_line_start = False
            chunks.append(char)
            if char == "\n":
                self._at_line_start = True

        rendered = "".join(chunks)
        self._stream.write(rendered)
        self._stream.flush()
        return len(data)

    def flush(self):
        self._stream.flush()


def configure_stderr_log(log_file: str) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        if os.path.exists(log_file):
            log_stat = os.stat(log_file)
            stderr_stat = os.fstat(sys.stderr.fileno())
            if log_stat.st_ino == stderr_stat.st_ino and log_stat.st_dev == stderr_stat.st_dev:
                return
        log_stream = TimestampedLineWriter(open(log_file, "a", encoding="utf-8", buffering=1))
    except (OSError, ValueError) as exc:
        print(f"Could not open log file {log_file}: {exc}", file=sys.stderr)
        return

    sys.stderr = TeeStderr(sys.stderr, log_stream)


def default_device_id() -> str:
    return socket.gethostname() or "raspberry-pi"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register a Raspberry Pi, report handset button calls, and handle two-way voice/TTS playback."
    )
    parser.add_argument("--server", required=True, help="Server URL such as http://192.168.0.10:5000")
    parser.add_argument("--token", required=True, help="Shared stream token configured on the Flask server")
    parser.add_argument("--device-id", default=default_device_id(), help="Stable Raspberry Pi ID")
    parser.add_argument("--source-name", help="Human-readable device name shown in the UI")
    parser.add_argument("--company-name", default="", help="Company name shown in the server UI")
    parser.add_argument("--install-location", default="", help="Install location shown in the server UI")
    parser.add_argument("--handset-input-device", help="ALSA input device for the USB handset microphone, for example plughw:1,0")
    parser.add_argument("--handset-speaker-device", help="ALSA output device for the USB handset speaker, for example plughw:1,0")
    parser.add_argument("--speaker-device", help="Deprecated alias for --handset-speaker-device")
    parser.add_argument("--aux-device", help="ALSA output device for AUX TTS playback, for example plughw:0,0")
    parser.add_argument("--handset-sample-rate", type=int, default=16000, help="Handset PCM sample rate in Hz")
    parser.add_argument("--speaker-sample-rate", type=int, help="Deprecated alias for --handset-sample-rate")
    parser.add_argument("--chunk-samples", type=int, default=1024, help="Microphone samples per WebSocket frame")
    parser.add_argument("--button-device", help="Linux input event device for the handset button, for example /dev/input/event0")
    parser.add_argument("--button-key-code", type=int, default=-1, help="Linux input key code to accept, or -1 for any key")
    parser.add_argument("--hook-gpio", type=int, help="BCM handset hook pin: pull-up, HIGH dials, LOW hangs up")
    parser.add_argument("--button-gpio", type=int, help="BCM GPIO pin for a handset button wired to the Pi")
    parser.add_argument("--button-debounce-ms", type=int, default=450, help="Button debounce time in milliseconds")
    parser.add_argument("--gpio-alert-pin", type=int, default=23, help="BCM GPIO pin that raises alert 0001 when LOW")
    parser.add_argument("--gpio-alert-disabled", action="store_true", help="Disable GPIO alert monitoring")
    parser.add_argument("--gpio-alert-file", default="/home/pi/shinwhatech/0001.mp3", help="MP3 file to play for GPIO alert")
    parser.add_argument("--gpio-alert-output-device", help="Optional ALSA output device for GPIO alert MP3 playback")
    parser.add_argument("--gpio-alert-interval-seconds", type=float, default=10.0, help="Seconds between GPIO alert MP3 repeats")
    parser.add_argument("--gpio-alert-led-pin", type=int, default=24, help="BCM GPIO pin set HIGH while GPIO alert is active")
    parser.add_argument("--siren-file", default="/home/pi/shinwhatech/0001.mp3", help="MP3 file to play when the server siren_on button is active")
    parser.add_argument("--siren-output-device", help="Optional ALSA output device for server siren MP3 playback")
    parser.add_argument("--log-file", default="/home/pi/shinwhatech/log.txt", help="Path to append Raspberry Pi warnings and errors")
    parser.add_argument("--gps-port", default="/dev/serial0", help="GPS serial port")
    parser.add_argument("--gps-baudrate", type=int, default=9600, help="GPS serial baud rate")
    parser.add_argument("--gps-interval-seconds", type=float, default=60.0, help="Seconds between GPS reads")
    parser.add_argument("--gps-read-timeout-seconds", type=float, default=10.0, help="Maximum seconds to wait for one GPS fix")
    parser.add_argument("--gps-cache-file", default="/home/pi/shinwhatech/last_location.json", help="Last known GPS cache file")
    parser.add_argument("--gps-disabled", action="store_true", help="Disable GPS reading")
    parser.add_argument("--ring-frequency-hz", type=int, default=440, help="USB handset ring tone frequency")
    parser.add_argument("--ring-tone-ms", type=int, default=500, help="USB handset ring tone length")
    parser.add_argument("--ring-silence-ms", type=int, default=700, help="USB handset ring silence length")
    parser.add_argument("--ring-volume", type=float, default=0.28, help="USB handset ring tone volume from 0.0 to 1.0")
    parser.add_argument("--reconnect-seconds", type=int, default=3, help="Reconnect delay after a WebSocket failure")
    parser.add_argument("--heartbeat-seconds", type=int, default=10, help="How often to POST a heartbeat")
    args = parser.parse_args()
    if not args.source_name:
        args.source_name = args.device_id
    if not args.handset_speaker_device and args.speaker_device:
        args.handset_speaker_device = args.speaker_device
    if args.speaker_sample_rate:
        args.handset_sample_rate = args.speaker_sample_rate
    return args


def build_http_url(server_url: str, path: str) -> str:
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError("Server URL must start with http://, https://, ws://, or wss://")

    scheme = parsed.scheme
    if scheme == "ws":
        scheme = "http"
    elif scheme == "wss":
        scheme = "https"

    base_path = parsed.path.rstrip("/")
    full_path = f"{base_path}{path}" if base_path else path
    return urlunparse((scheme, parsed.netloc, full_path, "", "", ""))


def build_pi_listen_ws_url(server_url: str, token: str, args: argparse.Namespace) -> str:
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError("Server URL must start with http://, https://, ws://, or wss://")

    scheme = parsed.scheme
    if scheme == "http":
        scheme = "ws"
    elif scheme == "https":
        scheme = "wss"

    base_path = parsed.path.rstrip("/")
    path = (
        f"{base_path}/ws/pi/listen/{quote(args.device_id, safe='')}"
        if base_path
        else f"/ws/pi/listen/{quote(args.device_id, safe='')}"
    )
    query = urlencode(
        {
            "token": token,
            "source_name": args.source_name,
            "company_name": args.company_name,
            "install_location": args.install_location,
        }
    )
    return urlunparse((scheme, parsed.netloc, path, "", query, ""))


def build_live_send_ws_url(server_url: str, token: str) -> str:
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError("Server URL must start with http://, https://, ws://, or wss://")

    scheme = parsed.scheme
    if scheme == "http":
        scheme = "ws"
    elif scheme == "https":
        scheme = "wss"

    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/ws/live/send" if base_path else "/ws/live/send"
    query = urlencode({"token": token})
    return urlunparse((scheme, parsed.netloc, path, "", query, ""))


def post_json(server_url: str, path: str, token: str, payload: dict) -> dict:
    request = Request(
        build_http_url(server_url, path),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Stream-Token": token,
        },
        method="POST",
    )

    with urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def build_device_payload(args: argparse.Namespace, location: dict | None = None) -> dict:
    payload = {
        "device_id": args.device_id,
        "source_name": args.source_name,
        "company_name": args.company_name,
        "install_location": args.install_location,
    }
    if location:
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is not None and longitude is not None:
            payload["latitude"] = latitude
            payload["longitude"] = longitude

    return payload


def nmea_to_decimal(raw_value, direction):
    if not raw_value or not direction:
        return None

    try:
        if direction in ("N", "S"):
            degree_len = 2
        elif direction in ("E", "W"):
            degree_len = 3
        else:
            return None

        degrees = float(raw_value[:degree_len])
        minutes = float(raw_value[degree_len:])
        decimal = degrees + (minutes / 60.0)
    except (TypeError, ValueError):
        return None

    if direction in ("S", "W"):
        decimal *= -1

    return decimal


def parse_nmea_location(sentence: str) -> tuple[float | None, float | None]:
    parts = sentence.strip().split(",")
    if not parts:
        return None, None

    if parts[0] in {"$GPGGA", "$GNGGA"} and len(parts) > 6 and parts[6] in {"1", "2", "3", "4", "5", "6", "7", "8"}:
        return nmea_to_decimal(parts[2], parts[3]), nmea_to_decimal(parts[4], parts[5])

    if parts[0] in {"$GPRMC", "$GNRMC"} and len(parts) > 6 and parts[2] == "A":
        return nmea_to_decimal(parts[3], parts[4]), nmea_to_decimal(parts[5], parts[6])

    return None, None


def build_aplay_command(sample_rate: int, device: str | None) -> list[str]:
    command = ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(sample_rate)]
    if device:
        command.extend(["-D", device])
    return command


def build_arecord_command(sample_rate: int, device: str | None) -> list[str]:
    command = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(sample_rate)]
    if device:
        command.extend(["-D", device])
    return command


def build_mpg123_command(mp3_path: str, device: str | None) -> list[str]:
    command = ["mpg123", "-q"]
    if device:
        command.extend(["-a", device])
    command.append(mp3_path)
    return command


def generate_tone_pcm(sample_rate: int, frequency_hz: int, duration_ms: int, volume: float) -> bytes:
    sample_count = max(1, int(sample_rate * duration_ms / 1000))
    amplitude = int(32767 * min(max(volume, 0.0), 1.0))
    pcm = bytearray()

    for index in range(sample_count):
        angle = 2 * math.pi * frequency_hz * index / sample_rate
        sample = int(amplitude * math.sin(angle))
        pcm.extend(sample.to_bytes(2, byteorder="little", signed=True))

    return bytes(pcm)


def generate_silence_pcm(sample_rate: int, duration_ms: int) -> bytes:
    sample_count = max(1, int(sample_rate * duration_ms / 1000))
    return b"\x00\x00" * sample_count


class Playback:
    def __init__(self, device: str | None, default_sample_rate: int, label: str):
        self._device = device
        self._default_sample_rate = default_sample_rate
        self._label = label
        self._process: subprocess.Popen | None = None
        self._sample_rate = default_sample_rate
        self._lock = threading.Lock()

    def ensure_started(self, sample_rate: int | None = None) -> None:
        desired_sample_rate = int(sample_rate or self._default_sample_rate)
        with self._lock:
            if (
                self._process is not None
                and self._process.poll() is None
                and self._sample_rate == desired_sample_rate
                and self._process.stdin is not None
            ):
                return

            self._stop_locked(force=False)
            self._process = subprocess.Popen(
                build_aplay_command(desired_sample_rate, self._device),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._sample_rate = desired_sample_rate

            if self._process.stdin is None:
                raise RuntimeError(f"Could not open aplay stdin for {self._label}.")

    def write(self, chunk: bytes, *, sample_rate: int | None = None) -> None:
        self.ensure_started(sample_rate)
        with self._lock:
            if self._process is None or self._process.stdin is None:
                raise RuntimeError(f"{self._label} playback is not available.")

            try:
                self._process.stdin.write(chunk)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                error_output = self._consume_error_output_locked()
                self._stop_locked(force=True)
                raise RuntimeError(f"{self._label} aplay write failed: {error_output or exc}") from exc

            return_code = self._process.poll()
            if return_code is not None:
                error_output = self._consume_error_output_locked()
                self._stop_locked(force=True)
                raise RuntimeError(f"{self._label} aplay exited: {error_output or return_code}")

    def stop(self, *, force: bool = False) -> None:
        with self._lock:
            self._stop_locked(force=force)

    def _stop_locked(self, *, force: bool = False) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        try:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        except Exception:
            pass

        if force:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            return

        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    def _consume_error_output_locked(self) -> str:
        process = self._process
        return ""


class RingTonePlayer:
    def __init__(self, args: argparse.Namespace, playback: Playback):
        self._args = args
        self._playback = playback
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="handset-ring-tone", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None
        self._playback.stop()

    def _run(self) -> None:
        sample_rate = self._args.handset_sample_rate
        tone = generate_tone_pcm(
            sample_rate,
            self._args.ring_frequency_hz,
            self._args.ring_tone_ms,
            self._args.ring_volume,
        )
        silence = generate_silence_pcm(sample_rate, self._args.ring_silence_ms)
        cycle_seconds = max(0.1, (self._args.ring_tone_ms + self._args.ring_silence_ms) / 1000)

        while not self._stop_event.is_set():
            try:
                self._playback.write(tone + silence, sample_rate=sample_rate)
            except Exception as exc:
                print(f"Ring tone failed: {exc}", file=sys.stderr)
                time.sleep(1)
            self._stop_event.wait(cycle_seconds)


class LinuxInputButtonWatcher:
    def __init__(self, device: str, key_code: int, debounce_ms: int, callback):
        self._device = device
        self._key_code = key_code
        self._debounce_seconds = max(0.05, debounce_ms / 1000)
        self._callback = callback
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="linux-input-button", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1)

    def _run(self) -> None:
        event_format = "llHHI"
        event_size = struct.calcsize(event_format)
        last_press_at = 0.0
        last_error_at = 0.0

        while not self._stop_event.is_set():
            try:
                with open(self._device, "rb", buffering=0) as input_device:
                    print(f"Watching handset button on {self._device}", file=sys.stderr)
                    last_error_at = 0.0
                    while not self._stop_event.is_set():
                        event = input_device.read(event_size)
                        if len(event) != event_size:
                            continue

                        _, _, event_type, code, value = struct.unpack(event_format, event)
                        if event_type != 1 or value != 1:
                            continue
                        if self._key_code >= 0 and code != self._key_code:
                            continue

                        now = time.monotonic()
                        if now - last_press_at < self._debounce_seconds:
                            continue

                        last_press_at = now
                        self._callback()
            except OSError as exc:
                now = time.monotonic()
                if now - last_error_at >= 60:
                    print(f"Button device read failed: {exc}", file=sys.stderr)
                    last_error_at = now
                self._stop_event.wait(2)


class GpioButtonWatcher:
    def __init__(self, pin: int, debounce_ms: int, callback):
        self._pin = pin
        self._debounce_seconds = max(0.05, debounce_ms / 1000)
        self._callback = callback
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gpio-button", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1)

    def _run(self) -> None:
        try:
            import RPi.GPIO as GPIO
        except ImportError:
            print("RPi.GPIO is not installed; GPIO button is disabled.", file=sys.stderr)
            return

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        last_value = GPIO.input(self._pin)
        last_press_at = 0.0
        print(f"Watching handset button on BCM GPIO {self._pin}", file=sys.stderr)

        try:
            while not self._stop_event.wait(0.02):
                value = GPIO.input(self._pin)
                if last_value == 1 and value == 0:
                    now = time.monotonic()
                    if now - last_press_at >= self._debounce_seconds:
                        last_press_at = now
                        self._callback()
                last_value = value
        finally:
            GPIO.cleanup(self._pin)


class Mp3Repeater:
    def __init__(self, mp3_path: str, interval_seconds: float, output_device: str | None):
        self._player = Mp3OutputWorker(output_device, "GPIO alert")
        self._mp3_path = mp3_path
        self._interval_seconds = max(1.0, float(interval_seconds))

    def start(self) -> None:
        self._player.play_file(self._mp3_path, repeat_interval=self._interval_seconds)

    def stop(self) -> None:
        self._player.stop()

    def shutdown(self) -> None:
        self._player.shutdown()


class Mp3OneShotPlayer:
    def __init__(self, mp3_path: str, output_device: str | None, label: str):
        self._mp3_path = mp3_path
        self._player = Mp3OutputWorker(output_device, label)

    def play(self) -> None:
        self._player.play_file(self._mp3_path)

    def stop(self) -> None:
        self._player.stop()

    def shutdown(self) -> None:
        self._player.shutdown()


class Mp3OutputWorker:
    def __init__(self, output_device: str | None, label: str):
        self._output_device = output_device
        self._label = label
        self._lock = threading.Lock()
        self._command_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._process: subprocess.Popen | None = None
        self._generation = 0
        self._command: tuple[str, object, float | None, int] | None = None
        self._thread = threading.Thread(target=self._run, name=f"{label}-mp3-worker", daemon=True)
        self._thread.start()

    def play_file(self, mp3_path: str, repeat_interval: float | None = None) -> None:
        with self._lock:
            self._generation += 1
            self._command = ("file", mp3_path, repeat_interval, self._generation)
        self._stop_process()
        self._command_event.set()

    def play_bytes(self, mp3_data: bytes) -> None:
        with self._lock:
            self._generation += 1
            self._command = ("bytes", mp3_data, None, self._generation)
        self._stop_process()
        self._command_event.set()

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._command = None
        self._stop_process()
        self._command_event.set()

    def shutdown(self) -> None:
        self.stop()
        self._shutdown_event.set()
        self._command_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._shutdown_event.is_set():
            self._command_event.wait()
            self._command_event.clear()
            if self._shutdown_event.is_set():
                return

            with self._lock:
                command = self._command

            if command is None:
                continue

            command_type, payload, repeat_interval, generation = command
            if command_type == "bytes":
                self._play_bytes_once(payload, generation)
                continue

            if repeat_interval is None:
                self._play_file_once(payload, generation)
                continue

            while not self._shutdown_event.is_set():
                if not self._is_generation_current(generation):
                    break
                self._play_file_once(payload, generation)
                if self._command_event.wait(repeat_interval):
                    self._command_event.clear()
                    break

    def _play_file_once(self, mp3_path: object, generation: int) -> None:
        process = None
        try:
            process = subprocess.Popen(
                build_mpg123_command(str(mp3_path), self._output_device),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if not self._register_process(process, generation):
                return
            process.wait()
            if process.returncode not in {0, None, -15}:
                print(f"{self._label} MP3 playback failed: {process.returncode}", file=sys.stderr)
        except Exception as exc:
            print(f"{self._label} MP3 playback failed: {exc}", file=sys.stderr)
        finally:
            self._clear_process(process)

    def _play_bytes_once(self, mp3_data: object, generation: int) -> None:
        process = None
        try:
            process = subprocess.Popen(
                build_mpg123_command("-", self._output_device),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if not self._register_process(process, generation):
                return
            if process.stdin is None:
                raise RuntimeError("Could not open mpg123 stdin.")
            process.stdin.write(bytes(mp3_data))
            process.stdin.close()
            process.wait()
            if process.returncode not in {0, None, -15}:
                print(f"{self._label} MP3 playback failed: {process.returncode}", file=sys.stderr)
        except Exception as exc:
            print(f"{self._label} MP3 playback failed: {exc}", file=sys.stderr)
        finally:
            self._clear_process(process)

    def _register_process(self, process: subprocess.Popen, generation: int) -> bool:
        with self._lock:
            if generation != self._generation:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                return False
            self._process = process
            return True

    def _clear_process(self, process: subprocess.Popen | None) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def _stop_process(self) -> None:
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def _is_generation_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation


class GpioAlertWatcher:
    def __init__(
        self,
        pin: int,
        led_pin: int,
        debounce_ms: int,
        player: Mp3Repeater,
        callback,
    ):
        self._pin = pin
        self._led_pin = led_pin
        self._debounce_seconds = max(0.05, debounce_ms / 1000)
        self._player = player
        self._callback = callback
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._gpio_lock = threading.Lock()
        self._GPIO = None
        self._alert_active = False
        self._suppressed_until_high = False
        self._last_low_at = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gpio-alert-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.clear()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1)

    def clear(self) -> None:
        with self._lock:
            self._alert_active = False
            self._suppressed_until_high = True
        self._set_led(False)
        self._player.stop()

    def is_active(self) -> bool:
        with self._lock:
            return self._alert_active

    def _trigger(self) -> None:
        with self._lock:
            if self._alert_active or self._suppressed_until_high:
                return

            self._alert_active = True

        print(f"[SOS] 버튼 감지: GPIO {self._pin} LOW", file=sys.stderr)
        self._set_led(True)
        self._player.start()
        self._callback()

    def _set_led(self, enabled: bool) -> None:
        with self._gpio_lock:
            GPIO = self._GPIO
            if GPIO is None:
                return

            GPIO.output(self._led_pin, GPIO.HIGH if enabled else GPIO.LOW)

    def _run(self) -> None:
        try:
            import RPi.GPIO as GPIO
        except ImportError:
            print("RPi.GPIO is not installed; GPIO alert listener is disabled.", file=sys.stderr)
            return

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self._led_pin, GPIO.OUT, initial=GPIO.LOW)
        with self._gpio_lock:
            self._GPIO = GPIO
        last_value = GPIO.input(self._pin)
        print(f"Watching GPIO alert on BCM GPIO {self._pin}; LED on BCM GPIO {self._led_pin}", file=sys.stderr)

        try:
            if last_value == 0:
                self._trigger()

            while not self._stop_event.wait(0.05):
                value = GPIO.input(self._pin)
                if value == 1:
                    if last_value == 0:
                        self.clear()
                        print(f"[SOS] 버튼 해제: GPIO {self._pin} HIGH, 알람음 정지", file=sys.stderr)
                    with self._lock:
                        self._suppressed_until_high = False

                if last_value == 1 and value == 0:
                    now = time.monotonic()
                    if now - self._last_low_at >= self._debounce_seconds:
                        self._last_low_at = now
                        self._trigger()

                last_value = value
        finally:
            self._set_led(False)
            with self._gpio_lock:
                self._GPIO = None
            GPIO.cleanup((self._pin, self._led_pin))


class GpsLocationProvider:
    def __init__(
        self,
        *,
        port: str,
        baudrate: int,
        interval_seconds: float,
        read_timeout_seconds: float,
        cache_file: str,
        disabled: bool = False,
    ):
        self._port = port
        self._baudrate = baudrate
        self._interval_seconds = max(10.0, float(interval_seconds))
        self._read_timeout_seconds = max(1.0, float(read_timeout_seconds))
        self._cache_file = cache_file
        self._disabled = disabled
        self._lock = threading.Lock()
        self._last_read_at = 0.0
        self._reading = False
        self._receive_status = 'GPS 응답 확인 중'
        self._monitor_stop = threading.Event()
        self._location = self._load_cache()

    def get_location(self, *, force: bool = False) -> dict | None:
        with self._lock:
            if self._disabled or self._reading:
                return dict(self._location) if self._location else None

            now = time.monotonic()
            if not force and now - self._last_read_at < self._interval_seconds:
                return dict(self._location) if self._location else None

            self._last_read_at = now
            self._reading = True

        try:
            location = self._read_once()
            if location:
                with self._lock:
                    self._location = location
                self._save_cache(location)
            with self._lock:
                return dict(self._location) if self._location else None
        finally:
            with self._lock:
                self._reading = False

    def format_status(self) -> str:
        with self._lock:
            location = dict(self._location) if self._location else None
            receive_status = self._receive_status
        if self._disabled:
            return '[GPS] GPS 수신 비활성화'
        if not location:
            return f'[GPS] {receive_status}'
        read_at = location.get('read_at')
        try:
            age = max(0, time.time() - float(read_at))
            received = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(read_at)))
        except (TypeError, ValueError, OverflowError, OSError):
            age, received = float('inf'), '알 수 없음'
        status = '최근 수신 좌표' if age <= 20 else '이전 좌표 (새 GPS 수신 대기)'
        return (f"[GPS] {receive_status} | {status}: 위도={location['latitude']:.6f}, "
                f"경도={location['longitude']:.6f} | 마지막 수신={received}")

    def start_monitor(self) -> None:
        def read_loop():
            while not self._monitor_stop.is_set():
                started = time.monotonic()
                self.get_location()
                self._monitor_stop.wait(max(0.1, self._interval_seconds - (time.monotonic() - started)))

        def log_loop():
            while not self._monitor_stop.is_set():
                print(self.format_status(), file=sys.stderr, flush=True)
                self._monitor_stop.wait(10.0)

        threading.Thread(target=read_loop, name='gps-reader', daemon=True).start()
        threading.Thread(target=log_loop, name='gps-log', daemon=True).start()

    def stop_monitor(self) -> None:
        self._monitor_stop.set()

    def _set_receive_status(self, message: str) -> None:
        with self._lock:
            self._receive_status = message

    @staticmethod
    def _valid_nmea(line: str) -> bool:
        if not line.startswith('$') or ',' not in line or '*' not in line:
            return False
        body, checksum = line[1:].rsplit('*', 1)
        if len(body.split(',', 1)[0]) != 5 or len(checksum) != 2:
            return False
        calculated = 0
        for char in body:
            calculated ^= ord(char)
        try:
            return calculated == int(checksum, 16)
        except ValueError:
            return False

    def _read_once(self) -> dict | None:
        try:
            import serial
        except ImportError:
            self._set_receive_status('GPS 수신 오류 (pyserial 미설치)')
            print("pyserial is not installed; using last cached GPS location if available.", file=sys.stderr)
            return None

        deadline = time.monotonic() + self._read_timeout_seconds
        byte_count = 0
        nmea_count = 0
        last_nmea = ''
        try:
            with serial.Serial(self._port, self._baudrate, timeout=1) as gps_serial:
                while time.monotonic() < deadline:
                    raw = gps_serial.readline()
                    byte_count += len(raw)
                    line = raw.decode('ascii', errors='replace').strip()
                    if not self._valid_nmea(line):
                        continue
                    nmea_count += 1
                    last_nmea = line[:160]
                    latitude, longitude = parse_nmea_location(line)
                    if latitude is None or longitude is None:
                        continue

                    self._set_receive_status(
                        f'GPS 수신 정상 ({byte_count}바이트, NMEA {nmea_count}건) | 수신 로그={last_nmea}')
                    location = {
                        "latitude": round(float(latitude), 7),
                        "longitude": round(float(longitude), 7),
                        "source": "gps",
                        "read_at": int(time.time()),
                    }
                    print(
                        f"GPS location read: {location['latitude']:.6f}, {location['longitude']:.6f}",
                        file=sys.stderr,
                    )
                    return location
        except Exception as exc:
            if not os.path.exists(self._port):
                self._set_receive_status(f'GPS 연결안됨 (포트 없음: {self._port})')
            else:
                self._set_receive_status(f'GPS 수신 오류 ({exc})')
            print(f"GPS read failed: {exc}", file=sys.stderr)
            return None

        if byte_count == 0:
            self._set_receive_status(
                f'GPS 연결안됨 (응답 없음: {self._port}, {self._read_timeout_seconds:g}초, 0바이트)')
        elif nmea_count:
            self._set_receive_status(
                f'GPS 수신 정상 / 위치 확정 대기 ({byte_count}바이트, NMEA {nmea_count}건) | 수신 로그={last_nmea}')
        else:
            self._set_receive_status(
                f'GPS 데이터 수신 / 정상 NMEA 없음 ({byte_count}바이트, 형식·체크섬·통신속도 확인 필요)')
        return None

    def _load_cache(self) -> dict | None:
        try:
            with open(self._cache_file, "r", encoding="utf-8") as cache:
                payload = json.load(cache)
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
            return {
                "latitude": latitude,
                "longitude": longitude,
                "source": str(payload.get("source") or "cache"),
                "read_at": payload.get("read_at"),
            }
        except Exception:
            return None

    def _save_cache(self, location: dict) -> None:
        try:
            with open(self._cache_file, "w", encoding="utf-8") as cache:
                json.dump(location, cache)
        except OSError as exc:
            print(f"Could not save GPS cache: {exc}", file=sys.stderr)


class MicrophoneStreamer:
    def __init__(self, args: argparse.Namespace, location_provider: GpsLocationProvider):
        self._args = args
        self._location_provider = location_provider
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="handset-microphone-streamer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            result = self._stream_once()
            if result == 0 or self._stop_event.is_set():
                return
            self._stop_event.wait(self._args.reconnect_seconds)

    def _stream_once(self) -> int:
        process = None
        ws = None
        chunk_bytes = self._args.chunk_samples * 2
        metadata = {
            "type": "start",
            "sample_rate": self._args.handset_sample_rate,
            "channels": 1,
            "sample_width": 2,
            **build_device_payload(self._args, self._location_provider.get_location()),
        }

        try:
            process = subprocess.Popen(
                build_arecord_command(self._args.handset_sample_rate, self._args.handset_input_device),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if process.stdout is None:
                raise RuntimeError("Could not open arecord stdout.")

            ws_url = build_live_send_ws_url(self._args.server, self._args.token)
            ws = websocket.create_connection(ws_url, timeout=10)
            ws.send(json.dumps(metadata))
            first_reply = ws.recv()
            if isinstance(first_reply, str):
                payload = json.loads(first_reply)
                if payload.get("type") == "error":
                    raise RuntimeError(payload.get("message", "Server rejected the stream."))

            print(f"Streaming handset microphone to {ws_url}", file=sys.stderr)

            while not self._stop_event.is_set():
                chunk = process.stdout.read(chunk_bytes)
                if not chunk:
                    break
                ws.send_binary(chunk)

            return 0
        except Exception as exc:
            if not self._stop_event.is_set():
                print(f"Handset microphone streaming failed: {exc}", file=sys.stderr)
            return 1
        finally:
            if ws is not None:
                try:
                    ws.send(json.dumps({"type": "stop"}))
                except Exception:
                    pass
                try:
                    ws.close()
                except Exception:
                    pass

            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass

class TtsMp3Player:
    def __init__(self, output_device: str | None):
        self._player = Mp3OutputWorker(output_device, "TTS")

    def play(self, mp3_base64: str) -> None:
        mp3_data = base64.b64decode(str(mp3_base64))
        self._player.play_bytes(mp3_data)

    def stop(self) -> None:
        self._player.stop()

    def shutdown(self) -> None:
        self._player.shutdown()


class HeartbeatWorker:
    def __init__(self, server: str, token: str, interval_seconds: int, payload_factory):
        self._server = server
        self._token = token
        self._interval_seconds = max(1, int(interval_seconds))
        self._payload_factory = payload_factory
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        notice = ConnectionNotice()
        while not self._stop_event.wait(self._interval_seconds):
            try:
                post_json(self._server, "/api/pi/heartbeat", self._token, self._payload_factory())
                notice.connected()
            except (HTTPError, URLError, OSError, ValueError) as exc:
                notice.failed("Heartbeat", exc)


def play_tts_audio(
    args: argparse.Namespace,
    aux_playback: Playback,
    tts_mp3_player: TtsMp3Player,
    payload: dict,
) -> None:
    mp3_base64 = payload.get("mp3_base64")
    if mp3_base64:
        aux_playback.stop(force=True)
        tts_mp3_player.play(str(mp3_base64))
        return

    pcm_base64 = payload.get("pcm_base64")
    if not pcm_base64:
        return

    pcm = base64.b64decode(str(pcm_base64))
    sample_rate = int(payload.get("sample_rate", 16000))
    aux_playback.write(pcm, sample_rate=sample_rate)
    aux_playback.stop()


def handle_server_message(
    args: argparse.Namespace,
    handset_playback: Playback,
    aux_playback: Playback,
    tts_mp3_player: TtsMp3Player,
    siren_player: Mp3OneShotPlayer,
    ring_player: RingTonePlayer,
    mic_streamer: MicrophoneStreamer,
    gpio_alert_watcher: GpioAlertWatcher | None,
    state: dict,
    raw_message: str,
    send_response,
) -> None:
    payload = json.loads(raw_message)
    message_type = payload.get("type")

    if message_type in {"connected", "heartbeat", "pong"}:
        return

    if message_type == "call_ringing":
        state["call_state"] = "ringing"
        state["stream_format"] = None
        mic_streamer.stop()
        ring_player.start()
        print(f"Incoming call is ringing for {args.device_id}", file=sys.stderr)
        return

    if message_type == "call_accept":
        ring_player.stop()
        state["call_state"] = "active"
        state["stream_format"] = None
        mic_streamer.start()
        print(f"Call accepted for {args.device_id}", file=sys.stderr)
        return

    if message_type == "call_end":
        ring_player.stop()
        mic_streamer.stop()
        handset_playback.stop()
        state["call_state"] = "idle"
        state["stream_format"] = None
        print(f"Call ended for {args.device_id}: {payload.get('reason', 'ended')}", file=sys.stderr)
        return

    if message_type == "format":
        sample_rate = int(payload.get("sample_rate", args.handset_sample_rate))
        state["stream_format"] = payload
        handset_playback.ensure_started(sample_rate)
        print(f"Prepared handset speaker at {sample_rate} Hz for {args.device_id}", file=sys.stderr)
        return

    if message_type == "tts_audio":
        send_response({
            "type": "tts_response",
            "device_id": args.device_id,
            "response": "success",
        })
        print(f"[TTS] 수신 확인 응답 전송: success ({args.device_id})", file=sys.stderr)
        play_tts_audio(args, aux_playback, tts_mp3_player, payload)
        return

    if message_type == "tts_stop":
        tts_mp3_player.stop()
        aux_playback.stop()
        print(f"TTS stopped for {args.device_id}", file=sys.stderr)
        return

    if message_type == "siren_play":
        siren_player.play()
        print(f"Server siren MP3 triggered for {args.device_id}", file=sys.stderr)
        return

    if message_type == "siren_off":
        siren_player.stop()
        print(f"Server siren stopped for {args.device_id}", file=sys.stderr)
        return

    if message_type == "alert_clear":
        if gpio_alert_watcher is not None:
            gpio_alert_watcher.clear()
        print(f"GPIO alert cleared for {args.device_id}", file=sys.stderr)
        return

    if message_type == "beep":
        sample_rate = int(payload.get("sample_rate", args.handset_sample_rate))
        pcm = generate_tone_pcm(
            sample_rate,
            int(payload.get("frequency_hz", 880)),
            int(payload.get("duration_ms", 250)),
            float(payload.get("volume", 0.35)),
        )
        handset_playback.write(pcm, sample_rate=sample_rate)
        print(f"Played beep for {args.device_id}", file=sys.stderr)
        return

    if message_type == "stop":
        state["stream_format"] = None
        handset_playback.stop()
        print(f"Stopped handset playback for {args.device_id}", file=sys.stderr)
        return

    if message_type == "error":
        raise RuntimeError(payload.get("message", "Server error."))


def validate_runtime(args: argparse.Namespace) -> int:
    if args.heartbeat_seconds <= 0:
        print("--heartbeat-seconds must be a positive integer.", file=sys.stderr)
        return 1

    if args.reconnect_seconds <= 0:
        print("--reconnect-seconds must be a positive integer.", file=sys.stderr)
        return 1

    if args.handset_sample_rate <= 0:
        print("--handset-sample-rate must be a positive integer.", file=sys.stderr)
        return 1

    if args.chunk_samples <= 0:
        print("--chunk-samples must be a positive integer.", file=sys.stderr)
        return 1

    if shutil.which("aplay") is None:
        print("aplay not found. Install it with: sudo apt install -y alsa-utils", file=sys.stderr)
        return 1

    if shutil.which("arecord") is None:
        print("arecord not found. Install it with: sudo apt install -y alsa-utils", file=sys.stderr)
        return 1

    if shutil.which("mpg123") is None:
        print("mpg123 not found. Install it with: sudo apt install -y mpg123", file=sys.stderr)
        return 1

    if not args.button_device and args.button_gpio is None and args.hook_gpio is None:
        print("No button source configured. Use --button-device or --button-gpio.", file=sys.stderr)

    if not args.gpio_alert_disabled and args.gpio_alert_interval_seconds <= 0:
        print("--gpio-alert-interval-seconds must be a positive number.", file=sys.stderr)
        return 1

    if not args.gps_disabled and args.gps_interval_seconds <= 0:
        print("--gps-interval-seconds must be a positive number.", file=sys.stderr)
        return 1

    if not args.gps_disabled and args.gps_read_timeout_seconds <= 0:
        print("--gps-read-timeout-seconds must be a positive number.", file=sys.stderr)
        return 1

    if not args.aux_device:
        print("Warning: --aux-device is not set. TTS will play on the default ALSA output.", file=sys.stderr)

    return 0


def main(args: argparse.Namespace) -> int:
    validation_result = validate_runtime(args)
    if validation_result != 0:
        return validation_result

    location_provider = GpsLocationProvider(
        port=args.gps_port,
        baudrate=args.gps_baudrate,
        interval_seconds=args.gps_interval_seconds,
        read_timeout_seconds=args.gps_read_timeout_seconds,
        cache_file=args.gps_cache_file,
        disabled=args.gps_disabled,
    )

    def current_payload(*, force_gps: bool = False) -> dict:
        return build_device_payload(args, location_provider.get_location(force=force_gps))

    payload = current_payload(force_gps=True)
    location_provider.start_monitor()

    registration_notice = ConnectionNotice()
    while True:
        try:
            response = post_json(args.server, "/api/pi/register", args.token, payload)
            print(f"Registered {args.device_id}: {response.get('status', 'ok')}", file=sys.stderr)
            break
        except (HTTPError, URLError, OSError, ValueError) as exc:
            registration_notice.failed("Registration", exc)
            if is_dns_error(exc):
                time.sleep(max(1, args.reconnect_seconds))
                continue
            location_provider.stop_monitor()
            return 1

    ws_url = build_pi_listen_ws_url(args.server, args.token, args)
    handset_playback = Playback(args.handset_speaker_device, args.handset_sample_rate, "handset")
    aux_playback = Playback(args.aux_device, args.handset_sample_rate, "aux")
    tts_mp3_player = TtsMp3Player(args.aux_device)
    ring_player = RingTonePlayer(args, handset_playback)
    mic_streamer = MicrophoneStreamer(args, location_provider)
    gpio_alert_player = Mp3Repeater(
        args.gpio_alert_file,
        args.gpio_alert_interval_seconds,
        args.gpio_alert_output_device,
    )
    siren_player = Mp3OneShotPlayer(
        args.siren_file,
        args.siren_output_device or args.gpio_alert_output_device or args.aux_device,
        "server siren",
    )
    gpio_alert_watcher: GpioAlertWatcher | None = None
    state = {"stream_format": None, "call_state": "idle"}
    ws_lock = threading.Lock()
    ws_holder: dict[str, websocket.WebSocket | None] = {"ws": None}
    heartbeat_worker = HeartbeatWorker(args.server, args.token, args.heartbeat_seconds, current_payload)

    def send_response(payload: dict) -> None:
        ws = ws_holder.get("ws")
        if ws is None:
            raise RuntimeError("Cannot send response: server WebSocket is not connected.")
        with ws_lock:
            ws.send(json.dumps(payload))

    def send_button_press() -> None:
        print(f"[통화] 버튼 눌림: {args.device_id}", file=sys.stderr)
        if state.get("call_state") == "ringing":
            print(f"Ignored duplicate button press while ringing for {args.device_id}", file=sys.stderr)
            return

        message = json.dumps({"type": "button", **current_payload()})
        ws = ws_holder.get("ws")
        if ws is None:
            print("Button pressed, but server WebSocket is not connected.", file=sys.stderr)
            return

        try:
            with ws_lock:
                ws.send(message)
            print(f"[통화] 버튼 이벤트 서버 전송 완료: {args.device_id}", file=sys.stderr)
        except Exception as exc:
            print(f"Could not send button press: {exc}", file=sys.stderr)

    def silence_handset():
        ring_player.stop()
        mic_streamer.stop()
        handset_playback.stop()
        state['call_state'] = 'idle'
        state['stream_format'] = None

    def hook_dial():
        ws = ws_holder.get('ws')
        if ws is None:
            return False
        if state.get('call_state') in {'ringing', 'active'}:
            return True
        with ws_lock:
            ws.send(json.dumps({'type': 'button', **current_payload()}))
        print('[통화] 수화기 들림: 발신 요청 전송', file=sys.stderr)
        return True

    def hook_hangup():
        post_json(args.server, '/api/calls/' + quote(args.device_id, safe='') + '/end',
                  args.token, {'reason': 'handset_on_hook'})
        print('[통화] 수화기 내려놓음: 종료 요청 완료', file=sys.stderr)
        return True

    hook_controller = HookController(hook_dial, hook_hangup, silence_handset) if args.hook_gpio is not None else None

    def send_gpio_alert() -> None:
        print(f"[SOS] 서버 알림 전송 시도: {args.device_id}", file=sys.stderr)
        message = json.dumps(
            {
                "type": "gpio_alert",
                "alert_code": "0001",
                "message": "GPIO 23 LOW",
                **current_payload(),
            }
        )
        ws = ws_holder.get("ws")
        if ws is None:
            print("GPIO alert active, but server WebSocket is not connected.", file=sys.stderr)
            return

        try:
            with ws_lock:
                ws.send(message)
        except Exception as exc:
            print(f"Could not send GPIO alert: {exc}", file=sys.stderr)
        else:
            print(f"[SOS] 서버 알림 전송 완료: {args.device_id}", file=sys.stderr)

    button_watchers = []
    if hook_controller is not None:
        button_watchers.append(GpioHookWatcher(args.hook_gpio, args.button_debounce_ms, hook_controller))
    if args.button_device:
        button_watchers.append(
            LinuxInputButtonWatcher(args.button_device, args.button_key_code, args.button_debounce_ms, send_button_press)
        )
    if args.button_gpio is not None:
        button_watchers.append(GpioButtonWatcher(args.button_gpio, args.button_debounce_ms, send_button_press))
    for watcher in button_watchers:
        watcher.start()
    heartbeat_worker.start()

    if not args.gpio_alert_disabled:
        gpio_alert_watcher = GpioAlertWatcher(
            args.gpio_alert_pin,
            args.gpio_alert_led_pin,
            args.button_debounce_ms,
            gpio_alert_player,
            send_gpio_alert,
        )
        gpio_alert_watcher.start()

    downlink_notice = ConnectionNotice()
    try:
        while True:
            ws = None
            try:
                ws = websocket.create_connection(ws_url, timeout=10)
                ws.settimeout(max(5.0, float(args.heartbeat_seconds)))
                ws_holder["ws"] = ws
                downlink_notice.connected()
                print(f"Listening for server control/audio on {ws_url}", file=sys.stderr)
                if gpio_alert_watcher is not None and gpio_alert_watcher.is_active():
                    send_gpio_alert()

                while True:
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue

                    if message is None:
                        raise RuntimeError("Server closed the downlink connection.")

                    if isinstance(message, bytes):
                        if hook_controller is not None and hook_controller.off_hook is False:
                            continue
                        stream_format = state.get("stream_format") or {}
                        if state.get("call_state") == "active" or stream_format:
                            handset_playback.write(
                                message,
                                sample_rate=int(stream_format.get("sample_rate", args.handset_sample_rate)),
                            )
                        continue

                    if isinstance(message, str):
                        handle_server_message(
                            args,
                            handset_playback,
                            aux_playback,
                            tts_mp3_player,
                            siren_player,
                            ring_player,
                            mic_streamer,
                            gpio_alert_watcher,
                            state,
                            message,
                            send_response,
                        )
                        if (hook_controller is not None and hook_controller.off_hook is False
                                and state.get('call_state') in {'ringing', 'active'}):
                            silence_handset()
                            with hook_controller.lock:
                                hook_controller.pending_end = hook_controller.revision

            except KeyboardInterrupt:
                print("Stopping Raspberry Pi agent.", file=sys.stderr)
                return 0
            except (HTTPError, URLError, OSError, ValueError, RuntimeError, websocket.WebSocketException) as exc:
                ring_player.stop()
                mic_streamer.stop()
                handset_playback.stop()
                downlink_notice.failed("Downlink", exc)
                time.sleep(args.reconnect_seconds)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                if ws_holder.get("ws") is ws:
                    ws_holder["ws"] = None
    finally:
        location_provider.stop_monitor()
        for watcher in button_watchers:
            watcher.stop()
        heartbeat_worker.stop()
        if gpio_alert_watcher is not None:
            gpio_alert_watcher.stop()
        gpio_alert_player.shutdown()
        siren_player.shutdown()
        ring_player.stop()
        mic_streamer.stop()
        tts_mp3_player.shutdown()
        handset_playback.stop()
        aux_playback.stop()


if __name__ == "__main__":
    parsed_args = parse_args()
    configure_stderr_log(parsed_args.log_file)
    sys.exit(main(parsed_args))
