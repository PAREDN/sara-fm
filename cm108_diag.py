#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 PAREDN (Philippine Amateur Radio Emergency Data Network) / DW1IRF
"""
CM108 PTT Diagnostic — SARA FM  (interactive version)
Run:  python cm108_diag.py
"""
import sys, time

print("=" * 55)
print("  SARA FM — CM108 PTT Diagnostic  (interactive)")
print("=" * 55)
print()

try:
    import hid
    hid.enumerate()
except ImportError:
    print("FAIL — run:  pip uninstall hid hidapi -y && pip install hidapi")
    sys.exit(1)
except Exception:
    print("FAIL — run:  pip uninstall hid hidapi -y && pip install hidapi")
    sys.exit(1)

# Find CM108
cm_devs = [d for d in hid.enumerate() if d.get('vendor_id') == 0x0d8c]
if not cm_devs:
    print("No CM108 device found. Check USB cable.")
    sys.exit(1)

# Open first interface that accepts writes
dev = None
for d in cm_devs:
    try:
        d2 = hid.device()
        d2.open_path(d['path'])
        d2.set_nonblocking(1)
        d2.write([0,0,0,0x08,0,0,0,0])   # safe PTT OFF
        dev = d2
        print(f"Opened: VID={d['vendor_id']:04x} PID={d['product_id']:04x}")
        print(f"  path: {d['path']}")
        print()
        break
    except Exception as e:
        print(f"Skip {d['path']}: {e}")

if not dev:
    print("Could not open any CM108 interface.")
    print("Try: run as Administrator, or check Zadig driver.")
    sys.exit(1)

# Interactive one-pin-at-a-time test
PINS = [
    (0x08, "GPIO4 (0x08)  — most common on PARA RA-Board"),
    (0x04, "GPIO3 (0x04)  — used on some CM108AH builds"),
    (0x02, "GPIO2 (0x02)"),
    (0x01, "GPIO1 (0x01)"),
]

print("Watch the PTT LED on the RA-Board.")
print("Each pin will key for 2 seconds so you can see it clearly.")
print()

winner = None
for pin, label in PINS:
    input(f"  Press ENTER to test {label} ...")
    print(f"  >>> PTT ON  ({label}) — LED should light now <<<")
    dev.write([0,0,pin,pin,0,0,0,0])   # PTT ON
    time.sleep(2.0)
    dev.write([0,0,0,pin,0,0,0,0])    # PTT OFF
    print(f"  >>> PTT OFF <<<")
    ans = input("  Did the LED light? (y/n): ").strip().lower()
    if ans == 'y':
        winner = (pin, label)
        print(f"\n  *** FOUND IT: {label} ***\n")
        break
    print()

dev.close()

print()
if winner:
    pin, label = winner
    print(f"Result: Use {label}")
    print()
    print("In SARA FM:")
    print("  Settings → PTT / RA-Board")
    print(f"  GPIO dropdown → select  {label.split()[0]}  ({label.split()[1]})")
    print("  Click Apply, then PTT TEST to confirm.")
else:
    print("No GPIO pin triggered the LED.")
    print("Check:")
    print("  - Is the radio's PTT wire connected to RA-Board?")
    print("  - Is RA-Board power LED lit?")
    print("  - Try running as Administrator.")
