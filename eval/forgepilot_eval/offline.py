"""Deterministic model fixture. Never used by the real-model mode."""
import json

from forgepilot.llm import ChatReply


def message(content=None, calls=None):
    return ChatReply.model_validate({"message": {"role": "assistant", "content": content, "tool_calls": calls}, "usage": None})


def call(name, arguments, identity):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def plan(task, files):
    return {"summary": "OFFLINE FIXTURE: " + task.title, "root_cause": task.initial_state.description,
        "files_to_modify": [{"path": p, "reason": "Apply the evaluator's fixed development fixture"} for p in files],
        "implementation_steps": ["Apply the fixed source patch and preserve tests"],
        "validation_steps": ["Runtime Configure, Build and CTest"], "risks": ["Offline script is not model reasoning"]}


class OfflineModel:
    def __init__(self, task, solution, scenario="success"):
        script = [message(calls=[call("read_file", {"path": "README.md"}, "inspect")])]
        if task.analysis:
            script += [message(json.dumps(solution["answer"] if scenario != "wrong_analysis" else {"files": [], "symbols": [], "relations": []}))]
        else:
            files = solution.get("initial_files", solution["files"])
            scope = ["README.md"] if scenario == "out_of_scope" else list(solution["files"])
            script += [message("OFFLINE FIXTURE analysis"), message(json.dumps(plan(task, scope)))]
            if scenario != "no_change":
                script += [message(calls=[call("read_file", {"path": p}, f"read-{i}") for i, p in enumerate(files)] +
                    [call("write_file", {"path": p, "content": content}, f"write-{i}") for i, (p, content) in enumerate(files.items())])]
            script += [message("OFFLINE FIXTURE initial change")]
            if "initial_files" in solution and scenario != "no_change":
                script += [message(json.dumps({"category": "code", "plan": plan(task, solution["files"])})),
                    message(calls=[call("write_file", {"path": p, "content": content}, f"repair-{i}") for i, (p, content) in enumerate(solution["files"].items())]),
                    message("OFFLINE FIXTURE repair")]
        self.iterator = iter(script)

    async def chat(self, messages, tools=None):
        return next(self.iterator)

    async def aclose(self):
        pass
