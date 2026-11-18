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

    def latest_failure(self, task_id):
        """Return the last durable Mission failure for observed-writer recovery."""
        import json

        with self.events._lock:
            row = self.events._db.execute(
                "SELECT payload_json FROM graph_events WHERE task_id=? AND event_type='task.failed' ORDER BY sequence DESC LIMIT 1",
                (task_id,)).fetchone()
            return json.loads(row[0]) if row is not None else None

    def request_observed_writer_recovery(self, task_id, node_id, attempt, artifact_id):
        """Commit the existing recovery marker and attempt audit as one operation.

        Artifact registration and the RUNNING -> INTERRUPTED graph transition
        deliberately precede this transaction, preserving their restart points.
        """
        from .mission_events import append_mission_event

        with self.events._transaction():
            self.events._db.execute(
                "UPDATE task_graph_runs SET recovery_required=1,finished_at=NULL,recovery_plan_json=NULL WHERE task_id=?",
                (task_id,))
            self.events._db.execute(
                "UPDATE task_node_attempts SET status='interrupted',error_code='graph_storage' WHERE task_id=? AND node_id=? AND attempt=? AND finished_at IS NULL",
                (task_id, node_id, attempt))
            append_mission_event(self.events, task_id, "task.storage_recovery_requested",
                {"node_id": node_id, "artifact_id": artifact_id,
                 "reason": "recover_observed_writer_output"}, actor="user")
