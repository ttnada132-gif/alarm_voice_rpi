# Flask Live Voice Monitor

Flask-based Raspberry Pi device registry and live audio relay.

## Why this structure

For this project, `POST + WebSocket` is a better base than pure MQTT:

- `POST` is easier to set up on Raspberry Pi and PC because it only needs normal HTTP access.
- Device registration, heartbeat, company name, install location, and IP tracking fit naturally into `POST` APIs.
- Real-time voice should not use repeated `POST` uploads. WebSocket is still the right transport for low-latency PCM streaming.

So the current base structure is:

```text
Raspberry Pi agent -> POST register/heartbeat + WebSocket downlink receiver -> Flask device registry -> Raspberry Pi speaker
Raspberry Pi handset button -> WebSocket call request -> Flask call state -> Browser answer button
Browser microphone -> WebSocket talkback uplink -> Flask per-device downlink hub -> Raspberry Pi USB handset speaker
Raspberry Pi USB handset microphone -> WebSocket audio uplink -> Flask per-device audio hub -> Browser speaker
Browser TTS form -> server-side TTS -> Flask per-device downlink hub -> Raspberry Pi AUX output
```

This gives the PC server a persistent Raspberry Pi list first, and keeps the audio path separate and extensible.

## What it does now

- Raspberry Pi can register itself with `device_id`, `source_name`, `company_name`, and `install_location`
- Server tracks the latest IP and heartbeat time for each device
- Raspberry Pi can raise an incoming call when its USB handset button is pressed
- Web page shows incoming calls and allows one active two-way call at a time
- Browser microphone audio is sent to the selected Raspberry Pi USB handset speaker
- Raspberry Pi USB handset microphone audio is sent back to the browser
- Raspberry Pi plays ringing audio on the USB handset while waiting for browser answer
- Per-device TTS messages are generated on the server and played on the Raspberry Pi AUX output
- Raspberry Pi GPIO 23 LOW plays `/home/pi/shinwhatech/0001.mp3` every 10 seconds, notifies the server, and the browser repeats `static/0001.mp3` until the alert is confirmed
- Raspberry Pi reads GPS once per minute, caches the last latitude/longitude, and the server shows latitude, longitude, and Korean reverse-geocoded address in each device card
- Raspberry Pi sets GPIO 24 HIGH while GPIO 23 alert is active and LOW after alert confirmation

## Server setup

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:STREAM_TOKEN = "change-this-token"
python app.py
```

Open `http://127.0.0.1:5000` in a browser.

## Raspberry Pi setup

Install ALSA tools on the Pi:

```bash
sudo apt update
sudo apt install -y alsa-utils evtest mpg123 python3-rpi.gpio
python3 -m pip install -r requirements.txt
```

Keep the Raspberry Pi visible in the server list, watch the handset button, and handle call audio:

```bash
python3 raspi_agent.py --server http://SERVER_IP:5000 --token change-this-token --device-id pi-lab-1 --company-name Shinwha --install-location Office-A --handset-input-device plughw:1,0 --handset-speaker-device plughw:1,0 --aux-device plughw:0,0 --button-device /dev/input/event0 --gpio-alert-pin 23 --gpio-alert-led-pin 24 --gpio-alert-file /home/pi/shinwhatech/0001.mp3 --gpio-alert-interval-seconds 10 --gps-port /dev/serial0 --gps-interval-seconds 60
```

If the handset button is wired to a Pi GPIO instead of a USB input event:

```bash
python3 raspi_agent.py --server http://SERVER_IP:5000 --token change-this-token --device-id pi-lab-1 --handset-input-device plughw:1,0 --handset-speaker-device plughw:1,0 --aux-device plughw:0,0 --button-gpio 17 --gpio-alert-pin 23 --gpio-alert-led-pin 24 --gpio-alert-file /home/pi/shinwhatech/0001.mp3 --gpio-alert-interval-seconds 10 --gps-port /dev/serial0 --gps-interval-seconds 60
```

Optional sender arguments:

- `--handset-sample-rate 16000`
- `--handset-input-device plughw:1,0`
- `--handset-speaker-device plughw:1,0`
- `--aux-device plughw:0,0`
- `--button-device /dev/input/event0`
- `--button-key-code -1`
- `--button-gpio 17`
- `--gpio-alert-pin 23`
- `--gpio-alert-led-pin 24`
- `--gpio-alert-file /home/pi/shinwhatech/0001.mp3`
- `--gpio-alert-output-device plughw:0,0`
- `--gpio-alert-interval-seconds 10`
- `--gps-port /dev/serial0`
- `--gps-interval-seconds 60`
- `--gps-cache-file /home/pi/shinwhatech/last_location.json`
- `--chunk-samples 1024`
- `--company-name Shinwha`
- `--install-location Office-A`

