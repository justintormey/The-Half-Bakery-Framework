#!/usr/bin/env python3
"""Local LLM agent harness for Half Bakery dispatcher.

Talks to a local llama-server (or any OpenAI-compatible endpoint) via the
OpenAI SDK. Implements a tool-use loop with file operations, bash, grep,
glob, and git — a minimal subset of what the claude CLI provides.

Output format matches `claude --print --output-format json` so the
dispatcher's phase_harvest() can process it identically.

Usage:
    python3 local_agent.py \
        --base-url http://YOUR_SERVER_IP:8080/v1 \
        --model qwen3-coder \
        --persona agents/founding-engineer/AGENTS.md \
        --assignment "## Your Assignment ..." \
        --workdir /path/to/worktree \
        --max-turns 50 \
        --ctx-size 65536
"""

import argparse
import glob as glob_mod
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai package not installed. Run: pip install openai", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the local filesystem. Returns file contents with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the file"},
                    "offset": {"type": "integer", "description": "Line number to start reading from (1-based)"},
                    "limit": {"type": "integer", "description": "Maximum number of lines to read"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, creating it if it doesn't exist or overwriting if it does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the file"},
                    "content": {"type": "string", "description": "The content to write"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific string in a file with a new string. The old_string must appear exactly once in the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the file"},
                    "old_string": {"type": "string", "description": "The exact text to find and replace"},
                    "new_string": {"type": "string", "description": "The replacement text"},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command and return its output. Use for git operations, build commands, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for a regex pattern in files. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory or file to search in"},
                    "include": {"type": "string", "description": "Glob pattern to filter files (e.g. '*.py')"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern. Returns matching file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')"},
                    "path": {"type": "string", "description": "Base directory to search from"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List contents of a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list"},
                },
                "required": ["path"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_read_file(file_path, offset=None, limit=None):
    """Read a file with optional offset/limit, returning numbered lines."""
    path = Path(file_path)
    if not path.exists():
        return f"Error: File not found: {file_path}"
    if not path.is_file():
        return f"Error: Not a file: {file_path}"
    try:
        lines = path.read_text(errors="replace").splitlines()
        start = max(0, (offset or 1) - 1)
        end = start + limit if limit else len(lines)
        numbered = [f"{i + start + 1}\t{line}" for i, line in enumerate(lines[start:end])]
        if not numbered:
            return "(empty file)"
        return "\n".join(numbered)
    except Exception as e:
        return f"Error reading {file_path}: {e}"


def tool_write_file(file_path, content):
    """Write content to a file."""
    path = Path(file_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"Successfully wrote {len(content)} bytes to {file_path}"
    except Exception as e:
        return f"Error writing {file_path}: {e}"


def tool_edit_file(file_path, old_string, new_string):
    """Replace old_string with new_string in a file."""
    path = Path(file_path)
    if not path.exists():
        return f"Error: File not found: {file_path}"
    try:
        text = path.read_text()
        count = text.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {file_path}"
        if count > 1:
            return f"Error: old_string appears {count} times in {file_path}. Must be unique."
        new_text = text.replace(old_string, new_string, 1)
        path.write_text(new_text)
        return f"Successfully edited {file_path}"
    except Exception as e:
        return f"Error editing {file_path}: {e}"


def tool_bash(command, timeout=120, cwd=None):
    """Execute a bash command."""
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if result.returncode != 0:
            output += f"\n(exit code: {result.returncode})"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as e:
        return f"Error executing command: {e}"


def tool_grep(pattern, path=None, include=None, cwd=None):
    """Search for a pattern using ripgrep (rg) or grep."""
    search_path = path or (cwd or ".")
    # Try ripgrep first, fall back to grep
    for grep_cmd in ["rg", "grep -rn"]:
        cmd_parts = grep_cmd.split()
        cmd = cmd_parts + [pattern, search_path]
        if include and "rg" in grep_cmd:
            cmd = cmd_parts + ["--glob", include, pattern, search_path]
        elif include:
            cmd = cmd_parts + [f"--include={include}", pattern, search_path]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, cwd=cwd,
            )
            if result.returncode <= 1:  # 0=matches, 1=no matches
                output = result.stdout.strip()
                # Truncate if too long
                lines = output.splitlines()
                if len(lines) > 100:
                    return "\n".join(lines[:100]) + f"\n... ({len(lines) - 100} more lines)"
                return output or "(no matches)"
            continue  # Try next grep variant
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "Error: Neither rg nor grep available"


def tool_glob(pattern, path=None, cwd=None):
    """Find files matching a glob pattern."""
    base = path or (cwd or ".")
    try:
        matches = sorted(glob_mod.glob(os.path.join(base, pattern), recursive=True))
        if not matches:
            return "(no matches)"
        if len(matches) > 100:
            return "\n".join(matches[:100]) + f"\n... ({len(matches) - 100} more)"
        return "\n".join(matches)
    except Exception as e:
        return f"Error: {e}"


def tool_list_directory(path):
    """List directory contents."""
    p = Path(path)
    if not p.exists():
        return f"Error: Directory not found: {path}"
    if not p.is_dir():
        return f"Error: Not a directory: {path}"
    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        lines = []
        for entry in entries:
            prefix = "d " if entry.is_dir() else "  "
            lines.append(f"{prefix}{entry.name}")
        return "\n".join(lines) or "(empty directory)"
    except Exception as e:
        return f"Error listing {path}: {e}"


# Tool dispatch table
TOOL_HANDLERS = {
    "read_file": lambda args, cwd: tool_read_file(**args),
    "write_file": lambda args, cwd: tool_write_file(**args),
    "edit_file": lambda args, cwd: tool_edit_file(**args),
    "bash": lambda args, cwd: tool_bash(cwd=cwd, **args),
    "grep": lambda args, cwd: tool_grep(cwd=cwd, **args),
    "glob": lambda args, cwd: tool_glob(cwd=cwd, **args),
    "list_directory": lambda args, cwd: tool_list_directory(**args),
}


def execute_tool(name, arguments, workdir):
    """Execute a tool call and return the result string."""
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return f"Error: Unknown tool '{name}'"
    try:
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        return handler(arguments, workdir)
    except Exception as e:
        return f"Error executing tool {name}: {e}"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def build_messages(system_prompt, assignment, initial_message="Execute the assignment above."):
    """Build the initial message list."""
    messages = [
        {"role": "system", "content": system_prompt + "\n\n" + assignment},
        {"role": "user", "content": initial_message},
    ]
    return messages


def _estimate_tokens(messages):
    """Rough token estimate: ~4 chars per token for English/code."""
    total_chars = 0
    for msg in messages:
        if isinstance(msg, dict):
            total_chars += len(msg.get("content", "") or "")
        else:
            # ChatCompletionMessage object
            total_chars += len(msg.content or "")
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    total_chars += len(tc.function.arguments or "")
    return total_chars // 4


def _compact_context(messages, ctx_limit):
    """Shrink old tool results when approaching the context window limit.

    Strategy: walk from oldest to newest, replacing tool-result messages with
    a short summary. Never touch system/user messages (index 0-1) or the
    last 6 messages (the most recent exchange the model needs).
    """
    # Keep at least 25% headroom for the next completion
    threshold = int(ctx_limit * 0.75)
    if _estimate_tokens(messages) <= threshold:
        return  # plenty of room

    # Compact tool results from oldest to newest, skip first 2 and last 6
    safe_end = max(2, len(messages) - 6)
    for i in range(2, safe_end):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "tool":
            content = msg["content"]
            if len(content) > 200:
                # Keep first/last 80 chars so context isn't totally lost
                messages[i] = {**msg, "content": content[:80] + "\n...(compacted)...\n" + content[-80:]}
        if _estimate_tokens(messages) <= threshold:
            return


def run_agent(client, model, messages, tools, workdir, max_turns=50,
              ctx_limit=131072, verbose=False):
    """Run the tool-use loop until the model produces a final text response."""
    turn = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0

    while turn < max_turns:
        turn += 1
        if verbose:
            est = _estimate_tokens(messages)
            print(f"[turn {turn}/{max_turns}] ~{est} tokens ({est*100//ctx_limit}% of ctx)",
                  file=sys.stderr)

        # Compact context if approaching the limit
        _compact_context(messages, ctx_limit)

        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.3,
            )
        except Exception as e:
            error_msg = f"API error on turn {turn}: {e}"
            print(error_msg, file=sys.stderr)
            # If context overflow, try compacting more aggressively and retry once
            if "context" in str(e).lower() or "too long" in str(e).lower():
                _compact_context(messages, ctx_limit // 2)
                try:
                    response = client.chat.completions.create(
                        model=model, messages=messages, tools=tools,
                        tool_choice="auto", temperature=0.3,
                    )
                except Exception as e2:
                    return f"API error (after compact retry): {e2}", total_prompt_tokens, total_completion_tokens
            else:
                return error_msg, total_prompt_tokens, total_completion_tokens

        # Track token usage
        if response.usage:
            total_prompt_tokens += response.usage.prompt_tokens or 0
            total_completion_tokens += response.usage.completion_tokens or 0

        choice = response.choices[0]
        message = choice.message

        # Add assistant message to conversation
        messages.append(message)

        # Check for tool calls
        if message.tool_calls:
            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                if verbose:
                    print(f"  → {fn_name}({json.dumps(fn_args)[:200]})", file=sys.stderr)

                result = execute_tool(fn_name, fn_args, workdir)

                # Truncate very long tool results to save context
                if len(result) > 15000:
                    result = result[:15000] + "\n... (truncated)"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
        else:
            # No tool calls — model is done
            final_text = message.content or ""
            if verbose:
                print(f"[done after {turn} turns]", file=sys.stderr)
            return final_text, total_prompt_tokens, total_completion_tokens

    # Exceeded max turns
    return (
        f"(agent exceeded {max_turns} turns without completing)\n\n"
        + (message.content or ""),
        total_prompt_tokens,
        total_completion_tokens,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_server_health(base_url, timeout=10):
    """Verify the llama-server is reachable and has a model loaded."""
    try:
        import urllib.request
        # Strip /v1 suffix for health endpoint
        health_url = base_url.rstrip("/").removesuffix("/v1") + "/health"
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            status = data.get("status", "unknown")
            if status == "ok":
                return True, "server healthy"
            return False, f"server status: {status}"
    except Exception as e:
        return False, f"health check failed: {e}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Local LLM agent for Half Bakery")
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible API base URL")
    parser.add_argument("--model", required=True, help="Model name for the API")
    parser.add_argument("--persona", required=True, help="Path to agent persona file (AGENTS.md)")
    parser.add_argument("--assignment", required=True, help="Assignment text for the agent")
    parser.add_argument("--workdir", required=True, help="Working directory for the agent")
    parser.add_argument("--max-turns", type=int, default=50, help="Maximum tool-use turns")
    parser.add_argument("--ctx-size", type=int, default=131072, help="Context window limit in tokens for compaction")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr")
    args = parser.parse_args()

    # Health check
    healthy, reason = check_server_health(args.base_url)
    if not healthy:
        # Output error as JSON matching claude --print --output-format json
        output = {
            "result": f"##BLOCKED##Local LLM server unreachable: {reason}",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "total_cost_usd": 0,
            "duration_ms": 0,
        }
        print(json.dumps(output))
        sys.exit(0)

    # Load persona
    persona_path = Path(args.persona)
    if not persona_path.exists():
        output = {
            "result": f"##BLOCKED##Agent persona file not found: {args.persona}",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "total_cost_usd": 0,
            "duration_ms": 0,
        }
        print(json.dumps(output))
        sys.exit(0)

    system_prompt = persona_path.read_text()

    # Build client
    client = OpenAI(base_url=args.base_url, api_key="not-needed")

    # Build conversation
    messages = build_messages(system_prompt, args.assignment)

    start_time = time.time()

    # Run agent loop
    result_text, prompt_tokens, completion_tokens = run_agent(
        client=client,
        model=args.model,
        messages=messages,
        tools=TOOL_DEFINITIONS,
        workdir=args.workdir,
        max_turns=args.max_turns,
        ctx_limit=args.ctx_size,
        verbose=args.verbose,
    )

    duration_ms = int((time.time() - start_time) * 1000)

    # Output in claude --print --output-format json format
    output = {
        "result": result_text,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
        "total_cost_usd": 0,  # Local inference is free
        "duration_ms": duration_ms,
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
