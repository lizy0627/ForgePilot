"""Local deterministic Chat Completions stand-in for the button-status UI demo.

This exercises the real Mission API and approval gate when a paid model is
unavailable. It is deliberately bound to the demo fixture, never a scorer.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import sys


DEMO = Path(__file__).resolve().parent / "qt-button-status"


def answer(messages):
    system = messages[0]["content"]
    payload = json.loads(next(message["content"] for message in messages
                              if message["role"] == "user" and message["content"].startswith("{")))
    if "task_spec" not in payload:
        return {
            "task_type": "bug_fix", "title": "Repair button status update",
            "objective": payload["request"], "repository": payload["repository"],
            "problem": {"observed_behavior": "Click leaves label at Ready",
                        "expected_behavior": "First click shows Active and emits once",
                        "reproduction_steps": ["Build and run CTest; button_updates_status fails at the label assertion"]},
            "acceptance_criteria": [
                {"id": "AC-1", "description": "Project builds with the pinned CMake configuration", "verification": "build"},
                {"id": "AC-2", "description": "Button click regression CTest passes", "verification": "ctest"},
                {"id": "AC-3", "description": "Protected button status test passes", "verification": "protected_test"}],
            "allowed_scope": {"path_patterns": ["src/StatusController.cpp"], "max_changed_files": 1},
            "forbidden_scope": ["CMakeLists.txt", "tests/**", ".git/**"],
            "test_policy": {"existing_tests_read_only": True, "allow_new_tests": False,
                            "modifiable_existing_test_patterns": []},
            "data_policy": payload["data_policy"], "assumptions": [],
            "risks": ["Signal must fire only once"], "risk_level": "low",
        }
    spec = payload["task_spec"]
    if "evidence" not in payload:
        path = "src/StatusController.cpp" if "root-cause" in system else "src/MainWindow.cpp"
        source = (DEMO / path).read_bytes()
        return {"items": [{"evidence_id": "EV-1", "claim": "Button status depends on statusChanged signal",
                            "path": path, "start_line": 1, "end_line": 4,
                            "file_hash": hashlib.sha256(source).hexdigest(),
                            "symbol": "StatusController::activate" if "StatusController" in path else "MainWindow::MainWindow",
                            "source": "local_repository", "confidence": 1.0,
                            "reason": "The committed demo source defines the state update and signal connection."}],
                "summary": "Source evidence verified against the demo baseline."}
    criteria = spec["content"]["acceptance_criteria"]
    return {"task_spec_artifact_id": spec["artifact_id"],
            "steps": [{"step_id": "STEP-1", "owner": "implementer", "depends_on": [],
                       "expected_files": ["src/StatusController.cpp"],
                       "action": "Emit statusChanged(status()) after changing active_ to true.",
                       "verification": list(dict.fromkeys(item["verification"] for item in criteria)),
                       "risk": "Keep the existing early return to avoid repeated emission.",
                       "adds_tests": False,
                       "acceptance_criteria_ids": [item["id"] for item in criteria]}],
            "summary": "One scoped source change followed by build and both test suites."}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 1_048_576:
                raise ValueError("invalid body length")
            request = json.loads(self.rfile.read(length))
            content = json.dumps(answer(request["messages"]), ensure_ascii=False)
            result = {"choices": [{"message": {"role": "assistant", "content": content},
                                   "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}}
            data = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ValueError, KeyError, TypeError):
            self.send_error(422)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8770), Handler).serve_forever()