The older standalone microphone sender still exists for manual testing:

```bash
python3 raspi_mic_sender.py --server http://SERVER_IP:5000 --token change-this-token --device-id pi-lab-1 --source-name pi-lab-1
```

Legacy talkback test arguments:

- `--speaker-device plughw:1,0`
- `--speaker-sample-rate 16000`
- `--reconnect-seconds 3`

Optional PC microphone sender arguments:

- `--input-device "Microphone (USB Audio Device)"`
- `--sample-rate 16000`
- `--chunk-samples 1024`
- `--list-devices`

## HTTP endpoints

- `GET /` : device directory and live listener UI
- `GET /api/health` : health check
- `GET /api/live/status` : summary counts
- `GET /api/devices` : Raspberry Pi list and per-device status
- `POST /api/pi/register` : Raspberry Pi registration
- `POST /api/pi/heartbeat` : Raspberry Pi heartbeat
- `POST /api/calls/<device_id>/accept` : answer an incoming call
- `POST /api/calls/<device_id>/end` : end the active or ringing call
- `POST /api/calls/<device_id>/clear` : acknowledge an ended call and return it to standby
- `POST /api/devices/<device_id>/alert/clear` : acknowledge GPIO alert and stop Pi/local browser alarm playback
- `POST /api/devices/<device_id>/tts` : generate TTS and send it to Raspberry Pi AUX playback

## WebSocket endpoints

- `/ws/live/send?token=...` : Raspberry Pi microphone uplink
- `/ws/live/listen/<device_id>` : browser listener for one Raspberry Pi
- `/ws/pi/listen/<device_id>?token=...` : Raspberry Pi speaker downlink for server talkback audio
- `/ws/pi/send/<device_id>?token=...` : server PC microphone uplink for Raspberry Pi talkback
- `/ws/call/send/<device_id>` : browser microphone uplink for the active call

## Next step candidates

- Persist device metadata in a database instead of memory
- Add authentication for browser users

## Testing

### TTS receipt response

On receiving a `tts_audio` message, the device sends the following JSON on the
same WebSocket before starting playback:

```json
{"type":"tts_response","device_id":"SH_VOICE_ALARM_005","response":"success"}
```

This acknowledges receipt only, not playback completion. The server must handle
`tts_response` messages. The device logs the response after the WebSocket send
succeeds; this log does not confirm that the server processed the response.

```bash
pytest
```

## 방송 장치 웹 Wi-Fi 설정

`shinwhatech-wifi-portal.service`와 `shinwhatech-raspi-agent.service`는 부팅 시 자동 실행됩니다. 같은 Wi-Fi에서 장치 IP의 HTTP 주소로 접속해 Wi-Fi 이름과 비밀번호를 저장합니다. 설정 페이지는 아이디와 비밀번호 입력 없이 바로 열립니다. 기존 `admin_username`, `admin_password` 설정은 사용하지 않습니다.

Wi-Fi 연결이 60초 이상 끊기면 저장된 목록을 재시도하고, 모두 실패하면 `sh_voice_alarm` 설정용 Wi-Fi를 엽니다. 설정용 AP는 비밀번호 없이 접속할 수 있으며(기존 `ap_password` 값은 사용하지 않음), 접속 주소는 `http://192.168.4.1/`입니다. 새 Wi-Fi 연결에 성공하면 저장하고 방송 서비스를 시작하며, 방송 프로그램은 방송 서버 연결 확인 후 실행합니다. 서버만 응답하지 않는 경우 기존 Wi-Fi를 유지합니다.

상태 확인: `systemctl status shinwhatech-wifi-portal shinwhatech-raspi-agent`

재시작: `sudo systemctl restart shinwhatech-wifi-portal shinwhatech-raspi-agent`

## PCM2902 수화기

시작 스크립트는 `find_handset.py`로 USB 식별자 `08bb:2902`의 오디오를 찾습니다. USB 버튼은 사용하지 않습니다. USB Bus/Device 번호나 Audio/Sound 이름에 의존하지 않습니다. `auto_start.sh`도 같은 시작 스크립트를 사용합니다. 네트워크와 방송 서버 확인 후 수화기 감지를 기다려 방송 프로그램을 실행합니다.

## GPIO4 수화기 훅 스위치

