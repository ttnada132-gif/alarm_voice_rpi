#!/usr/bin/env python3
import sys
import time

try:
    import serial
except ImportError:
    print("pyserial 이 설치되지 않았습니다. 다음 명령으로 설치하세요:")
    print("pip3 install pyserial")
    sys.exit(1)


SERIAL_PORT = "/dev/serial0"
BAUDRATE = 9600


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def nmea_to_decimal(raw_value, direction):
    if not raw_value or not direction:
        return None

    if direction in ("N", "S"):
        degree_len = 2
    elif direction in ("E", "W"):
        degree_len = 3
    else:
        return None

    degrees = float(raw_value[:degree_len])
    minutes = float(raw_value[degree_len:])
    decimal = degrees + (minutes / 60.0)

    if direction in ("S", "W"):
        decimal *= -1

    return decimal


def parse_nmea(sentence):
    parts = sentence.strip().split(",")
    if not parts:
        return None, None

    if parts[0] == "$GPGGA" or parts[0] == "$GNGGA":
        lat = nmea_to_decimal(parts[2], parts[3])
        lon = nmea_to_decimal(parts[4], parts[5])
        return lat, lon

    if parts[0] == "$GPRMC" or parts[0] == "$GNRMC":
        if len(parts) > 6 and parts[2] == "A":
            lat = nmea_to_decimal(parts[3], parts[4])
            lon = nmea_to_decimal(parts[5], parts[6])
            return lat, lon

    return None, None


def parse_gsv(sentence):
    parts = sentence.strip().split(",")
    if not parts or parts[0] not in ("$GPGSV", "$GNGSV"):
        return None

    total_messages = safe_int(parts[1]) if len(parts) > 1 else None
    message_number = safe_int(parts[2]) if len(parts) > 2 else None
    satellites_in_view = safe_int(parts[3]) if len(parts) > 3 else None

    snr_values = []
    for index in range(7, len(parts), 4):
        snr_field = parts[index].split("*")[0]
        snr = safe_int(snr_field)
        if snr is not None and snr > 0:
            snr_values.append(snr)

    return total_messages, message_number, satellites_in_view, snr_values


def main():
    try:
        with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1) as ser:
            print(f"GPS 수신 시작: {SERIAL_PORT}, {BAUDRATE}bps")
            gsv_expected = None
            gsv_current = 0
            gsv_satellites_in_view = None
            gsv_snr_values = []
            while True:
                line = ser.readline().decode("ascii", errors="ignore").strip()
                if not line.startswith("$"):
                    continue
                #print(line)

                gsv_data = parse_gsv(line)
                if gsv_data is not None:
                    total_messages, message_number, satellites_in_view, snr_values = gsv_data

                    if message_number == 1:
                        gsv_expected = total_messages
                        gsv_current = 1
                        gsv_satellites_in_view = satellites_in_view
                        gsv_snr_values = list(snr_values)
                    else:
                        gsv_current = message_number or 0
                        gsv_satellites_in_view = satellites_in_view
                        gsv_snr_values.extend(snr_values)

                    if gsv_expected is not None and gsv_current == gsv_expected:
                        if gsv_snr_values:
                            avg_snr = sum(gsv_snr_values) / len(gsv_snr_values)
                            max_snr = max(gsv_snr_values)
                            print(
                                "GPS 신호세기: "
                                f"추적 위성 {len(gsv_snr_values)}개 / "
                                f"가시 위성 {gsv_satellites_in_view}개, "
                                f"평균 SNR {avg_snr:.1f} dB, 최대 SNR {max_snr} dB"
                            )
                        else:
                            print(
                                "GPS 신호세기: "
                                f"가시 위성 {gsv_satellites_in_view}개, "
                                "SNR 정보 없음"
                            )

                lat, lon = parse_nmea(line)
                if lat is not None and lon is not None:
                    print(f"위도: {lat:.6f}, 경도: {lon:.6f}")
                    time.sleep(1)
    except serial.SerialException as exc:
        print(f"시리얼 포트를 열 수 없습니다: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
