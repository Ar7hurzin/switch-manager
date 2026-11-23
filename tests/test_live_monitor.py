"""Background monitor contract: read-only scheduling, exclusivity, and status."""
import threading
import unittest

from live_monitor import LiveMonitor


class LiveMonitorTest(unittest.TestCase):
    def test_success_does_not_reenter_serial(self):
        lock = threading.Lock()
        calls = []
        live = LiveMonitor(
            collect=lambda: calls.append("read") or {"stale": False},
            cli_busy=lambda: False,
            serial_lock=lock, interval=4, retry_interval=12,
        )
        self.assertEqual(live._tick(), 4)
        self.assertEqual(calls, ["read"])
        self.assertEqual(live.status()["state"], "ativo")
        self.assertIsNotNone(live.status()["last_success"])
        self.assertFalse(lock.locked())

    def test_cli_and_serial_occupation_pause_without_collect(self):
        lock = threading.Lock()
        calls = []
        cli = [True]
        live = LiveMonitor(
            collect=lambda: calls.append("read") or {"stale": False},
            cli_busy=lambda: cli[0], serial_lock=lock,
        )
        live._tick()
        self.assertEqual(live.status()["state"], "pausado")
        cli[0] = False
        lock.acquire()
        try:
            live._tick()
            self.assertEqual(live.status()["state"], "aguardando")
        finally:
            lock.release()
        self.assertEqual(calls, [])

    def test_read_failure_backoff_and_lock_release(self):
        lock = threading.Lock()
        def fail():
            raise RuntimeError("simulated timeout")
        live = LiveMonitor(
            collect=fail, cli_busy=lambda: False, serial_lock=lock,
            interval=4, retry_interval=12,
        )
        self.assertEqual(live._tick(), 12)
        self.assertEqual(live.status()["state"], "erro")
        self.assertFalse(lock.locked())
        self.assertIsNone(live.status()["last_success"])

    def test_partial_snapshot_is_marked_stale(self):
        live = LiveMonitor(
            collect=lambda: {"stale": True, "message": "resposta parcial"},
            cli_busy=lambda: False, serial_lock=threading.Lock(),
        )
        self.assertEqual(live._tick(), 15)
        self.assertEqual(live.status()["state"], "desatualizado")
        self.assertIsNone(live.status()["last_success"])

    def test_possible_switch_swap_is_rechecked_without_backoff(self):
        live = LiveMonitor(collect=lambda: {"stale": True, "identity_recheck": True, "message": "porta não reconhecida"},
                           cli_busy=lambda: False, serial_lock=threading.Lock(), interval=4, retry_interval=12)
        self.assertEqual(live._tick(), 4)

    def test_incomplete_coverage_does_not_backoff_healthy_serial_poll(self):
        live = LiveMonitor(collect=lambda: {"stale": False, "snapshot": {"coverage_partial": True}},
                           cli_busy=lambda: False, serial_lock=threading.Lock(), interval=4)
        self.assertEqual(live._tick(), 4)
        self.assertIn("horários", live.status()["message"])
        self.assertEqual(live.status()["state"], "ativo")


if __name__ == "__main__":
    unittest.main()