네트워크/서버 연결을 기다리는 동안에도 GPIO4 상태를 감시하여 `log.txt`에 수화기 들림/내려놓음을 기록합니다. 이 단계에서는 발신하지 않으며, 서버 연결 후 방송 프로그램이 감시를 이어받습니다.

`--hook-gpio 4`로 BCM GPIO4(물리 핀 7)를 내부 풀업 입력으로 사용합니다. 수화기 스위치를 GPIO4와 GND 사이에 연결합니다. LOW는 내려놓음(통화 종료), HIGH는 들림(발신)입니다. 물리 핀 4는 GPIO4가 아니므로 연결하지 않습니다. 반대 극성의 스위치는 접점 배선을 바꿔야 합니다.

접점은 기본 450ms 동안 안정된 상태만 처리합니다. 시작 시에는 HIGH/LOW 어느 상태에서도 발신하지 않습니다. LOW(내려놓음)가 확인된 뒤 HIGH(들림)로 바뀔 때만 발신합니다. 서버에서 통화를 종료해도 수화기를 내려놓았다가 다시 들면 새로 발신합니다. 내려놓으면 로컬 수화기 음성을 중단하고 기존 `/api/calls/<device_id>/end` API로 종료합니다. 서버 연결 실패 시 종료 요청을 재시도하고 다음 발신 전에 처리합니다. 발신은 기존 WebSocket `button` 이벤트를 사용합니다. GPIO23 SOS와 GPIO24 LED는 기존 기능을 유지합니다.

## 부팅 시 자동 업데이트 (현재 버전 1.0.1)

`VERSION`에 설치 버전을, `release.json`에 버전 및 실행 파일별 SHA-256을 기록합니다.
시작 스크립트는 기존 Wi-Fi/방송 서버 연결 확인이 끝난 뒤
`ttnada132-gif/alarm_voice_rpi`의 `main` 최신 커밋을 확인합니다.
설치 버전보다 높은 `MAJOR.MINOR.PATCH` 버전일 때만 해당 커밋의 코드를 내려받습니다.
파일 목록·해시·Python/Bash 구문을 검증한 뒤 `/home/pi/shinwhatech`에 적용하고
시작 스크립트를 새 코드로 다시 실행합니다. Wi-Fi 설정 서비스도 재시작합니다.
라즈베리파이 OS 전체를 재부팅하지는 않습니다.

- `wifi_portal.json`, `wifi_networks.csv`, `last_location.json`, `log.txt` 등 장치별 데이터는 보존합니다.
- GitHub 연결/다운로드/검증 실패 시 기존 코드로 실행합니다. 낮거나 같은 버전은 설치하지 않습니다.
- 적용 중 오류 시 이전 파일로 복원합니다. 전원 차단 등으로 중단되면 다음 시작 시 네트워크 확인 전에 복원합니다.
- 직전 파일 백업은 `.update-backup/`에 보관하며, 진행 중인 복구 정보는 `.update-transaction/`에 보관합니다.
- 업데이트 결과와 버전은 `log.txt`의 `[업데이트]` 항목에서 확인할 수 있습니다.
- 실행 코드와 템플릿, 알람 음원만 업데이트합니다. Python 패키지 설치와 systemd 유닛 변경은 자동화하지 않습니다. 이 릴리스는 추가 패키지가 필요 없습니다.
- 실행 폴더에는 Git 저장소나 GitHub 인증정보가 필요하지 않습니다. 공개 저장소의 HTTPS 주소를 이용합니다.

새 버전을 배포할 때는 코드를 수정한 후 **마지막으로** 아래 명령을 실행하고,
생성된 `VERSION`, `release.json`도 함께 `main`에 push합니다.

```bash
python3 build_release.py 1.0.2
python3 -m unittest discover -v
git add <수정한파일> VERSION release.json
git commit -m "Release 1.0.2"
git push origin main
```

버전을 올리지 않으면 코드를 push해도 장치는 업데이트하지 않습니다.
릴리스 파일을 수정하면 `build_release.py`를 다시 실행하여 해시를 갱신해야 합니다.
이미 설치된 장치는 다음 부팅 또는 `sudo systemctl restart shinwhatech-raspi-agent` 시 확인합니다.
구버전 시작 스크립트에는 자동 업데이트 기능이 없으므로 최초 한 번은
`auto_update.py`와 새 `run_raspi_agent.sh`를 실행 폴더에 설치해야 합니다.

DNS가 준비되지 않은 동안 등록·상태 보고·방송 재접속은 자동 재시도하며, 같은 대기 상태의 오류를 반복 출력하지 않습니다. 다른 서버 오류는 로그에 표시합니다.
