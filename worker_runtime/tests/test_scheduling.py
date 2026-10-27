from __future__ import annotations

import unittest
from datetime import datetime, timezone

from worker_runtime.scheduling import next_occurrence, validate_schedule


class CronTimezoneTests(unittest.TestCase):
    def test_five_field_cron_in_timezone(self) -> None:
        after = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc),
                         next_occurrence("0 20 * * *", "Asia/Kolkata", after))

    def test_nonexistent_spring_forward_wall_time_is_skipped(self) -> None:
        # 02:30 does not exist on 2026-03-08 in New York.
        after = datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc)
        self.assertEqual(datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc),
                         next_occurrence("30 2 * * *", "America/New_York", after))

    def test_ambiguous_fall_back_wall_time_runs_once_at_earlier_instant(self) -> None:
        after = datetime(2026, 11, 1, 4, 59, tzinfo=timezone.utc)
        first = next_occurrence("30 1 * * *", "America/New_York", after)
        self.assertEqual(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc), first)
        self.assertEqual(datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc),
                         next_occurrence("30 1 * * *", "America/New_York", first))

    def test_invalid_cron_and_unknown_timezone_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_schedule("61 * * * *", "UTC")
        with self.assertRaises(ValueError):
            validate_schedule("0 0 31 2 *", "UTC")
        with self.assertRaises(ValueError):
            validate_schedule("0 0 * * *", "Mars/Olympus")


if __name__ == "__main__":
    unittest.main()
