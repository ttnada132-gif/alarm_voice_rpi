#!/usr/bin/env python3
"""Wi-Fi setup portal for the existing dhcpcd/wpa_supplicant installation."""
import csv
import hmac
import io
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import time

from flask import Flask, jsonify, render_template, request
from connection_config import validate_connection
from wifi_connect import load_wifi_networks, server_is_reachable, log

BASE = Path(__file__).resolve().parent
CONFIG = BASE / 'wifi_portal.json'
STATUS = Path('/run/shinwhatech-wifi/status.json')
DHCP = Path('/etc/dhcpcd.conf')
BEGIN = '# BEGIN SHINWHATECH SETUP AP\n'
END = '# END SHINWHATECH SETUP AP\n'
SERVICE = 'shinwhatech-raspi-agent.service'


def command(args, timeout=15):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Never include arguments: Wi-Fi passwords may be present.
        raise RuntimeError(f'{args[0]} 실행 실패 (종료 코드 {result.returncode})')
    return result.stdout.strip()


def wpa(*args):
    value = command(['wpa_cli', '-i', 'wlan0', *args])
    if any(line.startswith('FAIL') for line in value.splitlines()):
        raise RuntimeError('Wi-Fi 설정 명령 실패')
    return value


def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def atomic_write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as out:
        os.chmod(temp, mode)
        if path.exists() and os.geteuid() == 0:
            owner = path.stat()
            os.chown(temp, owner.st_uid, owner.st_gid)
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)


