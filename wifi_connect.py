from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Iterable
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Connect Raspberry Pi WiFi from a CSV list before starting the agent.")
    parser.add_argument("--csv", default="/home/pi/shinwhatech/wifi_networks.csv", help="CSV file with ssid,password columns")
    parser.add_argument("--server-test-url", required=True, help="Server URL to keep checking after network connectivity is ready")
    parser.add_argument("--token", default="", help="Shared stream token for the server connectivity check")
    parser.add_argument("--interface", default="wlan0", help="Wireless interface, usually wlan0")
    parser.add_argument("--retry-seconds", type=float, default=30.0, help="Seconds between WiFi attempts and server retry checks")
    parser.add_argument("--connect-timeout-seconds", type=float, default=25.0, help="Seconds to wait for general network connectivity after each WiFi attempt")
    parser.add_argument(
        "--network-test-hosts",
        default="8.8.8.8,1.1.1.1",
        help="Comma-separated hosts to ping when confirming general network connectivity",
    )
    parser.add_argument("--log-file", default="/home/pi/shinwhatech/log.txt", help="Log file path")
    parser.add_argument("--hook-gpio", type=int, help="Monitor the handset while waiting for connectivity")
    return parser.parse_args()


def log(message: str, log_file: str) -> None:
    timestamped = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(timestamped, file=sys.stderr, flush=True)
    try:
        with open(log_file, "a", encoding="utf-8") as output:
            output.write(timestamped + "\n")
    except OSError:
        pass


def load_wifi_networks(csv_path: str) -> list[dict[str, str]]:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"WiFi CSV file not found: {csv_path}")

    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        networks = [
            {
                "ssid": str(row.get("ssid") or "").strip(),
                "password": str(row.get("password") or "").strip(),
            }
            for row in reader
        ]

    return [network for network in networks if network["ssid"]]


def run_command(command: list[str], *, timeout: float) -> tuple[int, str]:
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "command timed out"

    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
    return completed.returncode, output


def parse_network_test_hosts(raw_hosts: str) -> list[str]:
    return [host.strip() for host in raw_hosts.split(",") if host.strip()]


def ping_host(host: str, timeout: float = 3.0) -> bool:
    ping = shutil.which("ping")
    if ping is None:
        return False

    wait_seconds = max(1, int(round(timeout)))
    return_code, _ = run_command([ping, "-c", "1", "-W", str(wait_seconds), host], timeout=wait_seconds + 2)
    return return_code == 0


def network_is_connected(test_hosts: Iterable[str], timeout: float = 3.0) -> bool:
    for host in test_hosts:
        if ping_host(host, timeout=timeout):
            return True

    return False


def server_is_reachable(url: str, token: str, timeout: float = 5.0) -> bool:
    headers = {"User-Agent": "shinwha-raspi-wifi-check/1.0"}
    if token:
        headers["X-Stream-Token"] = token

    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, URLError):
        return False


def wait_for_network_connection(test_hosts: list[str], timeout_seconds: float, log_file: str) -> bool:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if network_is_connected(test_hosts):
            log("General network connectivity check succeeded.", log_file)
            return True

        time.sleep(3)

    log("General network connectivity check failed after WiFi attempt.", log_file)
    return False


def connect_with_nmcli(network: dict[str, str], interface: str, log_file: str) -> bool:
    nmcli = shutil.which("nmcli")
    if nmcli is None:
        return False

    ssid = network["ssid"]
    password = network["password"]
    log(f"Trying WiFi with nmcli: {ssid}", log_file)
    run_command([nmcli, "radio", "wifi", "on"], timeout=10)
    run_command([nmcli, "device", "wifi", "rescan"], timeout=20)

    command = [nmcli, "--wait", "20", "device", "wifi", "connect", ssid]
    if password:
        command.extend(["password", password])
    if interface:
        command.extend(["ifname", interface])

    return_code, output = run_command(command, timeout=30)
    if return_code == 0:
        return True

    log(f"nmcli failed for {ssid}: {output or return_code}", log_file)
    return False


