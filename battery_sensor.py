"""ADS1115 AIN0 battery measurement; calibration is persisted separately."""
import json
import math
from pathlib import Path
import statistics
import sys
import threading
import time

_READ_LOCK = threading.Lock()

CALIBRATION_FILE = Path(__file__).with_name('battery_calibration.json')


def read_raw(samples=8, bus_number=1, address=0x48):
    from smbus import SMBus
    bus = SMBus(bus_number)
    values = []
    try:
        for _ in range(samples):
            # AIN0-GND, +/-6.144 V, single-shot, 128 SPS, comparator disabled.
            bus.write_i2c_block_data(address, 1, [0xC1, 0x83])
            deadline = time.monotonic() + 0.1
            while True:
                time.sleep(0.002)
                high, low = bus.read_i2c_block_data(address, 1, 2)
                if high & 0x80:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('ADS1115 conversion timed out')
            high, low = bus.read_i2c_block_data(address, 0, 2)
            raw = (high << 8) | low
            if raw >= 32768:
                raw -= 65536
            if raw <= 0 or raw >= 32767:
                raise ValueError('ADS1115 reading is zero, negative or saturated')
            values.append(raw)
    finally:
        bus.close()
    return statistics.median(values)


def battery_from_raw(raw, calibration):
    reference = float(calibration['full_raw'])
    full = float(calibration['full_voltage'])
    empty = float(calibration['empty_voltage'])
    if not all(math.isfinite(v) for v in (raw, reference, full, empty)) or reference <= 0 or full <= empty:
        raise ValueError('Invalid battery calibration')
    voltage = raw / reference * full
    percent = max(0, min(100, round((voltage - empty) / (full - empty) * 100)))
    return {'voltage': round(voltage, 2), 'percent': percent}


def read_battery():
    calibration = json.loads(CALIBRATION_FILE.read_text())
    with _READ_LOCK:
        return battery_from_raw(read_raw(bus_number=calibration['bus'], address=calibration['address']), calibration)


class BatteryMonitor:
    def __init__(self, interval_seconds=5):
        self._interval = interval_seconds
        self._stop = threading.Event()

    def start(self):
        def run():
            while not self._stop.is_set():
                try:
                    battery = read_battery()
                    message = f"[BATTERY] Voltage={battery['voltage']:.2f}V | Bat={battery['percent']}%"
                except Exception as exc:
                    message = f"[BATTERY] 읽기 실패: {exc}"
                print(message, file=sys.stderr, flush=True)
                self._stop.wait(self._interval)
        threading.Thread(target=run, name='battery-monitor', daemon=True).start()

    def stop(self):
        self._stop.set()
