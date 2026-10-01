"""Живой скаттерплот данных Pico. Python 3.10+."""
# Настройки пользователя. Linux: /dev/ttyACM0; Windows: COM3.
PORT = "/dev/ttyACM0"
BAUDRATE = 115200             # Для USB CDC это параметр порта, не скорость USB.
MAX_POINTS = 2000
UPDATE_MS = 50
RECONNECT_SECONDS = 2.0
SILENCE_SECONDS = 5.0

import argparse
from collections import deque
from dataclasses import dataclass
import threading
import time

import serial
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation


class LineDecoder:
    """Сохраняет незавершенные строки между чтениями с таймаутом."""
    def __init__(self, discard_first=False):
        self.buffer = bytearray()
        self.discard = discard_first

    def feed(self, data):
        for byte in data:
            if byte == 10:
                if not self.discard:
                    yield bytes(self.buffer).rstrip(b"\r")
                self.buffer.clear()
                self.discard = False
            elif not self.discard:
                self.buffer.append(byte)
                if len(self.buffer) > 160:
                    self.buffer.clear()
                    self.discard = True


def parse_number(line):
    # Строки REPL, диагностика и ошибочные значения не становятся точками.
    if not 1 <= len(line) <= 3 or not line.isdigit():
        return None
    value = int(line)
    return value if value <= 255 else None


@dataclass
class Snapshot:
    points: list
    count: int
    mean: float
    minimum: object
    maximum: object
    last: object
    status: str


class StreamState:
    def __init__(self):
        self.lock = threading.Lock()
        self.points = deque(maxlen=MAX_POINTS)
        self.status = "Ожидание подключения"
        self.reset()

    def reset(self):
        with self.lock:
            self.points.clear()
            self.count = self.total = 0
            self.minimum = self.maximum = self.last = self.pending_x = None

    def set_status(self, text, break_pair=False):
        with self.lock:
            self.status = text
            if break_pair:
                self.pending_x = None

    def accept(self, value):
        with self.lock:
            self.count += 1
            self.total += value
            self.last = value
            self.minimum = value if self.minimum is None else min(self.minimum, value)
            self.maximum = value if self.maximum is None else max(self.maximum, value)
            if self.pending_x is None:
                self.pending_x = value
            else:
                self.points.append((self.pending_x, value))
                self.pending_x = None

    def snapshot(self):
        with self.lock:
            return Snapshot(list(self.points), self.count,
                            self.total / self.count if self.count else 0.0,
                            self.minimum, self.maximum, self.last, self.status)


class SerialReader(threading.Thread):
    def __init__(self, port, state):
        super().__init__(name="Pico USB reader", daemon=True)
        self.port = port
        self.state = state
        self.stop_event = threading.Event()

    def report(self, text, break_pair=False):
        print(text, flush=True)
        self.state.set_status(text, break_pair)

    def run(self):
        while not self.stop_event.is_set():
            try:
                with serial.Serial(self.port, BAUDRATE, timeout=0.2) as connection:
                    # При открытии можно попасть в середину строки: пропускаем
                    # до первого LF, чтобы не принять хвост «255» за число «5».
                    decoder = LineDecoder(discard_first=True)
                    self.report("Подключено: " + self.port, break_pair=True)
                    last_received = time.monotonic()
                    while not self.stop_event.is_set():
                        data = connection.read(min(max(connection.in_waiting, 1), 4096))
                        for line in decoder.feed(data):
                            value = parse_number(line)
                            if value is not None:
                                self.state.accept(value)
                                last_received = time.monotonic()
                            elif line.startswith(b"#"):
                                self.report(line.decode("utf-8", errors="replace"), True)
                            else:
                                # После поврежденной строки не склеиваем старую пару.
                                self.state.set_status("Пропущена некорректная строка", True)
                        if time.monotonic() - last_received > SILENCE_SECONDS:
                            raise serial.SerialException(
                                "нет чисел от Pico более 5 секунд; проверьте main.py")
            except (serial.SerialException, OSError) as error:
                self.report(f"Нет связи с {self.port}: {error}. Повтор через "
                            f"{RECONNECT_SECONDS:g} с", break_pair=True)
                self.stop_event.wait(RECONNECT_SECONDS)


class ScatterApp:
    def __init__(self, state):
        self.state = state
        self.paused = False
        self.figure, (self.left, self.right) = plt.subplots(
            1, 2, figsize=(11, 6), gridspec_kw={"width_ratios": [3, 1]},
            layout="constrained")
        self.figure.get_layout_engine().set(rect=(0, 0.06, 1, 0.94))
        self.left.set(xlim=(0, 255), ylim=(0, 255),
                      xlabel="X — числа №0, 2, 4…", ylabel="Y — числа №1, 3, 5…")
        self.left.set_aspect("equal", adjustable="box")
        self.left.grid(True, linestyle="--", color="0.85", linewidth=0.6)
        self.dots = self.left.scatter([], [], s=7, c="blue", alpha=0.35, edgecolors="none")
        self.right.set_axis_off()
        self.number = self.right.text(0.5, 0.60, "—", ha="center", va="center",
                                      fontsize=48, transform=self.right.transAxes)
        self.right.text(0.5, 0.45, "Последнее число от Pico", ha="center", fontsize=10,
                        transform=self.right.transAxes)
        self.right.text(0.5, 0.18, "Space — пауза\nR — сброс", ha="center", fontsize=11,
                        transform=self.right.transAxes)
        self.heading = self.figure.suptitle("Квантовый скаттерплот")
        self.status_text = self.figure.text(0.02, 0.015, "", fontsize=8)
        self.figure.canvas.mpl_connect("key_press_event", self.on_key)
        self.animation = FuncAnimation(self.figure, self.update, interval=UPDATE_MS,
                                       cache_frame_data=False)

    def on_key(self, event):
        if event.key == " ":
            self.paused = not self.paused
            self.left.set_title("Пауза" if self.paused else "Прямой эфир")
            self.figure.canvas.draw_idle()
        elif event.key and event.key.lower() == "r":
            # Сброс действует и в паузе; он не очищает USB-буфер устройства.
            self.state.reset()
            self.render(self.state.snapshot())
            self.figure.canvas.draw_idle()

    def render(self, snapshot):
        self.dots.set_offsets(np.asarray(snapshot.points, dtype=float).reshape(-1, 2))
        self.number.set_text("—" if snapshot.last is None else str(snapshot.last))
        stats = (f"Чисел: {snapshot.count} | Среднее: {snapshot.mean:.2f} | "
                 f"Min: {snapshot.minimum if snapshot.minimum is not None else '—'} | "
                 f"Max: {snapshot.maximum if snapshot.maximum is not None else '—'}")
        self.figure.canvas.manager.set_window_title("Квантовый скаттерплот | " + stats)
        self.heading.set_text(stats)
        self.status_text.set_text(snapshot.status[:140])

    def update(self, _frame):
        # При паузе поток чтения продолжает обновлять ограниченный deque.
        # После снятия паузы сразу показываем актуальные последние 2000 точек.
        if not self.paused:
            self.render(self.state.snapshot())
        return self.dots, self.number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=PORT, help="USB-COM порт Pico")
    args = parser.parse_args()
    state = StreamState()
    app = ScatterApp(state)
    reader = SerialReader(args.port, state)
    reader.start()
    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop_event.set()
        reader.join(timeout=3)
        plt.close(app.figure)


if __name__ == "__main__":
    main()
