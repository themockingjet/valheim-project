"""Tests for the bounded, atomically-written audit trail."""

from pathlib import Path
import tempfile
import unittest

from src import audit


class AuditTrailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.audit_directory = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_read_recent_events_is_empty_without_a_file(self) -> None:
        self.assertEqual(audit.read_recent_events(self.audit_directory), [])

    def test_record_event_appends_and_reads_back(self) -> None:
        audit.record_event(
            self.audit_directory,
            action="restart_request",
            outcome="accepted",
            detail="queued",
            remote_address="127.0.0.1",
        )
        events = audit.read_recent_events(self.audit_directory)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "restart_request")
        self.assertEqual(events[0]["outcome"], "accepted")
        self.assertEqual(events[0]["detail"], "queued")
        self.assertEqual(events[0]["remote_address"], "127.0.0.1")
        self.assertIn("recorded_at", events[0])

    def test_record_event_rejects_unsupported_action(self) -> None:
        with self.assertRaises(ValueError):
            audit.record_event(self.audit_directory, action="delete_everything", outcome="accepted")

    def test_record_event_rejects_unsupported_outcome(self) -> None:
        with self.assertRaises(ValueError):
            audit.record_event(self.audit_directory, action="restart_request", outcome="maybe")

    def test_detail_is_bounded(self) -> None:
        audit.record_event(
            self.audit_directory,
            action="manifest_submit",
            outcome="rejected",
            detail="x" * 1000,
        )
        events = audit.read_recent_events(self.audit_directory)
        self.assertEqual(len(events[0]["detail"]), audit.MAX_DETAIL_CHARACTERS)

    def test_events_are_bounded_in_count(self) -> None:
        for index in range(audit.MAX_AUDIT_EVENTS + 25):
            audit.record_event(
                self.audit_directory,
                action="restart_request",
                outcome="accepted",
                detail=str(index),
            )
        events = audit.read_recent_events(self.audit_directory, limit=audit.MAX_AUDIT_EVENTS + 25)
        self.assertEqual(len(events), audit.MAX_AUDIT_EVENTS)
        # oldest entries are trimmed, newest kept
        self.assertEqual(events[-1]["detail"], str(audit.MAX_AUDIT_EVENTS + 24))

    def test_read_recent_events_respects_limit(self) -> None:
        for index in range(10):
            audit.record_event(
                self.audit_directory,
                action="rollback_request",
                outcome="accepted",
                detail=str(index),
            )
        events = audit.read_recent_events(self.audit_directory, limit=3)
        self.assertEqual([event["detail"] for event in events], ["7", "8", "9"])

    def test_corrupt_audit_file_is_treated_as_empty(self) -> None:
        audit_path = self.audit_directory / audit.AUDIT_FILE_NAME
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text("not json", encoding="utf-8")
        self.assertEqual(audit.read_recent_events(self.audit_directory), [])


if __name__ == "__main__":
    unittest.main()
