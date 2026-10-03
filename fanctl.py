#!/usr/bin/env python3
"""fanctl host side — read the hottest sensor, map it through a curve, send duty.

The ESP32 owns the 25 kHz PWM and the tach; this script owns the policy (curve,
floor, slew limit). Protocol and failsafes: see README.md.

  ./fanctl.py --port /dev/ttyACM0                  # run the control loop
  ./fanctl.py --port /dev/ttyACM0 --gpu            # + include the Tesla P40 temp
  ./fanctl.py --port /dev/ttyACM0 --sweep          # calibrate: stall duty, max rpm
  ./fanctl.py --port /dev/ttyACM0 --sweep --duties 100,50,25   # targeted A/B run
  ./fanctl.py --port /dev/null --temp-file /tmp/t # test the curve with no hardware

No dependencies beyond Python 3 stdlib + stty.
"""
import argparse
import glob
import os
import select
import statistics
import subprocess
import sys
import time

BAUD = 115200
DEFAULT_CURVE = [(40, 20), (55, 30), (65, 50), (75, 80), (85, 100)]
DEFAULT_DUTIES = "0,10,15,20,25,30,40,50,60,70,80,90,100"
QUIET_MS = 15.0
HEARTBEAT_S = 2.0  # must stay under the firmware's 5 s failsafe


def curve(points, t):
    """Piecewise-linear temp -> duty, clamped at both ends."""
    if t <= points[0][0]:
        return float(points[0][1])
    for (t0, d0), (t1, d1) in zip(points, points[1:]):
        if t <= t1:
            return d0 + (d1 - d0) * (t - t0) / (t1 - t0)
    return float(points[-1][1])


def slew(current, target, step):
    """Move toward target by at most step, so the fan doesn't hunt."""
    if target > current:
        return min(target, current + step)
    return max(target, current - step)


def parse_curve(spec):
    pts = []
    for pair in spec.split(","):
        t, d = pair.split(":")
        pts.append((float(t), float(d)))
    pts.sort()
    if len(pts) < 2:
        raise argparse.ArgumentTypeError("need at least two temp:duty points")
    return pts


def hottest_temp():
    """Hottest plausible sensor reading in Celsius, or None."""
    temps = []
    for pat in ("/sys/class/thermal/thermal_zone*/temp",
                "/sys/class/hwmon/hwmon*/temp*_input"):
        for path in glob.glob(pat):
            try:
                v = int(open(path).read()) / 1000.0
            except (OSError, ValueError):
                continue
            if -20 <= v <= 150:  # skip unpopulated/absurd hwmon entries
                temps.append(v)
    return max(temps) if temps else None