def quote_wpa_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def connect_with_wpa_cli(network: dict[str, str], interface: str, log_file: str) -> bool:
    wpa_cli = shutil.which("wpa_cli")
    if wpa_cli is None:
        return False

    ssid = network["ssid"]
    password = network["password"]
    log(f"Trying WiFi with wpa_cli: {ssid}", log_file)

    base_command = [wpa_cli, "-i", interface]
    run_command(base_command + ["scan"], timeout=10)
    return_code, network_id_output = run_command(base_command + ["add_network"], timeout=10)
    network_id = next(
        (line.strip() for line in reversed(network_id_output.splitlines()) if line.strip().isdigit()),
        "",
    )
    if return_code != 0 or not network_id:
        log(f"wpa_cli add_network failed for {ssid}: {network_id_output or return_code}", log_file)
        return False

    commands = [
        base_command + ["set_network", network_id, "ssid", quote_wpa_value(ssid)],
        base_command + ["set_network", network_id, "psk", quote_wpa_value(password)] if password else base_command + ["set_network", network_id, "key_mgmt", "NONE"],
        base_command + ["enable_network", network_id],
        base_command + ["select_network", network_id],
        base_command + ["save_config"],
        base_command + ["reconfigure"],
    ]

    for command in commands:
        return_code, output = run_command(command, timeout=10)
        if return_code != 0:
            log(f"wpa_cli command failed for {ssid}: {output or return_code}", log_file)
            return False

    return True


def wait_for_server_connection(url: str, token: str, retry_seconds: float, log_file: str) -> None:
    while True:
        if server_is_reachable(url, token):
            log("Server connectivity check succeeded.", log_file)
            return

        log("Server connectivity check failed; retrying without changing network.", log_file)
        time.sleep(max(1.0, retry_seconds))


class WaitingHookController:
    def __init__(self, log_file):
        self.log_file = log_file

    def changed(self, off_hook):
        state = '들림 (HIGH)' if off_hook else '내려놓음 (LOW)'
        log(f'[통화] 수화기 {state} · 네트워크/서버 연결 대기 중', self.log_file)

    def sync(self):
        pass


def main(args: argparse.Namespace) -> int:
    from handset_hook import GpioHookWatcher

    watcher = None
    try:
        if args.hook_gpio is not None:
            watcher = GpioHookWatcher(args.hook_gpio, 450, WaitingHookController(args.log_file))
            log(f'[통화] 서버 연결 대기 중 수화기 감시 시작: BCM GPIO{args.hook_gpio}', args.log_file)
            watcher.start()
        return wait_for_connectivity(args)
    finally:
        if watcher is not None:
            watcher.stop()


def wait_for_connectivity(args: argparse.Namespace) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(args.log_file)), exist_ok=True)
    if Path(__file__).with_name('wifi_portal.json').exists():
        # The portal owns Wi-Fi selection. Never compete with its setup AP.
        status_path = Path('/run/shinwhatech-wifi/status.json')
        previous = None
        while True:
            try:
                status = json.loads(status_path.read_text())
                fresh = time.time() - status.get('updated_at', 0) < 30
                if fresh and status.get('network_ready') and status.get('server_ready'):
                    log('Wi-Fi portal confirmed network and server connectivity.', args.log_file)
                    return 0
                message = status.get('message', '연결 확인 중') if fresh else 'Wi-Fi 설정 서비스 응답 대기'
            except (OSError, ValueError, TypeError):
                message = 'Wi-Fi 설정 서비스 시작 대기'
            if message != previous:
                log(message, args.log_file)
                previous = message
            time.sleep(3)
    test_hosts = parse_network_test_hosts(args.network_test_hosts)
    if not test_hosts:
        raise ValueError("At least one --network-test-hosts entry is required.")

    if network_is_connected(test_hosts):
        log("General network connectivity is already available; skipping WiFi changes.", args.log_file)
        wait_for_server_connection(args.server_test_url, args.token, args.retry_seconds, args.log_file)
        return 0

    networks = load_wifi_networks(args.csv)
    if not networks:
        log(f"No WiFi networks found in {args.csv}.", args.log_file)
        return 1

    attempt_index = 0
    log(f"Waiting for general network connectivity using {args.csv}.", args.log_file)
    while True:
        if network_is_connected(test_hosts):
            log("General network connectivity check succeeded.", args.log_file)
            break

        network = networks[attempt_index % len(networks)]
        attempt_index += 1

        connected = connect_with_nmcli(network, args.interface, args.log_file)
        if not connected:
            connected = connect_with_wpa_cli(network, args.interface, args.log_file)

        if connected and wait_for_network_connection(test_hosts, args.connect_timeout_seconds, args.log_file):
            break

        log(f"Next WiFi attempt in {args.retry_seconds:.0f} seconds.", args.log_file)
        time.sleep(max(1.0, args.retry_seconds))

    wait_for_server_connection(args.server_test_url, args.token, args.retry_seconds, args.log_file)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(parse_args()))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        args = parse_args()
        log(f"WiFi setup failed: {exc}", args.log_file)
        sys.exit(1)
