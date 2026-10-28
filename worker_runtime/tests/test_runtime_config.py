import os
import unittest
from unittest.mock import patch

from worker_runtime import api
from worker_runtime.runtime import Worker


class WorkerConfigTests(unittest.TestCase):
    def test_api_prefers_render_port_and_keeps_local_fallback(self) -> None:
        variables = {
            "WORKER_DATABASE_URL": "postgresql://unused/chronos",
            "CHRONOS_API_KEYS": '{"test":{"token":"abcdefghijklmnopqrstuvwxyz0123456789","scopes":["read"]}}',
        }
        with patch.dict(os.environ, {**variables, "PORT": "10000", "CHRONOS_API_PORT": "8000"}):
            with patch("worker_runtime.api.uvicorn.run") as run:
                api.main()
        self.assertEqual(10000, run.call_args.kwargs["port"])

        with patch.dict(os.environ, variables, clear=True):
            with patch("worker_runtime.api.uvicorn.run") as run:
                api.main()
        self.assertEqual(8000, run.call_args.kwargs["port"])

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
