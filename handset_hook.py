"""Debounced, active-high handset hook and retryable call control."""
import sys
import threading
import time


class HookController:
    def __init__(self, dial, hangup, silence):
        self.dial, self.hangup, self.silence = dial, hangup, silence
        self.lock = threading.Lock()
        self.off_hook = None
        self.revision = 0
        self.dialed = -1
        self.pending_end = None
        self.armed = False

    def changed(self, off_hook):
        with self.lock:
            if self.off_hook == off_hook:
                return
            was_armed = self.armed
            previous = self.off_hook
            self.off_hook = off_hook
            self.revision += 1
            if not off_hook:
                self.armed = True
            if not off_hook and previous is True and was_armed:
                self.pending_end = self.revision
        print('[통화] 수화기 ' + ('들림 (HIGH)' if off_hook else '내려놓음 (LOW)'), file=sys.stderr)
        if previous is None and off_hook:
            print('[통화] 시작 시 들림 신호: 자동 발신 차단, 수화기를 내려놓은 뒤 들어 주세요', file=sys.stderr)
        if not off_hook:
            self.silence()

    def sync(self):
        # Only the control worker calls sync; GPIO sampling never waits on HTTP.
        with self.lock:
            end = self.pending_end
            revision = self.revision
            dial = self.armed and self.off_hook and self.dialed != revision
        if end is not None:
            if self.hangup():
                with self.lock:
                    if self.pending_end == end:
                        self.pending_end = None
            return
        if dial and self.dial():
            with self.lock:
                self.dialed = revision


class GpioHookWatcher:
    def __init__(self, pin, debounce_ms, controller):
        self.pin = pin
        self.debounce = max(0.05, debounce_ms / 1000)
        self.controller = controller
        self.stop_event = threading.Event()
        self.threads = []

    def start(self):
        for target in (self.sample, self.control):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=1)

    def control(self):
        while not self.stop_event.wait(0.2):
            try:
                self.controller.sync()
            except Exception as exc:
                print(f'Hook call control retry: {exc}', file=sys.stderr)
                self.stop_event.wait(3)

    def sample(self):
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        print(f'Watching handset hook on BCM GPIO {self.pin} (pull-up, HIGH=off-hook, LOW=on-hook)', file=sys.stderr)
        candidate = GPIO.input(self.pin)
        since = time.monotonic()
        stable = None
        try:
            while not self.stop_event.wait(0.02):
                value = GPIO.input(self.pin)
                now = time.monotonic()
                if value != candidate:
                    candidate, since = value, now
                if stable != candidate and now - since >= self.debounce:
                    stable = candidate
                    self.controller.changed(stable == GPIO.HIGH)
        finally:
            GPIO.cleanup(self.pin)
