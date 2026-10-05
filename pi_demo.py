#!/usr/bin/env python3
"""
Topside proof-of-concept demo (runs on the Raspberry Pi).

  keyboard arrow keys --> Pi --> Fathom-S topside --> tether --> Fathom-S ROV --> Arduino
  monitor (this screen) <-- Pi <-- Fathom-S topside <-- tether <-- Fathom-S ROV <-- Arduino <-- sensor

Run on the Pi, in a terminal that is showing on the monitor (no pip install needed;
pyserial is used if present, otherwise a built-in fallback takes over):

    python3 pi_demo.py                      # auto-detects the Fathom-S USB serial port
    python3 pi_demo.py --port /dev/ttyUSB0  # or name it yourself
    python3 pi_demo.py --sim                # no hardware: fake ROV, for practicing the UI

Keys:  arrow keys = send a command     p = ping the ROV     q = quit

Wire protocol (plain text, one message per line, 115200 baud):
    Pi  -> ROV :  CMD:U | CMD:D | CMD:L | CMD:R      PING
    ROV -> Pi  :  ACK:<letter>                       PONG
                  SENSOR:<id>:<distance_cm>          (sent by the Arduino every 0.5 s)
"""
import argparse
import collections
import curses
import glob
import random
import threading
import time

BAUD = 115200
KEYMAP = {
    curses.KEY_UP: "U",
    curses.KEY_DOWN: "D",
    curses.KEY_LEFT: "L",
    curses.KEY_RIGHT: "R",
}
LABEL = {"U": "UP", "D": "DOWN", "L": "LEFT", "R": "RIGHT"}


# ---------------------------------------------------------------- serial link
class FakeRov:
    """Stand-in for the serial port so the UI can be practiced with no hardware."""

    def __init__(self):
        self._lines = collections.deque()
        self._lock = threading.Lock()
        self._last_sensor = time.time()
        self.is_open = True

    def write(self, data):
        for raw in data.decode().splitlines():
            if raw.startswith("CMD:"):
                self._push("ACK:" + raw[4:5])
            elif raw == "PING":
                self._push("PONG")

    def _push(self, line):
        with self._lock:
            self._lines.append((line + "\n").encode())

    def readline(self):
        time.sleep(0.05)
        if time.time() - self._last_sensor > 0.5:
            self._last_sensor = time.time()
            self._push("SENSOR:1:%.1f" % random.uniform(10, 60))
        with self._lock:
            return self._lines.popleft() if self._lines else b""

    def close(self):
        self.is_open = False


class PlainSerial:
    """Tiny serial port built on Python's standard library (Linux/Raspberry Pi only).

    Used automatically when pyserial is not installed, so no `pip install` is needed.
    """

    def __init__(self, port, baud):
        import os
        import termios

        self.port = port
        self._os = os
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        attrs = termios.tcgetattr(self._fd)
        speed = getattr(termios, "B%d" % baud)
        attrs[0] = 0                                             # iflag: no input processing
        attrs[1] = 0                                             # oflag: no output processing
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # 8 data bits, no parity
        attrs[3] = 0                                             # lflag: raw
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 1                              # read() waits up to 0.1 s
        termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
        self._termios = termios
        self._buf = b""

    def write(self, data):
        self._os.write(self._fd, data)

    def readline(self):
        deadline = time.time() + 0.1
        while b"\n" not in self._buf and time.time() < deadline:
            self._buf += self._os.read(self._fd, 256)
        if b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            return line + b"\n"
        return b""

    def reset_input_buffer(self):
        self._termios.tcflush(self._fd, self._termios.TCIFLUSH)
        self._buf = b""

    def close(self):
        self._os.close(self._fd)


def find_port():
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return ports[0] if ports else None


def open_link(port, sim):
    if sim:
        return FakeRov()

    port = port or find_port()
    if not port:
        raise SystemExit(
            "No USB serial port found. Is the Fathom-S topside board plugged into the Pi?\n"
            "Check with:  ls /dev/ttyUSB* /dev/ttyACM*"
        )
    try:
        import serial  # pyserial, if it happens to be installed
        ser = serial.Serial(port, BAUD, timeout=0.1)
    except ImportError:
        ser = PlainSerial(port, BAUD)  # no install needed
    # The Fathom-S topside board can reset the Arduino when the port opens
    # (RTS-reset jumper). Give the Arduino time to boot before we send anything.
    time.sleep(2.0)
    ser.reset_input_buffer()
    return ser


