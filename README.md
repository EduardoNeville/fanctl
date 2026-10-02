# fanctl

Drive a 4-wire 12 V fan from an Intel server with an ESP32 as the PWM/tach bridge.

**Split of responsibilities:** the ESP32 owns the 25 kHz PWM and the tach, and
fails safe to 100% if the server stops talking. The server owns the policy —
which sensor to watch, the curve, the floor, the slew limit. All of that is
`fanctl.py`; if it dies, the fan goes loud, not quiet.

Firmware lives in `src/main.cpp` (`pio run -t upload`); the host side is a single
stdlib-only Python file.

## Run it

```sh
uv run fanctl --port /dev/ttyACM0               # uv project, entry point defined
uv run fanctl --port /dev/ttyACM0 --sweep       # calibrate this fan
./fanctl.py --port /dev/ttyACM0                 # or run it directly — no deps
python3 test_fanctl.py                          # curve + slew self-check
```

No dependencies beyond the Python 3 standard library and a not-brain-dead libc
(the script shells out to `stty` to configure the port).

```
        PSU (all-black harness)          FAN  Delta BFB1012EH           ESP32-C3
        ───────────────────────          ────────────────────           ────────

   (1)  24-pin PS_ON# ●──┐
          24-pin GND   ●──┘   one jumper wire → the PSU is dead without it

   (2)  8-pin GND ●─────────────────► pin 1 BLACK (GND) ──────┬───► GND
                                                              │
   (3)  8-pin +12V ●──[ 5 A FUSE ]──► pin 2 RED (+12 V)        │
                                                              │
   (4)                               pin 3 YELLOW (TACH) ─[1 kΩ]─┬──► GPIO 5
                                                               └──[10 kΩ]──► 3V3
   (5)                               pin 4 BLUE  (PWM) ──[220 Ω]────► GPIO 4

   (6)  USB ●─────────────────────────────────────────────────────► server USB port
                                                         5 V power + /dev/ttyACM0 control

   (7)  100 µF / 25 V from pin 2 (+) to pin 1 (−) — optional, only if the blower
        stutters on start-up
```

That is the entire circuit — **five nets** (`+12V`, `GND`, `PWM`, `TACH`, plus the
pull-up), and nothing else is connected to anything. The five nets, exactly:

```
+12V  : PSU 8-pin +12 V ──[5 A fuse]── fan pin 2 RED
GND   : PSU 8-pin GND ──┬── fan pin 1 BLACK
                        └── ESP32 GND pin                    ← shared reference, non-negotiable
PWM   : ESP32 GPIO 4 ──[220 Ω]── fan pin 4 BLUE
TACH  : ESP32 GPIO 5 ──[1 kΩ]── fan pin 3 YELLOW
PULLUP: ESP32 GPIO 5 ──[10 kΩ]── ESP32 3V3                   ← GPIO side of the 1 kΩ, not the fan side
```

The `GND` net has three ends on it (PSU, fan, ESP32) — join them at one point
rather than chaining, so the fan's 2.94 A of return current does not flow
through the wire that the ESP32 uses as its reference.

## Connection by connection

Do these in this order. Each step has one thing to observe, so a failure points
at the wire you just added and nothing else.

| # | Connection | Through | Observe before moving on |
|---|---|---|---|
| 1 | PSU 24-pin **PS_ON#** → PSU 24-pin **GND** | one jumper wire | PSU fan spins up. Measure **12.0–12.6 V** across two pins of the 8-pin |
| 2 | PSU **GND** → fan **BLACK** (pin 1) | nothing, thick wire | — |
| 3 | fan **BLACK** (pin 1) → ESP32 **GND** pin | nothing | continuity/beeper between the two GND ends |
| 4 | PSU **+12 V** → **fuse** → fan **RED** (pin 2) | 5 A slow-blow | **Fan roars at full speed.** A floating PWM input defaults to 100%. `RPM 0` still — correct, no tach yet |
| 5 | fan **YELLOW** (pin 3) → **1 kΩ** → ESP32 pin labelled **5** | 1 kΩ in series | `RPM` jumps to thousands |
| 6 | ESP32 pin **5** → **10 kΩ** → ESP32 **3V3** pin | 10 kΩ | pull-up only; RPM unchanged |
| 7 | fan **BLUE** (pin 4) → **220 Ω** → ESP32 pin labelled **4** | 220 Ω in series | `--sweep` changes both the sound and the RPM |
| 8 | ESP32 **USB** → server USB port | USB cable | `fanctl ready` on `/dev/ttyACM0` |