class Manager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.busy = False
        self.dnsmasq = None
        self.ap_id = None
        self.state = {'mode': 'starting', 'message': '연결 상태 확인 중', 'network_ready': False,
                      'server_ready': False, 'ssid': '', 'ip': ''}

    def update(self, **values):
        with self.lock:
            self.state.update(values)
            self.state['updated_at'] = time.time()
            snapshot = dict(self.state)
        atomic_write(STATUS, json.dumps(snapshot, ensure_ascii=False), 0o644)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def note(self, message):
        log('[Wi-Fi 설정] ' + message, str(BASE / 'log.txt'))
        self.update(message=message)

    def station(self):
        status = dict(line.split('=', 1) for line in wpa('status').splitlines() if '=' in line)
        ips = json.loads(command(['ip', '-j', '-4', 'address', 'show', 'dev', 'wlan0']))
        addresses = [a['local'] for dev in ips for a in dev.get('addr_info', [])
                     if a.get('scope') == 'global' and not a['local'].startswith('169.254.')
                     and a['local'] != self.cfg['ap_ip']]
        ready = status.get('wpa_state') == 'COMPLETED' and status.get('mode') == 'station' and bool(addresses)
        return ready, status.get('ssid', ''), addresses[0] if addresses else ''

    def static_ap_address(self, enabled):
        text = DHCP.read_text()
        if BEGIN in text:
            start = text.index(BEGIN)
            finish = text.index(END, start) + len(END)
            text = text[:start] + text[finish:]
        if enabled:
            text += f'\n{BEGIN}interface wlan0\nstatic ip_address={self.cfg["ap_ip"]}/24\nstatic routers=\nstatic domain_name_servers=\n{END}'
        atomic_write(DHCP, text, 0o644)
        command(['dhcpcd', '-n', 'wlan0'])

    def create_network(self, ssid, password, ap=False):
        network_id = wpa('add_network').splitlines()[-1]
        if not network_id.isdigit():
            raise RuntimeError('Wi-Fi 프로필 생성 실패')
        try:
            wpa('set_network', network_id, 'ssid', ssid.encode().hex())
            wpa('set_network', network_id, 'key_mgmt', 'WPA-PSK' if password else 'NONE')
            if password:
                psk = password if len(password) == 64 else quoted(password)
                wpa('set_network', network_id, 'psk', psk)
            if ap:
                wpa('set_network', network_id, 'mode', '2')
                wpa('set_network', network_id, 'frequency', '2437')
                wpa('set_network', network_id, 'id_str', '"shinwhatech-setup"')
            return network_id
        except Exception:
            wpa('remove_network', network_id)
            raise

    def stop_ap(self):
        if self.dnsmasq is not None:
            self.dnsmasq.terminate()
            try:
                self.dnsmasq.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.dnsmasq.kill()
                self.dnsmasq.wait()
            self.dnsmasq = None
        if self.ap_id is not None:
            wpa('remove_network', self.ap_id)
            self.ap_id = None
        self.static_ap_address(False)

    def start_ap(self):
        self.update(mode='switching', network_ready=False, server_ready=False)
        self.note('설정용 Wi-Fi 시작 중')
        command(['systemctl', 'stop', SERVICE], timeout=30)
        self.stop_ap()
        self.ap_id = self.create_network(self.cfg['ap_ssid'], '', ap=True)
        self.static_ap_address(True)
        wpa('select_network', self.ap_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if 'mode=AP' in wpa('status') and self.cfg['ap_ip'] in command(['ip', '-4', 'addr', 'show', 'wlan0']):
                break
            time.sleep(1)
        else:
            raise RuntimeError('설정용 Wi-Fi 시작 확인 실패')
        ip = self.cfg['ap_ip']
        prefix = ip.rsplit('.', 1)[0]
        self.dnsmasq = subprocess.Popen([
            '/usr/sbin/dnsmasq', '--keep-in-foreground', '--conf-file=/dev/null',
            '--interface=wlan0', '--bind-dynamic', '--port=0',
            f'--dhcp-range={prefix}.20,{prefix}.100,255.255.255.0,1h',
            f'--dhcp-option=3,{ip}', '--dhcp-option=6',
            '--dhcp-leasefile=/run/shinwhatech-wifi/dnsmasq.leases',
            '--pid-file=/run/shinwhatech-wifi/dnsmasq.pid'],
            stdout=subprocess.DEVNULL, stderr=None)
        time.sleep(1)
        if self.dnsmasq.poll() is not None:
            raise RuntimeError('설정 Wi-Fi 주소 배포 시작 실패')
        self.update(mode='ap', ssid=self.cfg['ap_ssid'], ip=ip)
        self.note(f'설정 Wi-Fi: {self.cfg["ap_ssid"]} / http://{ip}/')

    def save_network(self, ssid, password):
        networks = load_wifi_networks(str(BASE / 'wifi_networks.csv'))
        networks = [{'ssid': ssid, 'password': password}] + [n for n in networks if n['ssid'] != ssid]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=['ssid', 'password'])
        writer.writeheader()
        writer.writerows(networks)
        atomic_write(BASE / 'wifi_networks.csv', out.getvalue())
        wpa('save_config')

    def try_station(self, ssid, password, persist=False):
        self.update(mode='connecting', network_ready=False, server_ready=False)
        self.note(f'Wi-Fi 접속 시험: {ssid}')
        network_id = self.create_network(ssid, password)
        ok = False
        try:
            command(['ip', '-4', 'address', 'flush', 'dev', 'wlan0', 'scope', 'global'])
            wpa('select_network', network_id)
            command(['dhcpcd', '-n', 'wlan0'])
            deadline = time.monotonic() + self.cfg['connect_timeout_seconds']
            while time.monotonic() < deadline:
                ready, current_ssid, ip = self.station()
                if ready and current_ssid == ssid:
                    if persist:
                        self.save_network(ssid, password)
                    ok = True
                    self.update(mode='station', network_ready=True, ssid=ssid, ip=ip)
                    self.note(f'Wi-Fi 연결 성공: {ssid} / {ip}')
                    command(['systemctl', 'start', SERVICE], timeout=30)
                    return True
                time.sleep(2)
            self.note(f'Wi-Fi 연결 실패: {ssid}')
            return False
        finally:
            if not ok:
                wpa('remove_network', network_id)

    def submit(self, ssid, password):
        with self.lock:
            if self.busy:
                return False
            self.busy = True
        self.jobs.put_nowait((ssid, password))
        return True

    def submit_connection(self, settings):
        with self.lock:
            if self.busy:
                return False
            updated = {**self.cfg, **settings}
            atomic_write(CONFIG, json.dumps(updated, ensure_ascii=False, indent=2) + '\n')
            self.cfg = updated
            self.busy = True
            self.jobs.put_nowait((None, None))
        return True

    def apply_connection(self):
        self.update(server_ready=False)
        # AP mode intentionally keeps the agent stopped until Wi-Fi connects.
        if self.ap_id is None:
            command(['systemctl', 'restart', SERVICE], timeout=30)
        self.note('서버 접속정보 적용 완료. 서버 연결을 확인합니다.')

    def run(self):
        failed_since = None
        # Recover only a stale AP address, not an AP started by this manager.
        if self.ap_id is None and BEGIN in DHCP.read_text():
            self.static_ap_address(False)
            wpa('reconfigure')
        while True:
            try:
                try:
                    ssid, password = self.jobs.get_nowait()
                except queue.Empty:
                    pass
                else:
                    try:
                        time.sleep(2)  # Allow the browser to receive the response.
                        if ssid is None:
                            self.apply_connection()
                        else:
                            command(['systemctl', 'stop', SERVICE], timeout=30)
                            self.stop_ap()
                            if not self.try_station(ssid, password, persist=True):
                                self.start_ap()
                    except Exception:
                        if ssid is None:
                            self.note('접속정보는 저장되었으나 서비스 적용에 실패했습니다. 다시 저장해 재시도하세요.')
                        else:
                            self.note('새 Wi-Fi 적용 실패. 설정용 Wi-Fi를 복원합니다.')
                            self.start_ap()
                    finally:
                        with self.lock:
                            self.busy = False
                    failed_since = None
                if self.ap_id is not None:
                    if self.dnsmasq is None or self.dnsmasq.poll() is not None:
                        self.start_ap()
                    self.update(mode='ap', network_ready=False, server_ready=False)
                    time.sleep(2)
                    continue
                ready, ssid, ip = self.station()
                if ready:
                    failed_since = None
                    reachable = server_is_reachable(self.cfg['server_test_url'], self.cfg['stream_token'])
                    message = 'Wi-Fi 및 방송 서버 연결 정상' if reachable else 'Wi-Fi 연결됨 · 방송 서버 응답 대기'
                    old = self.snapshot()
                    self.update(mode='station', network_ready=True, server_ready=reachable, ssid=ssid, ip=ip)
                    if old.get('message') != message:
                        self.note(message)
                else:
                    self.update(network_ready=False, server_ready=False, mode='waiting')
                    if failed_since is None:
                        failed_since = time.monotonic()
                        self.note('Wi-Fi 연결 대기 (60초 후 저장된 목록 재시도)')
                    if time.monotonic() - failed_since >= self.cfg['fail_seconds']:
                        with self.lock:
                            self.busy = True
                        try:
                            command(['systemctl', 'stop', SERVICE], timeout=30)
                            networks = load_wifi_networks(str(BASE / 'wifi_networks.csv'))
                            if not any(self.try_station(n['ssid'], n['password']) for n in networks):
                                self.start_ap()
                        finally:
                            with self.lock:
                                self.busy = False
                        failed_since = None
                time.sleep(5)
            except Exception as exc:
                self.update(network_ready=False, server_ready=False)
                self.note(f'연결 관리 오류: {type(exc).__name__}. 5초 후 재시도')
                time.sleep(5)