def gpu_temp():
    """Hottest NVIDIA GPU temp via nvidia-smi, or None if unavailable.

    The P40 exposes no hwmon entry, so this is the only way its temp reaches
    the curve. ~50 ms per call; one call per --interval is nothing.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        vals = [float(v) for v in out.stdout.replace(",", " ").split()]
        return max(vals) if vals else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def open_port(port):
    # -hupcl keeps DTR from dropping on close (would reset the ESP32).
    # clocal ignores carrier detect: the ESP32's USB CDC can report no
    # carrier until DTR is asserted, which silently starves reads (os.read
    # returns empty forever and everything looks like "no samples").
    subprocess.run(["stty", "-F", port, str(BAUD), "raw", "-echo", "-hupcl",
                    "clocal"], check=True)
    return os.open(port, os.O_RDWR | os.O_NOCTTY)


def pump(fd, buf, state, timeout=0.0):
    """Drain serial lines into state, return the leftover partial buffer."""
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        return buf
    buf += os.read(fd, 512)
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        text = line.decode(errors="replace").strip()
        if not text:
            continue
        state["rx"] = time.monotonic()
        if text.startswith("ALARM"):
            print(f"!! {text}", flush=True)
        elif text.startswith("RPM"):
            state["rpm"] = text
            rate = state.setdefault("samples", [])
            rate.append(int(text.split()[1]))
            del rate[:-64]  # bounded even when run() loops for weeks
    return buf


def read_temp(args):
    """Return (temp_c, source_tag). GPU wins only when it is the hotter one,
    so one blower serves the card without ignoring the CPUs it also feeds."""
    if args.temp_file:
        try:
            return float(open(args.temp_file).read()), " (sim)"
        except (OSError, ValueError):
            return None, " (sim)"
    t = hottest_temp()
    if args.gpu:
        g = gpu_temp()
        if g is not None and (t is None or g > t):
            return g, " (gpu)"
    return t, ""


def run(args):
    fd = open_port(args.port)
    pts = parse_curve(args.curve)
    buf, state = b"", {"rpm": "-", "rx": time.monotonic()}
    duty, next_send, warned = 100.0, 0.0, 0.0
    last_log, last_duty = 0.0, None

    while True:
        now = time.monotonic()
        if now >= next_send:
            next_send = now + args.interval
            temp, source = read_temp(args)
            if temp is None:
                print("no readable sensor, holding duty", flush=True)
            else:
                target = max(args.floor, curve(pts, temp))
                duty = slew(duty, target, args.slew * args.interval)
                os.write(fd, f"{int(round(duty))}\n".encode())
                d = int(round(duty))
                if last_duty is None or abs(d - last_duty) >= 1 \
                        or now - last_log >= args.log_every:
                    print(f"{time.strftime('%H:%M:%S')} temp={temp:5.1f}C "
                          f"duty={d:3d}% rpm={state['rpm']}{source}",
                          flush=True)
                    last_log, last_duty = now, d

        buf = pump(fd, buf, state, timeout=0.2)

        if now - state["rx"] > QUIET_MS and now - warned > 60:
            warned = now
            print(f"!! no serial data for {QUIET_MS:.0f}s — esp32 failsafe "
                  f"should have taken it to 100%", flush=True)


def sweep(args):
    """Stepped measurement. Reports the *median* rpm per duty: the tach line
    picks up switching noise, so a single sample lies but a median does not."""
    fd = open_port(args.port)
    buf, state = b"", {"rpm": "-", "rx": time.monotonic(), "samples": []}
    print(f"{'duty':>4} {'median':>6} {'mean':>6} {'min':>5} {'max':>5} "
          f"{'spread':>7} {'n':>3}")
    for d in (int(x) for x in args.duties.split(",")):
        os.write(fd, f"{d}\n".encode())
        beat, deadline = time.monotonic(), time.monotonic() + args.settle
        while time.monotonic() < deadline:
            buf = pump(fd, buf, state, 0.2)
            if time.monotonic() - beat > HEARTBEAT_S:  # keep the failsafe off
                beat = time.monotonic()
                os.write(fd, f"{d}\n".encode())
        state["samples"].clear()  # discard settling samples
        deadline = time.monotonic() + args.samples
        while time.monotonic() < deadline:
            buf = pump(fd, buf, state, 0.2)
            if time.monotonic() - beat > HEARTBEAT_S:
                beat = time.monotonic()
                os.write(fd, f"{d}\n".encode())
        s = state["samples"]
        if not s:
            print(f"{d:>4}   no samples")
            continue
        print(f"{d:>4} {statistics.median(s):>6.0f} {statistics.mean(s):>6.0f} "
              f"{min(s):>5} {max(s):>5} {max(s) - min(s):>7} {len(s):>3}",
              flush=True)
    os.write(fd, b"100\n")
    print("left at 100%; pick --floor above the lowest duty that spins")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between commands")
    p.add_argument("--curve", default=",".join(f"{t:g}:{d:g}" for t, d in DEFAULT_CURVE))
    p.add_argument("--floor", type=float, default=20, help="minimum duty %%")
    p.add_argument("--slew", type=float, default=5, help="max duty change per second")
    p.add_argument("--temp-file", help="read temp from a file instead of sensors")
    p.add_argument("--gpu", action="store_true",
                   help="also read the NVIDIA GPU temp (nvidia-smi); the hotter "
                        "of GPU and motherboard sensors drives the curve")
    p.add_argument("--sweep", action="store_true", help="calibration sweep, then exit")
    p.add_argument("--log-every", type=float, default=30.0,
                   help="seconds between status lines when duty is steady "
                        "(0 = log every interval; duty changes always log)")
    p.add_argument("--duties", default=DEFAULT_DUTIES, help="sweep steps, comma separated")
    p.add_argument("--settle", type=float, default=4.0, help="seconds to settle per step")
    p.add_argument("--samples", type=float, default=8.0, help="seconds of sampling per step")
    args = p.parse_args()
    try:
        sweep(args) if args.sweep else run(args)
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        print(f"serial error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"port not ready ({e}); retrying shortly", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
