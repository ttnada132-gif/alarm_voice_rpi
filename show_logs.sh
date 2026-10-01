#!/bin/bash
set -euo pipefail
SCRIPT_DIR="/home/pi/shinwhatech"
if [[ "${1:-}" != "--follow" ]]; then
  export DISPLAY="${DISPLAY:-:0}"
  export XAUTHORITY="${XAUTHORITY:-/home/pi/.Xauthority}"
  exec /usr/bin/lxterminal --geometry=120x32 --title="ShinwhaTech 방송 / 수화기 / 연결 로그" -e "$SCRIPT_DIR/show_logs.sh --follow"
fi

echo '통화 / SOS 실시간 로그 (창을 닫아도 서비스는 계속 실행됩니다)'
echo '과거 로그 30줄을 표시한 뒤 새 이벤트를 표시합니다.'
echo '수화기 들림 / 수화기 내려놓음: 수화기 상태 변경'
echo 'Listening for server control/audio: 방송 서버 연결 성공'
echo 'Incoming call: 수신 / Call accepted: 통화 연결 / Call ended: 종료'
echo 'Streaming handset microphone: 음성 송신 / Prepared handset speaker: 음성 수신 준비'
echo '상태는 5초마다 확인하며 변경될 때 표시합니다.'

show_status() {
  previous=''
  while true; do
    service_state=$(systemctl is-active shinwhatech-raspi-agent.service 2>/dev/null || true)
    if pgrep -f '^/usr/bin/python3 /home/pi/shinwhatech/raspi_agent.py( |$)' >/dev/null; then
      stage='방송 에이전트 실행 중 (서버 연결 여부는 아래 로그 확인)'
    elif pgrep -f '^/usr/bin/python3 /home/pi/shinwhatech/wifi_connect.py( |$)' >/dev/null; then
      stage='네트워크/서버 연결 확인 중 — 방송 에이전트 시작 전'
    elif [[ "$service_state" == 'active' ]] && ! /usr/bin/python3 "$SCRIPT_DIR/find_handset.py" >/dev/null 2>&1; then
      stage='USB 수화기(PCM2902) 미인식 — 장치 연결 대기 중'
    else
      stage='방송 에이전트 미실행 / 시작 준비 중'
    fi
    current="서비스=$service_state | $stage"
    if [[ "$current" != "$previous" ]]; then
      echo "$(date '+%F %T') [상태] $current"
      previous="$current"
    fi
    sleep 5
  done
}
show_status &
status_pid=$!
trap 'kill "$status_pid" 2>/dev/null || true' EXIT
trap 'exit 0' INT TERM HUP
tail -n 30 -F "$SCRIPT_DIR/log.txt"