The resistors are in-line on the wire (solder mid-cable, heat-shrink) — they are
protection, not tuning. The 10 kΩ goes to **3V3**, never to 12 V: the tach line
is the fan's open-collector output, and pulling it to 12 V puts 12 V on GPIO 5.

## Verify every node with a meter

A DMM averages the 25 kHz PWM, so you can check each signal with no scope:

| Probe red → | Probe black → | Reading | Meaning |
|---|---|---|---|
| ESP32 pin **4** | ESP32 **GND** | ~**3.3 V** at duty 100, ~1.6 V at 50, 0 V at 0 | PWM present, firmware driving |
| ESP32 pin **5** | ESP32 **GND** | fan stopped: solid **3.3 V**; fan spinning: **~2 V** | pulses arriving; 3.3 V solid = no tach |
| fuse output | PSU **GND** | 12.0–12.6 V | fuse intact (0 V = blown) |
| fan pin 1 | ESP32 **GND** | beep / 0 Ω | shared reference exists |
| PSU 8-pin pin A | PSU 8-pin pin B | 0 V or 12 V | either reading is safe — 0 V just means same rail |

## Never do these

- **12 V to the ESP32.** Not to `5V`, not to `3V3`, not to any GPIO. The `5V` pin
takes USB power only — nothing from the PSU, ever.
- **Pull the tach up to 12 V.** 3V3 only.
- **PWM the 12 V wire.** Modulate the BLUE wire only; the fan has its own
controller and the red wire is not dimmable.
- **Swap RED and BLACK**, omit the fuse, or open the PSU (bulk caps hold ~400 V).
- **Guess a pin.** Any pin of the 8-pin CPU/PCIe connector is 12 V or GND, so a
wrong guess there is harmless — but pins 3 and 4 of the fan are not, and neither
is any pin of the 24-pin.

**The fuse is not optional on a 1000 W supply.** That 12 V rail can push ~80 A
into a dead short; 22 AWG wire in that path stops being a wire and becomes a
glowing element. 5 A slow-blow, in line, as close to the PSU as possible.

## BOM

ESP32 board · 12 V ≥5 A supply (inrush of this blower is ~2× steady state) ·
5 A fuse + holder · 100 µF/25 V electrolytic · 220 Ω, 1 k, 10 k resistors · wire.

Using the Aerolux 1000 W instead: jumper **PS_ON# (green, 24-pin) to any black
GND**, and park *some* load on 5 V (a spare fan or a resistor) or cheap
group-regulated units cycle. It is 30× oversized for this, and loud.

## Build order

**0. Bench only.** Fan on the 12 V supply, no ESP32, no server. Confirm it spins
and that the tach pin toggles between ~0 V and a few volts. Fix the plumbing
before the software can confuse you.

**Connect in this order, so each step is separately testable.** The firmware's
stall alarm forces 100% whenever duty ≥ 20 with no tach pulses, which is why the
TACH wire goes on before the PWM wire — until PWM exists, that failsafe holds
the fan at full speed, which is exactly the condition for measuring its max RPM:

| Step | Wires added | What you must observe |
|---|---|---|
| 1 | none — USB only | `RPM 0 DUTY 100` once a second. Meter on GPIO 4→GND averages 3.3 V at duty 100, 0 V at 0 |
| 2 | GND + fuse + 12 V | Fan roars at full speed (a floating PWM input defaults to 100%). Meter pin 2→1: 12.0–12.6 V. `RPM 0` still — expected, no tach yet |
| 3 | TACH (+ 10 k to 3V3) | `RPM` jumps to thousands. This is your max-RPM number |
| 4 | PWM (220 Ω to GPIO 4) | `--sweep` changes both the sound and the reported RPM |
| 5 | — | Kill `fanctl.py`: fan must go to 100% within 5 s |

