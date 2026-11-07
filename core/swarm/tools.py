"""Tools exposed to swarm agents. `log_step` is required before every edit or command."""

from __future__ import annotations

LOG_STEP_TOOL = {
    "type": "function",
    "function": {
        "name": "log_step",
        "description": "Record your reasoning BEFORE each edit or command: the assumption you are acting on, the action you will take, and the files it touches.",
        "parameters": {
            "type": "object",
            "properties": {
                "assumption": {"type": "string", "description": "The belief about the task, code or inputs this action relies on."},
                "action": {"type": "string", "description": "What you are about to do."},
                "files_touched": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["assumption", "action"],
        },
    },
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Create or overwrite a file in the sandbox.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}

RUN_TESTS_TOOL = {
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": "Run pytest in the sandbox and return the output.",
        "parameters": {"type": "object", "properties": {}},
    },
}

DONE_TOOL = {
    "type": "function",
    "function": {
        "name": "done",
        "description": "Call when the implementation and its tests are complete.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}},
    },
}

AGENT_TOOLS = [LOG_STEP_TOOL, WRITE_FILE_TOOL, RUN_TESTS_TOOL, DONE_TOOL]
