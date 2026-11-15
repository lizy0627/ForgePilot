"""Persistence operations for approval-bound implementation and delivery.

The surrounding workflow keeps its existing consistency windows and business
rules. This repository owns row selection and validation-attempt allocation;
the existing Mission repository still commits records and their outbox events.
"""


class DeliveryRepository:
    def __init__(self, events):
        self.events = events

    def approved_grant_ids(self, task_id, kind):
        """Select approval candidates; callers validate each frozen package on use."""
        with self.events._lock:
            rows = self.events._db.execute(
                "SELECT approval_id FROM approvals WHERE task_id=? AND approval_type=? AND status='approved'",
                (task_id, kind)).fetchall()
            return tuple(row[0] for row in rows)

    def record_next_validation(self, *, validation_id, task_id, status,
                               started_at, finished_at, report_artifact_id, error_code):
        """Allocate the next audit attempt without a competing local writer."""
        with self.events._lock:
            attempt = self.events._db.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 FROM validation_runs WHERE task_id=?",
                (task_id,)).fetchone()[0]
            return self.events.missions.record_validation(validation_id=validation_id, task_id=task_id,
                attempt=attempt, status=status, started_at=started_at, finished_at=finished_at,
                report_artifact_id=report_artifact_id, error_code=error_code)
