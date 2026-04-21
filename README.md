# prompt_compiler MCP

Transforms rough plain-language intent into precise, minimal Claude Code prompts.

Describe what you want in plain English. `prompt_compiler` detects your intent, strips filler words, maps to the right action verb, and appends an intent-appropriate constraint — so every prompt you send to Claude is focused, scoped, and free of ambiguity. Run `scan_project` once on your repo and compiled prompts automatically carry a compact stack fingerprint, giving Claude just enough context without dumping file contents.

---

## Features

- **Intent detection** — classifies raw input as `create`, `fix`, `refactor`, `explain`, `test`, `delete`, or `read`
- **Filler stripping** — removes "i want to", "please", "can you", "basically", and similar noise
- **Action-verb mapping** — maps each intent to a precise verb: _Fix_, _Refactor_, _Write tests for_, etc.
- **Intent-appropriate constraints** — appends a guard per intent type (e.g. "Only fix the reported issue." for bugs)
- **Project fingerprinting** — scans `package.json`, `requirements.txt`, `pyproject.toml`, `tsconfig.json`, `.eslintrc`, and README (first 3 lines) to build a ≤80-char stack summary
- **Cache-aware compilation** — reads `.prompt_compiler_cache.json` to inject stack context into every compiled prompt
- **Task splitting** — splits compound tasks on conjunctions and produces ordered, atomic steps
- **Prompt templates** — five `[PLACEHOLDER]`-style templates for the most common workflows
- **Never reads source code** — fingerprinting touches only manifest and config files; `.env` files are never read

---

## Installation

Requires Python 3.10+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/yinyarong/prompt_compiler
cd prompt_compiler
uv sync
```

---

## Configuration

### Claude Code

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "prompt_compiler": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/prompt_compiler",
        "run", "server.py"
      ],
      "type": "stdio"
    }
  }
}
```

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "prompt_compiler": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/prompt_compiler",
        "run", "server.py"
      ]
    }
  }
}
```

---

## Tools

### `compile_prompt`

Converts a rough intent into a focused, constraint-bound Claude Code prompt.

| Parameter | Type | Description |
|-----------|------|-------------|
| `raw_intent` | `str` | Plain-language task description |
| `scope` | `str` (optional) | File or module context (e.g. `src/auth.py`) |

**Example input:**
```
i want to fix the login button not working on mobile
```

**Example output:**
```json
{
  "detected_intent": "fix",
  "compiled_prompt": "Fix the login button not working on mobile.\nStack: React+Vite / FastAPI / pytest\n\nConstraint: Do not refactor unrelated code. Only fix the reported issue.",
  "char_count": 142,
  "tips": []
}
```

---

### `split_task`

Splits a compound task into ordered, atomic prompt steps. Splits on: _and then_, _then_, _after that_, _also_, _next_, _finally_, _and_.

| Parameter | Type | Description |
|-----------|------|-------------|
| `raw_intent` | `str` | Compound task description |

**Example input:**
```
add oauth login and then write tests for it and finally update the docs
```

**Example output:**
```json
{
  "total_steps": 3,
  "note": "Execute steps sequentially. Verify each step before proceeding.",
  "steps": [
    { "step": 1, "intent": "create", "prompt": "Create add oauth login." },
    { "step": 2, "intent": "test",   "prompt": "Write tests for it." },
    { "step": 3, "intent": "create", "prompt": "Create update the docs." }
  ]
}
```

---

### `get_template`

Returns a `[PLACEHOLDER]`-style prompt template for a common workflow.

| Template name | Use case |
|---------------|----------|
| `create_feature` | Building new functionality |
| `fix_bug` | Debugging a specific issue |
| `refactor` | Improving code structure |
| `add_test` | Adding test coverage |
| `explain_code` | Understanding existing code |

---

### `scan_project`

Scans a project root and writes a compact stack fingerprint to `.prompt_compiler_cache.json`. Subsequent `compile_prompt` calls read this cache automatically.

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_path` | `str` | Absolute path to project root |

**Files inspected (no source code):**

- `package.json` → framework, test runner, lint tools
- `requirements.txt` / `pyproject.toml` → Python packages
- `tsconfig.json` → strict mode flag
- `.eslintrc` / `.eslintrc.json` → ESLint presence
- Top-level folder names (1 level, no recursion)
- `README.md` (first 3 lines only)

**Cache written to:** `{project_path}/.prompt_compiler_cache.json`

**Example fingerprint:**
```
React+Vite / FastAPI / pytest / src+api layout / ESLint
```

---

## How It Works

```
raw_intent
    │
    ├─ detect intent (fix / create / refactor / …)
    ├─ strip filler words
    ├─ map to action verb
    ├─ read .prompt_compiler_cache.json (if present)
    │       └─ inject stack fingerprint
    ├─ append scope (if provided)
    └─ append intent constraint
           │
           ▼
    compiled_prompt
```

The fingerprint cache is project-local and safe to commit to `.gitignore`. Re-run `scan_project` whenever your stack changes.

---

## License

MIT