def create_app(manager):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 4096
    csrf = secrets.token_urlsafe(32)

    @app.before_request
    def protect_form_submission():
        if request.method == 'POST' and not hmac.compare_digest(request.form.get('csrf', ''), csrf):
            return '페이지를 새로고침한 뒤 다시 시도하세요.', 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.route('/', methods=['GET'])
    def index():
        return render_template('wifi_setup.html', state=manager.snapshot(), cfg=manager.cfg, csrf=csrf)

    @app.route('/status', methods=['GET'])
    def status():
        return jsonify(manager.snapshot())

    @app.route('/save', methods=['POST'])
    def save():
        ssid = request.form.get('ssid', '').strip()
        password = request.form.get('password', '')
        valid_psk = (not password or 8 <= len(password.encode()) <= 63 or
                     (len(password) == 64 and all(c in '0123456789abcdefABCDEF' for c in password)))
        if not 1 <= len(ssid.encode()) <= 32 or not valid_psk or any(ord(c) < 32 for c in ssid + password):
            return 'Wi-Fi 이름은 1~32바이트, 비밀번호는 8~63바이트로 입력하세요. 공개 Wi-Fi는 비워두세요.', 400
        if not manager.submit(ssid, password):
            return '이미 연결을 시험하고 있습니다. 잠시 후 다시 시도하세요.', 409
        return render_template('wifi_setup.html', state=manager.snapshot(), cfg=manager.cfg, csrf=csrf, saved=True)

    @app.route('/connection', methods=['POST'])
    def save_connection():
        try:
            settings = validate_connection(request.form)
        except ValueError as exc:
            return str(exc), 400
        try:
            accepted = manager.submit_connection(settings)
        except OSError:
            return '접속정보 저장에 실패했습니다. 다시 시도하세요.', 500
        if not accepted:
            return '이미 설정을 적용하고 있습니다. 잠시 후 다시 시도하세요.', 409
        return render_template('wifi_setup.html', state=manager.snapshot(), cfg=manager.cfg,
                               csrf=csrf, connection_saved=True)

    return app


if __name__ == '__main__':
    cfg = json.loads(CONFIG.read_text())
    manager = Manager(cfg)
    thread = threading.Thread(target=manager.run, daemon=True)
    thread.start()
    create_app(manager).run(host='0.0.0.0', port=80, threaded=True, use_reloader=False)