**1. Flash and talk to it.** Board detected as **ESP32-C3** (QFN32, 4 MB flash,
native USB-Serial/JTAG, `/dev/ttyACM0`, VID:PID `303a:1001`). `platformio.ini`
is already set up for it:
```sh
pipx install platformio
pio run                 # ~3 min first time, downloads the RISC-V toolchain
pio run -t upload       # esptool over USB-JTAG; no BOOT-button dance needed
pio device monitor      # then type: 50
```
Expect `RPM <n> DUTY 50` once a second. Sanity-check `<n>` against the fan's
spec RPM — if yours reads roughly 2× the spec, set `TACH_PPR = 1` in
`src/main.cpp` and reflash.

C3 pin rules, if you ever change the wiring: GPIO 25/26 **do not exist** on this
chip, GPIO 18/19 are USB D-/D+ (using them kills the serial link), and GPIO 2/8/9
are strapping pins. PWM = GPIO 4, TACH = GPIO 5 are free of all three.
`-DARDUINO_USB_CDC_ON_BOOT=1` is required or `Serial` prints to UART0 and the
board looks dead.

**2. Calibrate the fan.** Physical fans do not match the datasheet:
```sh
./fanctl.py --sweep --port /dev/serial/by-id/usb-...
./fanctl.py --sweep --duties 100,99,75,50,25,0 --settle 4 --samples 6   # targeted
```
The sweep sends a heartbeat every 2 s so the firmware watchdog never fires
mid-measurement, and reports the **median** rather than a single sample. Note the
lowest duty that keeps it spinning (probably 15–25%) and the max RPM. Set
`--floor` above the spin threshold. This is the number a minimal model cannot
guess.

**3. Run the loop.** Check the policy with no hardware first:
```sh
echo 70 | sudo tee /tmp/t && ./fanctl.py --port /dev/null --temp-file /tmp/t
```
then for real, tuning the curve to whatever this fan actually cools:
```sh
./fanctl.py --port /dev/serial/by-id/usb-... --floor 20 \
  --curve 40:20,55:30,65:50,75:80,85:100
python3 test_fanctl.py          # curve + slew-limit self-check
```

