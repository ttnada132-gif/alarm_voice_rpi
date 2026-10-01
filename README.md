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

`shinwhatech-wifi-portal.service`와 `shinwhatech-raspi-agent.service`는 부팅 시 자동 실행됩니다. 같은 Wi-Fi에서 장치 IP의 HTTP 주소로 접속해 Wi-Fi 이름과 비밀번호를 저장합니다. 웹 로그인 아이디는 `wifi_portal.json`의 `admin_username`(현재 `sd365`)이며 비밀번호는 `wifi_portal.json`의 `admin_password`입니다.

Wi-Fi 연결이 60초 이상 끊기면 저장된 목록을 재시도하고, 모두 실패하면 `sh_voice_alarm` 설정용 Wi-Fi를 엽니다. 비밀번호는 설정 파일의 `ap_password`이며 접속 주소는 `http://192.168.4.1/`입니다. 새 Wi-Fi 연결에 성공하면 저장하고 방송 서비스를 시작하며, 방송 프로그램은 방송 서버 연결 확인 후 실행합니다. 서버만 응답하지 않는 경우 기존 Wi-Fi를 유지합니다.

상태 확인: `systemctl status shinwhatech-wifi-portal shinwhatech-raspi-agent`

재시작: `sudo systemctl restart shinwhatech-wifi-portal shinwhatech-raspi-agent`

## PCM2902 수화기

시작 스크립트는 `find_handset.py`로 USB 식별자 `08bb:2902`의 오디오를 찾습니다. USB 버튼은 사용하지 않습니다. USB Bus/Device 번호나 Audio/Sound 이름에 의존하지 않습니다. `auto_start.sh`도 같은 시작 스크립트를 사용합니다. 네트워크와 방송 서버 확인 후 수화기 감지를 기다려 방송 프로그램을 실행합니다.

## GPIO4 수화기 훅 스위치

네트워크/서버 연결을 기다리는 동안에도 GPIO4 상태를 감시하여 `log.txt`에 수화기 들림/내려놓음을 기록합니다. 이 단계에서는 발신하지 않으며, 서버 연결 후 방송 프로그램이 감시를 이어받습니다.

`--hook-gpio 4`로 BCM GPIO4(물리 핀 7)를 내부 풀업 입력으로 사용합니다. 수화기 스위치를 GPIO4와 GND 사이에 연결합니다. LOW는 내려놓음(통화 종료), HIGH는 들림(발신)입니다. 물리 핀 4는 GPIO4가 아니므로 연결하지 않습니다. 반대 극성의 스위치는 접점 배선을 바꿔야 합니다.

접점은 기본 450ms 동안 안정된 상태만 처리합니다. 시작 시 이미 HIGH면 서버 연결 후 발신하며, LOW면 발신하지 않습니다. 내려놓으면 로컬 수화기 음성을 중단하고 기존 `/api/calls/<device_id>/end` API로 종료합니다. 서버 연결 실패 시 종료 요청을 재시도하고 다음 발신 전에 처리합니다. 발신은 기존 WebSocket `button` 이벤트를 사용합니다. GPIO23 SOS와 GPIO24 LED는 기존 기능을 유지합니다.
