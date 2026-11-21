// 4-way bend sensor + pushbutton on an Arduino Nano RP2040 Connect.
//
// Streams one line per sample: four whitespace-separated 10-bit ADC readings
// (0-1023), in the same format as the original bend4 sketch:
//
//     raw0    raw1    raw2    raw3\n
//
// Also turns the onboard LED on while a pushbutton on D2 is pressed.
//
// Wiring (everything is 3.3V logic, NOT 5V tolerant):
//  - Each flex sensor is a voltage divider: 3.3V -> flex sensor -> analog pin
//    -> ~10K resistor -> GND
//  - Pushbutton: one side to 3.3V, other side to D2, with a 10K pull-down
//    resistor from D2 to GND

// --- Pins ---
const int SENSOR_PIN0 = A2;
const int SENSOR_PIN1 = A3;
const int SENSOR_PIN2 = A4;
const int SENSOR_PIN3 = A5;
const int BUTTON_PIN  = 2;            // D2
const int LED_PIN     = LED_BUILTIN;  // D13, the onboard LED

// --- Timing ---
const unsigned long SAMPLE_INTERVAL_MS = 200;  // time between sensor reports
unsigned long lastSampleMs = 0;

// --- Options ---
// If true, appends the button state (0 or 1) as a 5th column. Leave it false
// if the program reading the stream expects exactly four values per line.
const bool STREAM_BUTTON = true;

void setup() {
  Serial.begin(9600);
  while (!Serial && millis() < 3000) {
    ;  // wait up to 3 s for the USB serial connection
  }

  pinMode(LED_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT);  // external 10K pull-down
  // Analog input pins do not need pinMode().
}

void loop() {
  // 1) Button: checked on every pass through loop(), so it feels instant.
  int buttonState = digitalRead(BUTTON_PIN);
  digitalWrite(LED_PIN, buttonState == HIGH ? HIGH : LOW);

  // 2) Sensors: only read and report every SAMPLE_INTERVAL_MS.
  unsigned long now = millis();
  if (now - lastSampleMs >= SAMPLE_INTERVAL_MS) {
    lastSampleMs = now;

    int raw0 = analogRead(SENSOR_PIN0);
    int raw1 = analogRead(SENSOR_PIN1);
    int raw2 = analogRead(SENSOR_PIN2);
    int raw3 = analogRead(SENSOR_PIN3);

    const char* buttonText = (buttonState == HIGH) ? "PRESSED" : "RELEASED";

    Serial.print(raw0);
    Serial.print("    ");
    Serial.print(raw1);
    Serial.print("    ");
    Serial.print(raw2);
    Serial.print("    ");
    if (STREAM_BUTTON) {
      Serial.print(raw3);
      Serial.print("    ");
      Serial.println(buttonText);
    } else {
      Serial.println(raw3);
    }
  }
}