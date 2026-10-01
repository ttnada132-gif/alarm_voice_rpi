from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import websocket


def default_device_id() -> str:
    return socket.gethostname() or "raspberry-pi"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send Raspberry Pi microphone audio to the Flask live audio server."
    )
    parser.add_argument("--server", required=True, help="Server URL such as http://192.168.0.10:5000")
    parser.add_argument("--token", required=True, help="Shared stream token configured on the Flask server")
    parser.add_argument("--device-id", default=default_device_id(), help="Stable Raspberry Pi ID")
    parser.add_argument("--device", help="Optional ALSA input device, for example plughw:1,0")
    parser.add_argument("--sample-rate", type=int, default=16000, help="PCM sample rate in Hz")
    parser.add_argument("--chunk-samples", type=int, default=1024, help="Samples per WebSocket frame")
    parser.add_argument("--source-name", help="Human-readable source name shown in the UI")
    parser.add_argument("--company-name", default="", help="Company name shown in the server UI")
    parser.add_argument("--install-location", default="", help="Install location shown in the server UI")
    parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=10,
        help="How often to POST a heartbeat while streaming",
    )
    args = parser.parse_args()
    if not args.source_name:
        args.source_name = args.device_id
    return args


def build_ws_url(server_url: str, token: str) -> str:
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


def build_arecord_command(sample_rate: int, device: str | None) -> list[str]:
    command = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(sample_rate)]
    if device:
        command.extend(["-D", device])
    return command


def build_device_payload(args: argparse.Namespace) -> dict:
    return {
        "device_id": args.device_id,
        "source_name": args.source_name,
        "company_name": args.company_name,
        "install_location": args.install_location,
    }


def register_device(args: argparse.Namespace) -> None:
    try:
        post_json(args.server, "/api/pi/register", args.token, build_device_payload(args))
    except (HTTPError, URLError, ValueError) as exc:
        print(f"Warning: registration POST failed: {exc}", file=sys.stderr)


def send_heartbeat(args: argparse.Namespace) -> None:
    try:
        post_json(args.server, "/api/pi/heartbeat", args.token, build_device_payload(args))
    except (HTTPError, URLError, ValueError) as exc:
        print(f"Warning: heartbeat POST failed: {exc}", file=sys.stderr)


def stream_microphone(args: argparse.Namespace) -> int:
    if shutil.which("arecord") is None:
        print("arecord not found. Install it with: sudo apt install -y alsa-utils", file=sys.stderr)
        return 1

    if args.chunk_samples <= 0:
        print("--chunk-samples must be a positive integer.", file=sys.stderr)
        return 1

    if args.heartbeat_seconds <= 0:
        print("--heartbeat-seconds must be a positive integer.", file=sys.stderr)
        return 1

    register_device(args)

    ws_url = build_ws_url(args.server, args.token)
    chunk_bytes = args.chunk_samples * 2
    metadata = {
        "type": "start",
        "sample_rate": args.sample_rate,
        "channels": 1,
        "sample_width": 2,
        **build_device_payload(args),
    }

    process = subprocess.Popen(
        build_arecord_command(args.sample_rate, args.device),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if process.stdout is None:
        print("Could not open arecord stdout.", file=sys.stderr)
        return 1

    ws = None
    next_heartbeat = time.monotonic() + args.heartbeat_seconds

    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        ws.send(json.dumps(metadata))

        first_reply = ws.recv()
        if isinstance(first_reply, str):
            payload = json.loads(first_reply)
            if payload.get("type") == "error":
                raise RuntimeError(payload.get("message", "Server rejected the stream."))

        print(f"Streaming microphone to {ws_url}", file=sys.stderr)

        while True:
            chunk = process.stdout.read(chunk_bytes)
            if not chunk:
                break

            ws.send_binary(chunk)

            if time.monotonic() >= next_heartbeat:
                send_heartbeat(args)
                next_heartbeat = time.monotonic() + args.heartbeat_seconds
    except KeyboardInterrupt:
        print("Stopping live audio sender.", file=sys.stderr)
    except Exception as exc:
        print(f"Streaming failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if ws is not None:
            try:
                ws.send(json.dumps({"type": "stop"}))
            except Exception:
                pass
            ws.close()

        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()

    return 0


if __name__ == "__main__":
    sys.exit(stream_microphone(parse_args()))