# ---------------------------------------------------------------- shared state
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.tx = collections.deque(maxlen=8)
        self.rx = collections.deque(maxlen=8)
        self.sensor_id = None
        self.sensor_cm = None
        self.last_rx_time = None
        self.running = True

    def log_tx(self, msg):
        with self.lock:
            self.tx.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))

    def log_rx(self, msg):
        with self.lock:
            self.last_rx_time = time.time()
            self.rx.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))


def reader(link, state):
    """Background thread: read lines coming back up the tether."""
    while state.running:
        try:
            raw = link.readline()
        except Exception as exc:  # port unplugged, etc.
            state.log_rx("serial error: %s" % exc)
            time.sleep(0.5)
            continue
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        if line.startswith("SENSOR:"):
            parts = line.split(":")
            with state.lock:
                state.sensor_id = parts[1] if len(parts) > 1 else "?"
                state.sensor_cm = parts[2] if len(parts) > 2 else "--"
                state.last_rx_time = time.time()
        else:
            state.log_rx(line)


def send(link, state, text):
    link.write((text + "\n").encode())
    state.log_tx(text)


# ---------------------------------------------------------------- screen
def draw(stdscr, state, port_name):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    bold = curses.A_BOLD

    stdscr.addstr(0, 0, " ROV PROOF OF CONCEPT  -  topside console ".center(w - 1), curses.A_REVERSE)
    stdscr.addstr(1, 0, ("link: %s @ %d baud" % (port_name, BAUD))[: w - 1])

    with state.lock:
        age = None if state.last_rx_time is None else time.time() - state.last_rx_time
        sid, cm = state.sensor_id, state.sensor_cm
        tx, rx = list(state.tx), list(state.rx)

    if age is None:
        status, attr = "WAITING FOR ROV...", curses.A_DIM
    elif age < 2:
        status, attr = "ROV LINK: OK", bold
    else:
        status, attr = "ROV LINK: LOST (%.0fs)" % age, curses.A_BLINK
    stdscr.addstr(2, 0, status, attr)

    stdscr.addstr(4, 0, "SENSOR RECEIVING:", bold)
    stdscr.addstr(5, 2, ("#%s" % sid) if sid else "#-", bold)
    stdscr.addstr(6, 2, ("distance: %s cm" % cm) if cm else "distance: --")

    stdscr.addstr(8, 0, "SENT TO ROV (keyboard -> tether):", bold)
    for i, line in enumerate(tx[-4:]):
        stdscr.addstr(9 + i, 2, line[: w - 3])
    stdscr.addstr(14, 0, "RECEIVED FROM ROV (tether -> screen):", bold)
    for i, line in enumerate(rx[-4:]):
        stdscr.addstr(15 + i, 2, line[: w - 3])

    stdscr.addstr(min(h - 1, 20), 0, "arrows = send command   p = ping   q = quit"[: w - 1], curses.A_DIM)
    stdscr.refresh()


def run(stdscr, link, port_name):
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(50)  # getch waits 50 ms, so the screen keeps refreshing
    state = State()
    t = threading.Thread(target=reader, args=(link, state), daemon=True)
    t.start()
    try:
        while True:
            draw(stdscr, state, port_name)
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                break
            if key in KEYMAP:
                send(link, state, "CMD:" + KEYMAP[key])
            elif key in (ord("p"), ord("P")):
                send(link, state, "PING")
    finally:
        state.running = False
        t.join(timeout=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. /dev/ttyUSB0 (default: auto-detect)")
    ap.add_argument("--sim", action="store_true", help="use a fake ROV instead of real hardware")
    args = ap.parse_args()

    link = open_link(args.port, args.sim)
    name = "SIMULATED" if args.sim else getattr(link, "port", "?")
    try:
        curses.wrapper(run, link, name)
    finally:
        link.close()


if __name__ == "__main__":
    main()
