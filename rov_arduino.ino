/*
  ROV-side proof of concept  (Arduino UNO R4 WiFi - the board with the 12x8 LED matrix)

  Wiring
    Fathom-S ROV board  ->  Arduino
      TX  -> pin 0 (RX)      <- Blue Robotics' Arduino guide wires these straight through
      RX  -> pin 1 (TX)         (RX to RX, TX to TX). If nothing arrives, swap the two.
      5V  -> 5V   (or the breadboard 5V rail)
      GND -> GND  (grounds MUST be common)
    HC-SR04 ultrasonic sensor
      VCC -> 5V    Trig -> pin 9    Echo -> pin 10    GND -> GND

  Messages (text, one per line, 115200 baud, on the hardware UART "Serial1" = pins 0/1)
    from Pi:  CMD:U / CMD:D / CMD:L / CMD:R   -> show that letter on the LED matrix, reply ACK:<letter>
              PING                            -> reply PONG
    to Pi:    SENSOR:1:<cm>   every 500 ms    (SENSOR:1:-- if the sensor sees nothing)

  Libraries: ArduinoGraphics + Arduino_LED_Matrix (both ship with the UNO R4 board package;
  Tools > Manage Libraries > "ArduinoGraphics" if the compiler can't find it).

  If your board is NOT an R4 WiFi (no LED matrix), delete the matrix lines; everything else works.
*/
#include "ArduinoGraphics.h"
#include "Arduino_LED_Matrix.h"

ArduinoLEDMatrix matrix;

const int TRIG_PIN = 9;
const int ECHO_PIN = 10;
const int SENSOR_ID = 1;
const unsigned long REPORT_MS = 500;

String line = "";
unsigned long lastReport = 0;

void showChar(char c) {
  matrix.beginDraw();
  matrix.clear();
  matrix.stroke(0xFFFFFFFF);
  matrix.textFont(Font_5x7);
  matrix.beginText(3, 0, 0xFFFFFFFF);   // 5x7 glyph, roughly centered on 12x8
  matrix.print(c);
  matrix.endText();
  matrix.endDraw();
}

// returns distance in cm, or -1 if no echo
float readDistanceCm() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  unsigned long us = pulseIn(ECHO_PIN, HIGH, 30000UL);  // 30 ms timeout (~5 m)
  if (us == 0) return -1;
  return us * 0.0343 / 2.0;
}

void handleLine(const String &msg) {
  if (msg.startsWith("CMD:") && msg.length() >= 5) {
    char c = msg.charAt(4);
    showChar(c);                       // proof the command made it down the tether
    Serial1.print("ACK:");
    Serial1.println(c);                // proof the reply can come back up it
  } else if (msg == "PING") {
    Serial1.println("PONG");
  }
}

void setup() {
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  Serial1.begin(115200);               // to the Fathom-S ROV board
  matrix.begin();
  showChar('*');                       // boot indicator
}

void loop() {
  while (Serial1.available()) {
    char ch = Serial1.read();
    if (ch == '\n') {
      line.trim();
      if (line.length()) handleLine(line);
      line = "";
    } else if (line.length() < 40) {
      line += ch;
    }
  }

  if (millis() - lastReport >= REPORT_MS) {
    lastReport = millis();
    float cm = readDistanceCm();
    Serial1.print("SENSOR:");
    Serial1.print(SENSOR_ID);
    Serial1.print(":");
    if (cm < 0) Serial1.println("--");
    else Serial1.println(cm, 1);
  }
}
