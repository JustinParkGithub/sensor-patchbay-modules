// 4-way bend sensor on an Arduino Nano.
//
// Reads four flex/bend sensors wired to analog pins A2-A5 and streams one
// whitespace-separated line of four 10-bit ADC readings (0-1023) per sample:
//
//     raw2    raw3    raw4    raw5\n
//
// Pairs with sources/bend4/ in the sensor-patchbay repo. Each sensor
// is a variable resistor (flex sensor); wire it as a voltage divider into its
// analog pin. Order on the wire is A2, A3, A4, A5 -> bend.0, bend.1, bend.2,
// bend.3 in the plugin.

// const int SENSOR_PIN0 = A0;          // The analog input pin we are reading
const int SENSOR_PIN2 = A2;
const int SENSOR_PIN3 = A3;
const int SENSOR_PIN4 = A4;
const int SENSOR_PIN5 = A5;
const unsigned long DELAY_MS = 200; // Time between readings (milliseconds)

void setup() {
  Serial.begin(9600);               // Start serial communication at 9600 baud
  // Analog input pins do not need pinMode(); they default to input.
}

void loop() {
  // int raw0 = analogRead(SENSOR_PIN0);       // 0-1023 (10-bit ADC)
  int raw2 = analogRead(SENSOR_PIN2);
  int raw3 = analogRead(SENSOR_PIN3);
  int raw4 = analogRead(SENSOR_PIN4);
  int raw5 = analogRead(SENSOR_PIN5);

  // Serial.print("Raw: ");
  // Serial.print(raw0 );
  // Serial.print("    ");
  Serial.print(raw2 );
  Serial.print("    ");
  Serial.print(raw3 );
  Serial.print("    ");
  Serial.print(raw4 );
  Serial.print("    ");
  Serial.println(raw5 );

  delay(DELAY_MS);
}