**4. Install it.**
```sh
sudo usermod -aG dialout "$USER"          # serial access
sudo systemctl mask ModemManager          # else it grabs ttyUSB0 on Debian/Ubuntu
```
`/etc/systemd/system/fanctl.service`:
```ini
[Unit]
Description=fanctl 4-wire fan controller
After=multi-user.target

[Service]
ExecStart=/usr/bin/python3 /opt/fanctl/fanctl.py --port /dev/serial/by-id/usb-XXX --floor 20
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

**5. Prove the failsafes** — this is the acceptance test, not optional:
- `kill` the script while the fan runs → fan must jump to 100% within 5 s (ESP32 watchdog).
- Unplug the USB cable → same, and it stays at 100%.
- Block the rotor (carefully) at duty ≥20 for 3 s → `ALARM stall` and 100%.

## Identifying the fan's 4 pins

Two colour conventions collide in this build — this is the trap:

| | black | red | yellow | blue | green |
|---|---|---|---|---|---|
| **PSU output cables** (ATX) | GND | +5 V | **+12 V** | −12 V | PS_ON# |
| **Intel 4-wire fan** | GND | — | **+12 V** | PWM | tach |
| **Delta 4-wire fan** | GND | **+12 V** | tach | PWM | — |

**Confirmed on this build:** fan leads are **black / red / yellow / blue** = GND,
+12 V, **TACH**, PWM. No green lead, so it is the Delta convention — and the
yellow wire is the tach, *not* 12 V. Same colour that means +12 V on the PSU cables.

So a yellow wire means +12 V on the PSU cable and *tach* on a Delta fan lead.
Never transfer a colour reading from one side to the other.

The reliable anchors, in order of trust:

1. **Pin order is fixed** (Intel 4-Wire PWM Fan Spec rev 1.2): 1 GND, 2 +12 V,
   3 TACH, 4 PWM. No exceptions on any PC fan.
2. **Wire order in the housing equals pin order**, and **GND is black in both
   conventions** — the black wire is pin 1, the wire beside it is pin 2.
3. **PWM is always the outermost wire (pin 4)** — deliberate, so a 4-wire fan
   can plug into a 3-pin header with the PWM lead hanging off the end. If the
   wire you think is PWM is not the end one, you have it backwards.
4. Tie-breaker with a meter: pins 1↔2 show a finite, drifting resistance (the
   fan's input capacitance charging, tens of kΩ+). Tach↔GND looks different.
5. Still unsure: read the **full** part number off the fan label
   (`BFB1012EH-XXXX`, the suffix matters — the family ships as 2-, 3- and
   4-wire) and look up that exact variant's datasheet.

What a mis-wire costs: swapping **3 and 4** is loud but harmless — a floating
PWM input defaults to full speed, and the tach pin is open-collector so it can't
be back-driven. Putting **12 V on 3 or 4** kills the fan controller and the
GPIO. Putting **12 V on the ESP32's 5 V/3V3 pin or any GPIO** kills the board.
Spend your verification effort on pins 1 and 2, and on never letting 12 V near
the ESP32.

## Powering from the Aerolux 1000 W

1. **Check the input voltage.** The LUX 1000 W is a 200–240 Vac model. On
   127 V mains it will never start, and you will spend an hour debugging firmware.
2. **Enable it:** jumper **PS_ON# (green)** in the 24-pin to **any black GND**.
   Nothing happens until you do — no standby, no fan, nothing.
3. **Measure before connecting anything:** 12.0–12.6 V between yellow and black
   at whatever connector you plan to tap.
4. **Tap point:** a CPU/EPS 8-pin or PCIe 8-pin lead is the best source — thick
   wires, latched, all yellow (+12 V) and black (GND). Molex/SATA also work.
   Do not tap the 24-pin for the fan's current path.
5. **Fuse it: 5 A in line, near the PSU.** See the warning above.
6. **First light on a small supply.** If you have any 12 V ≥3 A brick, use it for
   the fan's first spin. Reserve the 1000 W unit for the final configuration —
   it adds an always-on 12 cm fan and ~80 A of fault current to a 35 W load.

### When every wire is black

The LUX harness is all black — no colour coding to read, so identify rails
electrically. **Never open the PSU to look:** the bulk capacitors hold ~400 V and
stay lethal for a long time after unplugging.

Use the **shape** of the connectors first:

- **CPU 8-pin (4+4) or PCIe 8-pin (6+2): every pin is either +12 V or GND.** Nothing
  else is possible. So *any* two pins on that connector are safe to test: same
  rail gives 0 V and nothing happens, different rails give 12 V and the fan runs.
  There is no wrong pairing, which makes it the correct tap point for the fan.
- **24-pin:** the only messy one. You need it solely for PS_ON#.

Finding PS_ON# on an all-black 24-pin, with the PSU plugged in but not enabled:

1. Clip the meter's black probe to one pin, then touch the red probe to each
   other pin (DC volts). Repeat with a different reference pin until you find a
   reference where **exactly two pins read ~5 V and the rest read 0 V**. That
   reference is a GND pin; the two live ones are **+5VSB and PS_ON#** (PS_ON# is
   pulled up to the standby rail internally, so an unloaded meter cannot tell
   them apart).
2. Tell them apart with a **1 kΩ resistor from each to GND for a second**:
   +5VSB holds ~5 V (it is a supply), PS_ON# collapses to <1 V (it is a weak
   pull-up). 1 kΩ on 5 VSB draws 5 mA — harmless.
3. **Jumper the PS_ON# pin to a GND pin.** The PSU starts and its own fan spins.
   This is self-verifying: no start = wrong pin, and no damage done.
4. Confirm with the meter: **12.0–12.6 V** between pins of the CPU/PCIe connector.

Alternative to all of the above: a cheap **ATX PSU tester or 24-pin breakout
board** plugs onto the 24-pin, bridges PS_ON# internally, and labels every rail.

## Measured calibration

Delta BFB1012EH on 12 V from the Aerolux, `TACH_PPR = 2`, median of 5–6 samples
per step via `--sweep`. Rated speed is 5300 RPM; we read 4275 at 100% duty — the
gap is supply voltage and lead drop, not a tach error.

| duty | 0 | 10 | 15 | 20 | 25 | 50 | 75 | 90 | 99 | 100 |
|---|---|---|---|---|---|---|---|---|---|---|
| RPM | 0 | **0\*** | 510 | 840 | 1110 | 2190 | 2760 | 3060 | 3180 | **4275** |

\* 10% duty will **not** spin this blower up from a standstill; 15% does. Keep
`--floor` at 20 or above. 0% duty does stop it.

Two shapes worth knowing before you write a curve:

- **A cliff at 99%.** 99% duty means 400 ns low pulses at 25 kHz, which the fan's
  internal PWM detector under-reads: 3180 RPM at 99% versus 4275 at 100%. The top
  of the range is not linear, so treat 100% as a distinct *turbo* step — which is
exactly what the failsafe uses.
- **The middle is flat.** 50% → 90% duty buys only 2190 → 3060 RPM (+40%). Use
  25–75% as the quiet band and 100% as the only real cooling step.

Verified: cold start from 0 RPM at every step, 0% stops the fan, and cutting the
serial link ramps 1110 → 4350 RPM through the firmware watchdog with no host
alive.

## Troubleshooting: `RPM 0` everywhere

The fan either isn't spinning, or it is spinning with TACH not reaching GPIO 5.
Discriminate by ear (or hand over the exhaust) — the fan is loud when it runs.

| Fan state at duty 100 | Cause | Check |
|---|---|---|
| Silent | No 12 V | Meter pin 2→1: expect 12.0–12.6 V. PS_ON# jumpered? PSU switched on? |
| Silent, 12 V present | Dead fan or no PWM reference | Swap the fan on the same supply |
| Roaring at full speed | PWM not reaching GPIO 4 (floating PWM input = full speed) | Meter GPIO 4→GND: ~3.3 V at duty 100, ~1.6 V at 50, 0 V at 0. A DMM averages 25 kHz |
| Spinning, `RPM 0` | TACH not reaching GPIO 5 | 10 k pull-up to 3V3 present? Meter GPIO 5→GND: solid 3.3 V = no pulses |

The firmware's `ALARM stall` + auto-100% covering for a missing tach is expected
behaviour, not a bug — that is the failsafe doing its job.

## Notes and gotchas

- **Never PWM the 12 V rail** — the fan has its own controller; modulate only the
  PWM wire. **Never connect the ESP32 to 12 V**, and never bridge this fan's
  12 V to a motherboard header pin.
- TACH is open-collector: pull it up to **3V3**, not 12 V. The internal pull-up
  works but is weak at high RPM; the external 10 k is there for cleanliness.
- Some fans ignore 3.3 V PWM. If yours does, add a BSS138 level shifter or a
  2N7002 as an open-drain driver — 4 extra parts, no rewrites.
- Opening the serial port may reset the ESP32. Harmless: it boots at 100% duty,
  and `stty -hupcl` in `fanctl.py` stops a *close* from doing the same.
- Don't mount the ESP32 in the blower's exhaust path. This fan at 100% sounds
  like a hair dryer; that is the point of the curve.
- **The tach line depends on the firmware's min-low-width filter.** Raw edge
  counting read 1.8–4.9× the true RPM at intermediate duties: the fan's
  open-collector output shares a return path with 2.94 A of 25 kHz-switched motor
  current, and narrow PWM pulses are broadband. Real low phases are ~ms, coupled
  bursts are sub-µs, so an edge counts only after the line *stays* low for
  `TACH_MIN_LOW_US` (200 µs, override with `-DTACH_MIN_LOW_US=…`). If you ever see
  RPM that is not monotonic in duty, suspect this filter, not the fan — and
  confirm with `--sweep`, where a healthy tach shows a spread of ~60 RPM.
- `fancontrol`/lm-sensors cannot drive the headers on an Intel S2600-class
  board — the BMC owns them. Hence this whole setup.
