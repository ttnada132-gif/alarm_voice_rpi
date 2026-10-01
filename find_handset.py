#!/usr/bin/env python3
"""Find the PCM2902 audio card independently of USB input buttons."""
from pathlib import Path


def usb_parent(path):
    for parent in path.resolve().parents:
        try:
            if ((parent / 'idVendor').read_text().strip().lower() == '08bb'
                    and (parent / 'idProduct').read_text().strip().lower() == '2902'):
                return parent
        except OSError:
            continue
    return None


def find_handset():
    for card in sorted(Path('/sys/class/sound').glob('card[0-9]*')):
        parent = usb_parent(card)
        if parent is None:
            continue
        try:
            return (card / 'id').read_text().strip()
        except OSError:
            continue
    return None


if __name__ == '__main__':
    handset = find_handset()
    if handset:
        print(handset)
    else:
        raise SystemExit(1)
