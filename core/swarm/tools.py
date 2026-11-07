"""Tools exposed to swarm agents. `log_step` is required before every edit or command."""

from __future__ import annotations

_GATE = "Requires a preceding `log_step` call; without one the call is REJECTED and nothing happens."

LOG_STEP_TOOL = {
    "type": "function",
    "function": {
        "name": "log_step",
        "description": (
            "REQUIRED before every write_file, run_tests or run_spec_tests call: record the assumption "
            "you are acting on, the action you are about to take, and the files it touches. Each log_step "
            "arms exactly one following action; actions without it are rejected."
        ),
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

LIST_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "List repo-relative paths in the sandbox matching a glob (default '**/*'). No log_step needed.",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "Glob, e.g. 'src/**/*.py' or 'tests/*'."}},
        },
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a repo-relative file from the sandbox (truncated to 20k bytes). No log_step needed.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": f"Create or overwrite a repo-relative file in the sandbox with the full content. {_GATE}",
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
        "description": f"Run pytest in the sandbox on a repo-relative file or directory (default: whole suite) and return the output. {_GATE}",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Optional test file or directory, e.g. 'tests/test_x.py'."}},
        },
    },
}

RUN_SPEC_TESTS_TOOL = {
    "type": "function",
    "function": {
        "name": "run_spec_tests",
        "description": (
            "Run the independent acceptance tests for this subtask against your current code. Returns pass/fail "
            "per visible case with the input and, on failure, expected vs actual. These tests were written from "
            f"the spec before any implementation; a hidden set decides the final verdict. {_GATE}"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

DONE_TOOL = {
    "type": "function",
    "function": {
        "name": "done",
        "description": "Stop: call once run_spec_tests passes, or when you have done your best. No log_step needed.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}},
    },
}

AGENT_TOOLS = [LOG_STEP_TOOL, LIST_FILES_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL, RUN_TESTS_TOOL, RUN_SPEC_TESTS_TOOL, DONE_TOOL]
GATED_TOOLS = {"write_file", "run_tests", "run_spec_tests"}
