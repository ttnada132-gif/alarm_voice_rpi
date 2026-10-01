"""Shared validation and safe startup output for portal connection settings."""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def validate_connection(values):
    device_id = values.get('device_id', '').strip()
    server_url = values.get('server_url', '').strip().rstrip('/')
    token = values.get('stream_token', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', device_id):
        raise ValueError('기기 ID는 영문, 숫자, 밑줄, 하이픈으로 1~128자 입력하세요.')
    try:
        parsed = urlsplit(server_url)
        valid_url = (parsed.scheme in {'http', 'https'} and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment
                     and 0 < (parsed.port or 443) <= 65535)
    except ValueError:
        valid_url = False
    if (not valid_url or len(server_url) > 1024
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in server_url)):
        raise ValueError('서버 URL은 http:// 또는 https:// 주소로 입력하세요. 공백, 인증정보, 쿼리는 제외하세요.')
    if not 1 <= len(token) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError('스트림 토큰은 공백 없이 영문, 숫자, 기호로 1~512자 입력하세요.')
    return {'device_id': device_id, 'server_url': server_url, 'stream_token': token,
            'server_test_url': server_url + '/api/pi/wifi-check'}


if __name__ == '__main__':
    settings = validate_connection(json.loads(Path(sys.argv[1]).read_text()))
    for key in ('device_id', 'server_url', 'stream_token'):
        print(settings[key])
