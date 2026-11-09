#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
SARA FM PTT Controller
=======================
Confirmed working on CM108B (PID=0x013c):
  GPIO3 (0x04), Format 1, Interface 3, Usage Page 0x000c
  ON:  [0, 0, 4, 4, 0, 0, 0, 0]
  OFF: [0, 0, 0, 4, 0, 0, 0, 0]
"""

import threading
import time

PTT_VOX    = "VOX"
PTT_RTS    = "RTS"
PTT_DTR    = "DTR"
PTT_CM108  = "CM108"
PTT_HAMLIB = "HAMLIB"
# v0.9.81: a Raspberry Pi GPIO pin keying the radio (through a transistor or
# opto-isolator), for go-kits with no USB interface PTT. BCM pin numbers.
PTT_PIGPIO = "Pi GPIO"
PI_GPIO_DEFAULT_PIN = 17

# Confirmed GPIO pin for CM108B PID=0x013c
CM108_GPIO_PIN = 0x04


# v0.9.69: how long Windows took over the RA-Board (seconds) -- the slow
# calls recorded here, newest last, for the program to report. Field (0.9.68,
# DV1UK, two RA-Boards): "Testing PTT" took ~10 s a board, Start took 20 s and
# one PING went out 10 s late; the window showed "Not Responding" each time.
# While Windows answers an HID call the whole program waits (the HID library
# holds Python's lock), so a slow RA-Board freezes the window and the audio.
SLOW_HID_S = 0.5
slow_hid_events: list = []          # (time, what, seconds)
_hidapi_ok = False


def _note_hid(what: str, t0: float):
    dt = time.time() - t0
    if dt >= SLOW_HID_S:
        slow_hid_events.append((time.time(), what, dt))
        del slow_hid_events[:-20]
    return dt


def take_slow_hid() -> list:
    out = list(slow_hid_events)
    slow_hid_events.clear()
    return out


def _ensure_hidapi():
    """Make sure hidapi (with DLL) is installed, not the broken 'hid' package."""
    global _hidapi_ok
    if _hidapi_ok:
        return True            # v0.9.69: checked once -- each check was a full HID scan
    try:
        import hid
        # Quick test - if enumerate works, we're good
        t0 = time.time()
        hid.enumerate()
        _note_hid("scanning USB for RA-Boards", t0)
        _hidapi_ok = True
        return True
    except Exception:
        pass
    import sys
    if getattr(sys, "frozen", False):
        # Inside the standalone .exe, hidapi is bundled and sys.executable
        # is SARA-PP.exe itself -- running "sys.executable -m pip ..." would
        # LAUNCH MORE COPIES OF THE APP, not repair anything. Report failure.
        return False
    # Try to fix: uninstall 'hid', install 'hidapi'
    try:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "hid"],
                       capture_output=True)
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "hidapi"],
                       capture_output=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "hidapi"],
                       capture_output=True)
        # Force reimport
        import importlib
        if 'hid' in __import__('sys').modules:
            importlib.reload(__import__('sys').modules['hid'])
        import hid
        hid.enumerate()
        return True
    except Exception:
        return False


def _find_cm108_devices():
    """
    Find all CM108 HID devices.
    Returns list of (name, path) tuples.
    """
    _ensure_hidapi()
    import hid
    found = []
    # Enumerate ALL HID devices, filter for C-Media VID=0x0d8c
    # Do NOT filter by usage_page - cm108_diag worked without that filter
    t0 = time.time()
    all_hid = hid.enumerate()
    _note_hid("scanning USB for RA-Boards", t0)
    idx = 1
    seen = set()
    for d in all_hid:
        if d.get('vendor_id') != 0x0d8c:
            continue
        path = d.get('path', b'')
        if path in seen:
            continue
        seen.add(path)
        name = f"RA-Board-{idx}"
        found.append((name, path))
        idx += 1
    return found


def enumerate_ra_boards():
    """Return list of (name, path, chip) for GUI display."""
    try:
        devices = _find_cm108_devices()
        return [(name, path, 'CM108') for name, path in devices]
    except Exception:
        return []


class CM108PTT:
    """
    Direct CM108 HID PTT.
    Uses exact method confirmed by ptt_test3: GPIO3 (0x04), format 1.
    """

    def __init__(self, path=None, gpio_pin=CM108_GPIO_PIN):
        self._path     = path
        self._gpio_pin = gpio_pin
        self._dev      = None
        self._keyed    = False

    def open(self):
        import hid

        devices = _find_cm108_devices()
        if not devices:
            raise RuntimeError(
                "No CM108/RA-Board found.\n"
                "Check USB and run: pip install hidapi")

        # If a specific path was given, use it; otherwise use first found
        target_path = self._path
        if not target_path:
            target_path = devices[0][1]

        # Try to open — exactly as cm108_diag did
        t0 = time.time()
        self._dev = hid.device()
        self._dev.open_path(target_path)
        self._dev.set_nonblocking(1)
        _note_hid("opening the RA-Board", t0)
        self._opened_path = target_path     # v0.8.9: reopen THIS board only

        # Send PTT OFF immediately to verify device is responsive
        self._dev.write([0x00, 0x00, 0x00,
                         self._gpio_pin, 0x00, 0x00, 0x00, 0x00])
        return True

    def close(self):
        if self._dev:
            try:
                self._dev.write([0x00, 0x00, 0x00,
                                 self._gpio_pin, 0x00, 0x00, 0x00, 0x00])
                self._dev.close()
            except Exception:
                pass
            self._dev = None
        self._keyed = False

    # v0.8.9 -- field: the radio stayed keyed until SARA-PP was restarted.
    # A failed HID write (USB hiccup -- e.g. RF getting into the cabling)
    # raised out of ptt_off(), and keyed-state flags had already been
    # flipped, so later release attempts did nothing. Writes now check the
    # result, retry, and on a stale handle REOPEN the same board by its USB
    # path (never "the first board found": a station with two RA-Boards
    # must never key the other radio). ptt_off always sends OFF.
    WRITE_ATTEMPTS = 3

    def _reopen(self):
        import hid
        path = getattr(self, "_opened_path", None) or self._path
        if not path:
            raise RuntimeError("CM108 device path unknown -- can't reopen safely")
        present = [p for _, p in _find_cm108_devices()]
        if path not in present:
            raise RuntimeError("RA-Board/CM108 not present on USB (unplugged or resetting)")
        dev = hid.device()
        dev.open_path(path)
        dev.set_nonblocking(1)
        self._dev = dev

    def _send(self, on: bool):
        t0 = time.time()
        try:
            return self._send_inner(on)
        finally:
            _note_hid(f"PTT {'ON' if on else 'OFF'} on the RA-Board", t0)

    def _send_inner(self, on: bool):
        pin = self._gpio_pin
        report = [0x00, 0x00, pin if on else 0x00, pin, 0x00, 0x00, 0x00, 0x00]
        last = None
        for attempt in range(self.WRITE_ATTEMPTS):
            try:
                if self._dev is None:
                    self._reopen()
                r = self._dev.write(report)
                if r is None or r >= 0:
                    return
                last = OSError(f"HID write returned {r}")
            except Exception as e:
                last = e
            try:
                if self._dev is not None:
                    self._dev.close()
            except Exception:
                pass
            self._dev = None
            time.sleep(0.05 * (attempt + 1))
        raise RuntimeError(f"CM108 PTT {'ON' if on else 'OFF'} failed after "
                           f"{self.WRITE_ATTEMPTS} tries: {last}")

    def ptt_on(self):
        self._send(True)           # always: after a USB reset the real state is unknown
        self._keyed = True

    def ptt_off(self):
        self._send(False)          # always: releasing is safe, and state may be stale
        self._keyed = False

    @property
    def is_keyed(self):
        return self._keyed


def open_pi_gpio(pin: int):
    """v0.9.81: a Raspberry Pi output pin, LOW (unkeyed) to start. Uses
    gpiozero, which ships with Raspberry Pi OS (sudo apt install
    python3-gpiozero elsewhere). Returns an object with on() / off() / close()."""
    try:
        from gpiozero import OutputDevice
    except ImportError:
        raise RuntimeError("Pi GPIO PTT needs gpiozero: sudo apt install python3-gpiozero")
    try:
        return OutputDevice(int(pin), active_high=True, initial_value=False)
    except Exception as e:
        raise RuntimeError(f"Cannot use GPIO {pin} for PTT: {e} "
                           f"(is this a Raspberry Pi, and is the user in the 'gpio' group?)")


class PTTController:
    """Universal PTT controller — VOX, RTS, DTR, CM108, HAMLIB."""

    def __init__(self, method=PTT_VOX, port=None, baudrate=9600,
                 ra_board_path=None, gpio_pin=CM108_GPIO_PIN,
                 hamlib_host="127.0.0.1", hamlib_port=4532,
                 tx_delay_ms=0, tail_ms=0, pi_gpio_pin=PI_GPIO_DEFAULT_PIN):
        self.method       = method
        self.port         = port
        self.baudrate     = baudrate
        self.ra_path      = ra_board_path
        self.gpio_pin     = gpio_pin
        self.hamlib_host  = hamlib_host
        self.hamlib_port  = hamlib_port
        self.tx_delay_ms  = tx_delay_ms
        self.tail_ms      = tail_ms
        self._lock        = threading.Lock()
        self.release_failed = False      # v0.8.9: last PTT release didn't go through
        self._keyed       = False
        self._serial      = None
        self._hamlib      = None
        self._cm108       = None
        self.pi_gpio_pin  = pi_gpio_pin
        self._gpio        = None

    def open(self):
        if self.method == PTT_VOX:
            return True

        if self.method in (PTT_RTS, PTT_DTR):
            try:
                import serial
                self._serial = serial.Serial(
                    port=self.port, baudrate=self.baudrate, timeout=1)
                self._serial.rts = False
                self._serial.dtr = False
                return True
            except ImportError:
                raise RuntimeError("Run: pip install pyserial")
            except Exception as e:
                raise RuntimeError(f"Cannot open {self.port}: {e}")

        if self.method == PTT_CM108:
            self._cm108 = CM108PTT(path=self.ra_path,
                                   gpio_pin=self.gpio_pin)
            self._cm108.open()
            return True

        if self.method == PTT_PIGPIO:
            self._gpio = open_pi_gpio(self.pi_gpio_pin)
            return True

        if self.method == PTT_HAMLIB:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((self.hamlib_host, self.hamlib_port))
            self._hamlib = s
            return True

        return False

    def close(self):
        try: self.ptt_off()
        except Exception: pass
        if self._serial:
            try: self._serial.close()
            except Exception: pass
            self._serial = None
        if self._cm108:
            try: self._cm108.close()
            except Exception: pass
            self._cm108 = None
        if self._hamlib:
            try: self._hamlib.close()
            except Exception: pass
            self._hamlib = None
        if self._gpio:
            try: self._gpio.close()
            except Exception: pass
            self._gpio = None

    def ptt_on(self):
        with self._lock:
            if self._keyed and not self.release_failed:
                return
            self._keyed = True
            if self.method == PTT_VOX:
                return
            if self.method == PTT_RTS and self._serial:
                self._serial.rts = True
            elif self.method == PTT_DTR and self._serial:
                self._serial.dtr = True
            elif self.method == PTT_CM108 and self._cm108:
                self._cm108.ptt_on()
            elif self.method == PTT_PIGPIO and self._gpio:
                self._gpio.on()
            elif self.method == PTT_HAMLIB and self._hamlib:
                try: self._hamlib.sendall(b"T 1\n")
                except Exception: pass

    def ptt_off(self):
        """Release PTT. If the release fails, the state stays 'keyed' and
        release_failed is set, so the next ptt_off() / ensure_released()
        really tries again (v0.8.9: previously the state was cleared BEFORE
        the hardware write, so one failed write left the radio keyed with
        every later release skipped)."""
        with self._lock:
            if not self._keyed and not self.release_failed:
                return
            try:
                if self.method == PTT_RTS and self._serial:
                    self._serial.rts = False
                elif self.method == PTT_DTR and self._serial:
                    self._serial.dtr = False
                elif self.method == PTT_CM108 and self._cm108:
                    self._cm108.ptt_off()
                elif self.method == PTT_PIGPIO and self._gpio:
                    self._gpio.off()
                elif self.method == PTT_HAMLIB and self._hamlib:
                    try: self._hamlib.sendall(b"T 0\n")
                    except Exception: pass
            except Exception:
                self.release_failed = True
                raise
            self._keyed = False
            self.release_failed = False

    def ensure_released(self) -> bool:
        """Watchdog hook: if a previous release failed, try again. Returns
        True if PTT is (now) released."""
        if not self.release_failed:
            return True
        try:
            self.ptt_off()
        except Exception:
            return False
        return True

    @property
    def is_keyed(self):
        return self._keyed

    @staticmethod
    def list_serial_ports():
        try:
            import serial.tools.list_ports
            return [p.device for p in serial.tools.list_ports.comports()]
        except ImportError:
            return []

    @staticmethod
    def list_ra_boards():
        return [name for name, path, chip in enumerate_ra_boards()]
