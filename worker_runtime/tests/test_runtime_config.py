import unittest

from worker_runtime.runtime import Worker


class WorkerConfigTests(unittest.TestCase):
    def test_resource_and_identity_bounds(self) -> None:
        with self.assertRaises(ValueError):
            Worker("postgresql://unused", concurrency=17)
        with self.assertRaises(ValueError):
            Worker("postgresql://unused", lease_seconds=1)
        with self.assertRaises(ValueError):
            Worker("postgresql://unused", handler_timeout=0.01)
        with self.assertRaises(ValueError):
            Worker("postgresql://unused", name="bad\nworker")
        worker = Worker("postgresql://unused", concurrency=16, lease_seconds=300, name="worker-1")
        worker.repo.close()


if __name__ == "__main__":
    unittest.main()
