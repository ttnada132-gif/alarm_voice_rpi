"""Boot-time updates from a pinned GitHub commit; requires the git executable."""
import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
import zipfile

REPOSITORY = 'ttnada132-gif/alarm_voice_rpi'
BRANCH = 'main'
# Device settings and mutable data must never be release payloads.
FILES = (
    'auto_update.py', 'run_raspi_agent.sh', 'auto_start.sh', 'show_logs.sh',
    'connection_config.py', 'find_handset.py', 'gps_latlon.py', 'handset_hook.py',
    'battery_sensor.py', 'raspi_agent.py', 'raspi_mic_sender.py', 'wifi_connect.py', 'wifi_portal.py',
    'templates/wifi_setup.html', '0001.mp3', 'VERSION',
)
MAX_DOWNLOAD = 20 * 1024 * 1024


def version(value):
    value = value.strip()
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError('버전은 MAJOR.MINOR.PATCH 형식이어야 합니다')
    return tuple(map(int, value.split('.')))


def log(root, message):
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' [업데이트] ' + message
    print(line, flush=True)
    try:
        with (root / 'log.txt').open('a') as stream:
            stream.write(line + '\n')
    except OSError:
        pass


def download(url, limit=MAX_DOWNLOAD):
    request = Request(url, headers={'User-Agent': 'alarm-voice-rpi-updater',
                                   'Accept': 'application/vnd.github+json'})
    with urlopen(request, timeout=12) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('업데이트 다운로드 크기 제한 초과')
    return data


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_target(root, name):
    if name not in FILES and name != 'release.json':
        raise ValueError('허용되지 않은 업데이트 파일: ' + name)
    path = root / name
    if path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise ValueError('업데이트 대상이 심볼릭 링크 또는 외부 경로입니다: ' + name)
    return path


def recover(root):
    transaction = root / '.update-transaction'
    if not transaction.exists():
        return
    journal = transaction / 'journal.json'
    if journal.exists():
        entries = json.loads(journal.read_text())
        for name, metadata in entries.items():
            target = safe_target(root, name)
            if metadata is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, (transaction / name).read_bytes(), metadata['mode'])
        log(root, '중단된 업데이트를 이전 코드로 복구했습니다')
    shutil.rmtree(transaction)


def validate_payload(manifest, archive, expected_version):
    if manifest.get('version') != expected_version or set(manifest.get('files', {})) != set(FILES):
        raise ValueError('릴리스 버전 또는 파일 목록 불일치')
    payload = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = bundle.namelist()
        if not names:
            raise ValueError('빈 업데이트 압축 파일')
        prefix = names[0].split('/')[0] + '/'
        for name in FILES:
            entry = bundle.getinfo(prefix + name)
            if entry.file_size > MAX_DOWNLOAD:
                raise ValueError('업데이트 파일 크기 제한 초과')
            data = bundle.read(entry)
            if hashlib.sha256(data).hexdigest() != manifest['files'][name]:
                raise ValueError('파일 검증 실패: ' + name)
            if name.endswith('.py'):
                compile(data, name, 'exec')
            elif name.endswith('.sh'):
                subprocess.run(['bash', '-n'], input=data, check=True, capture_output=True)
            payload[name] = data
    if payload['VERSION'].decode().strip() != expected_version:
        raise ValueError('VERSION 불일치')
    payload['release.json'] = (json.dumps(manifest, indent=2) + '\n').encode()
    return payload


def install(root, payload):
    transaction = root / '.update-transaction'
    transaction.mkdir(mode=0o700)
    entries = {}
    try:
        # Journal is durable before the first live file is changed.
        for name in payload:
            target = safe_target(root, name)
            entries[name] = None
            if target.exists():
                entries[name] = {'mode': target.stat().st_mode & 0o777}
                atomic_write(transaction / name, target.read_bytes())
        atomic_write(transaction / 'journal.json', json.dumps(entries).encode())
        for name, data in payload.items():
            mode = 0o755 if name.endswith('.sh') else 0o644
            atomic_write(safe_target(root, name), data, mode)
        # Retain one complete backup for manual recovery; rename commits the transaction.
        backup = root / '.update-backup'
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(transaction, backup)
    except BaseException:
        recover(root)
        raise


def check_update(root):
    local = (root / 'VERSION').read_text().strip() if (root / 'VERSION').exists() else '0.0.0'
    version(local)
    log(root, f'현재 버전 {local}; GitHub {REPOSITORY}/{BRANCH} 확인')
    # Git's ref lookup avoids the unauthenticated GitHub REST API rate limit.
    # Pin every download to this commit even if main changes during the update.
    ref = f'refs/heads/{BRANCH}'
    result = subprocess.run(
        ['git', 'ls-remote', '--exit-code', '--refs',
         f'https://github.com/{REPOSITORY}.git', ref],
        check=True, capture_output=True, text=True, timeout=15,
        env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
    )
    fields = result.stdout.strip().split()
    if len(fields) != 2 or fields[1] != ref or not re.fullmatch(r'[0-9a-f]{40}', fields[0]):
        raise ValueError('잘못된 GitHub 커밋')
    commit = fields[0]
    base = f'https://raw.githubusercontent.com/{REPOSITORY}/{commit}'
    remote = download(base + '/VERSION', 100).decode().strip()
    if version(remote) <= version(local):
        log(root, f'원격 버전 {remote}; 현재 코드 유지')
        return False
    log(root, f'새 버전 {remote} 다운로드 및 검증')
    manifest = json.loads(download(base + '/release.json', 64 * 1024))
    archive = download(f'https://codeload.github.com/{REPOSITORY}/zip/{commit}')
    payload = validate_payload(manifest, archive, remote)
    install(root, payload)
    log(root, f'{local} → {remote} 적용 완료; 새 코드로 재시작')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--recover-only', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    with (root / '.update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            recover(root)
        except Exception as exc:
            log(root, f'이전 코드 복구 실패; 혼합 버전 실행 중지: {exc}')
            return 20
        if args.recover_only:
            return 0
        try:
            return 10 if check_update(root) else 0
        except Exception as exc:
            if (root / '.update-transaction' / 'journal.json').exists():
                log(root, f'업데이트 복구 미완료; 실행 중지: {exc}')
                return 20
            log(root, f'업데이트 실패; 기존 코드로 시작: {exc}')
            return 0


if __name__ == '__main__':
    sys.exit(main())
