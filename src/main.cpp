// fanctl firmware — 4-wire fan PWM + tach bridge over USB serial.
//
// Protocol (115200 8N1, one command per line):
//   host -> esp32   "<duty 0-100>\n"          any line containing an integer works
//   esp32 -> host   "RPM <n> DUTY <n>"        once per second
//                   "ALARM stall" / "ALARM failsafe"
//
// Failsafe: no valid duty command for FAILSAFE_MS -> 100%. A cooling fan has to
// fail loud, not silent.

#include <Arduino.h>

#ifndef PIN_PWM
#define PIN_PWM 25
#endif
#ifndef PIN_TACH
#define PIN_TACH 26
#endif

#ifndef TACH_MIN_LOW_US
#define TACH_MIN_LOW_US 200  // a real tach low phase is ~ms; coupled bursts are ns
#endif

static const uint32_t PWM_FREQ = 25000;  // 4-wire fan spec; do not change
static const uint8_t PWM_BITS = 8;       // 80MHz/25kHz = 3200 steps, 8 bits fits
static const uint8_t PWM_CHAN = 0;       // used on Arduino core 2.x only
static const uint8_t TACH_PPR = 2;       // pulses per revolution; verify with --sweep
static const uint16_t FAILSAFE_MS = 5000;
static const uint16_t WINDOW_MS = 1000;
static const uint8_t SPIN_DUTY = 20;     // below this the blower may stall
static const uint32_t STALL_MS = 3000;

// Tach input. The fan's open-collector output shares a ground return with
// 2.94 A of 25 kHz-switched motor current, so this line carries HF bursts —
// worst at narrow PWM duties (99% duty = 400 ns pulses = broadband noise).
// Counting raw falling edges reads several times the true RPM there.
// So: timestamp the edge, then credit it only once the line has *stayed* low
// for TACH_MIN_LOW_US. Spikes never survive that test; real pulses always do.
static volatile uint32_t fallUs = 0;
static volatile bool fallPending = false;
static uint32_t pulses = 0;  // main loop only, so not volatile

static void IRAM_ATTR onTachFall() {
  fallUs = micros();
  fallPending = true;
}

static void pollTach() {
  static bool credited = false;
  if (digitalRead(PIN_TACH) == HIGH) {
    credited = false;  // low phase is over, next one can be credited
    return;
  }
  uint32_t t;
  bool pending;
  noInterrupts();
  t = fallUs;
  pending = fallPending;
  interrupts();
  if (pending && !credited && (micros() - t) >= TACH_MIN_LOW_US) {
    credited = true;
    fallPending = false;
    pulses++;
  }
}

static uint8_t duty = 100;  // fail-safe default
static uint32_t lastCmdMs = 0, lastWindowMs = 0, stalledMs = 0, lastAlarmMs = 0;

static void writeOutput() {
  uint32_t scaled = (uint32_t)duty * 255 / 100;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(PIN_PWM, scaled);
#else
  ledcWrite(PWM_CHAN, scaled);
#endif
}

static void handleSerial() {
  static char buf[24];
  static uint8_t n = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c != '\n' && c != '\r') {
      if (n < sizeof(buf) - 1) buf[n++] = c;
      continue;
    }
    buf[n] = 0;
    n = 0;
    char *end;
    long v = strtol(buf, &end, 10);
    if (end == buf) continue;  // not a duty command, ignore
    if (v < 0) v = 0;
    if (v > 100) v = 100;
    duty = (uint8_t)v;
    lastCmdMs = millis();
  }
}

static void alarm(const char *what) {
  if (millis() - lastAlarmMs < 5000) return;  // don't spam the log
  lastAlarmMs = millis();
  Serial.print("ALARM ");
  Serial.println(what);
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_TACH, INPUT_PULLUP);
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PIN_PWM, PWM_FREQ, PWM_BITS);
#else
  ledcSetup(PWM_CHAN, PWM_FREQ, PWM_BITS);
  ledcAttachPin(PIN_PWM, PWM_CHAN);
#endif
  attachInterrupt(digitalPinToInterrupt(PIN_TACH), onTachFall, FALLING);
  lastCmdMs = millis();
  writeOutput();
  Serial.println("fanctl ready");
}

void loop() {
  handleSerial();
  pollTach();  // needs a ~1 ms loop period to catch a 3 ms low phase

  if (millis() - lastCmdMs > FAILSAFE_MS && duty != 100) {
    duty = 100;
    lastCmdMs = millis();
    alarm("failsafe: no command, duty=100");
  }
  writeOutput();

  if (millis() - lastWindowMs >= WINDOW_MS) {
    uint32_t p = pulses;
    pulses = 0;
    lastWindowMs = millis();

    uint32_t rpm = (uint32_t)((uint64_t)p * 60000ULL / TACH_PPR / WINDOW_MS);
    Serial.print("RPM ");
    Serial.print(rpm);
    Serial.print(" DUTY ");
    Serial.println(duty);

    // Spinning fan with no tach = dead fan, unplugged tach, or stalled rotor.
    if (duty >= SPIN_DUTY && rpm == 0) {
      stalledMs += WINDOW_MS;
      if (stalledMs >= STALL_MS) {
        stalledMs = 0;
        duty = 100;
        lastCmdMs = millis();
        alarm("stall");
      }
    } else {
      stalledMs = 0;
    }
  }
  delay(1);
}
