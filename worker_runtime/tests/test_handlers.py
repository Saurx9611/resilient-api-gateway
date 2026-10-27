from __future__ import annotations

import base64
import struct
import unittest
import zlib

from worker_runtime.handlers import ExecutionContext, HandlerTimeout, PermanentJobError, execute, validate_payload


def png(width: int = 16, height: int = 9) -> bytes:
    chunk = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)


class HandlerTests(unittest.TestCase):
    def test_image_metadata_reads_png_header_without_decoder(self) -> None:
        result = execute("image_metadata", {"png_base64": base64.b64encode(png()).decode()}, 1)
        self.assertEqual({"format": "png", "width": 16, "height": 9, "bit_depth": 8, "color_type": 2, "encoded_bytes": 33}, result)

    def test_image_rejects_malformed_crc_and_non_png(self) -> None:
        bad = bytearray(png())
        bad[-1] ^= 1
        for data in (bytes(bad), b"GIF89a", b"not-base64!"):
            with self.subTest(data=data[:10]):
                with self.assertRaises(PermanentJobError):
                    execute("image_metadata", {"png_base64": base64.b64encode(data).decode()}, 1)
        with self.assertRaises(PermanentJobError):
            execute("image_metadata", {"png_base64": "%%%"}, 1)

    def test_csv_transform_is_bounded_and_deterministic(self) -> None:
        result = execute("csv_transform", {"csv_text": "name,city\nAda,london\nGrace,New York\n", "column": "city"}, 1)
        self.assertEqual({"rows": 2, "column": "city", "csv_text": "name,city\nAda,LONDON\nGrace,NEW YORK\n"}, result)
        with self.assertRaises(PermanentJobError):
            validate_payload("csv_transform", {"csv_text": "a" * (257 * 1024), "column": "a"})
        with self.assertRaises(PermanentJobError):
            execute("csv_transform", {"csv_text": "value\n" + "x\n" * 10_001, "column": "value"}, 1)

    def test_synthetic_report_aggregates_and_validates(self) -> None:
        result = execute("synthetic_report", {"records": [{"category": "books", "amount": 4}, {"category": "books", "amount": -1}, {"category": "games", "amount": 9}]}, 1)
        self.assertEqual({"record_count": 3, "categories": {"books": {"count": 2, "total": 3}, "games": {"count": 1, "total": 9}}}, result)
        with self.assertRaises(PermanentJobError):
            validate_payload("synthetic_report", {"records": [{"category": "a", "amount": True}]})

    def test_notification_is_simulated_and_allowlisted(self) -> None:
        result = execute("notification_simulation", {"template": "receipt", "recipient_ref": "acct_123", "delay_ms": 1}, 1)
        self.assertEqual({"delivery": "simulated", "template": "receipt"}, result)
        with self.assertRaises(PermanentJobError):
            validate_payload("notification_simulation", {"template": "custom_code", "recipient_ref": "x"})

    def test_cooperative_context_deadline(self) -> None:
        context = ExecutionContext(0)
        with self.assertRaises(HandlerTimeout):
            context.checkpoint()

    def test_unknown_job_type_never_loads_or_executes_code(self) -> None:
        with self.assertRaises(PermanentJobError):
            validate_payload("shell", {"command": "echo unsafe"})
        with self.assertRaises(PermanentJobError):
            validate_payload("notification_simulation", {"template": "alert", "recipient_ref": "x", "command": "echo unsafe"})
        with self.assertRaises(PermanentJobError):
            validate_payload("synthetic_report", {"records": [{"category": "x", "amount": float("nan")} ]})


if __name__ == "__main__":
    unittest.main()
