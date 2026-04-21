#!/usr/bin/env python3
"""
MCP Server: prompt_compiler_mcp

Transforms rough plain-language intent into precise, minimal Claude Code prompts.
Scans the project directory for a lightweight context fingerprint so compiled
prompts are automatically aware of stack and conventions.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

mcp = FastMCP("prompt_compiler_mcp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FILLER_PATTERNS = re.compile(
    r"\b(i want to|i need to|please|can you|help me|just|basically)\b",
    re.IGNORECASE,
)

INTENT_VERBS = {
    "create": "Create",
    "fix": "Fix",
    "refactor": "Refactor",
    "explain": "Explain",
    "test": "Write tests for",
    "delete": "Remove",
    "read": "Find and show",
}

INTENT_CONSTRAINTS = {
    "fix": "Do not refactor unrelated code. Only fix the reported issue.",
    "refactor": "Preserve existing behavior. Do not change public APIs.",
    "test": "Use existing test framework. Cover happy path + one edge case.",
    "delete": "Confirm what will be removed before deleting.",
    "create": "Follow existing project conventions. Minimal focused changes only.",
}

INTENT_KEYWORDS = {
    "fix": ["fix", "bug", "error", "broken", "issue", "crash", "fail", "wrong", "repair"],
    "refactor": ["refactor", "clean", "reorganize", "restructure", "simplify", "rewrite"],
    "explain": ["explain", "understand", "what is", "how does", "describe", "clarify"],
    "test": ["test", "spec", "coverage", "unit test", "integration test"],
    "delete": ["delete", "remove", "drop", "eliminate", "clear"],
    "read": ["read", "show", "display", "list", "find", "get", "fetch", "view"],
    "create": ["create", "add", "build", "implement", "write", "generate", "make", "new"],
}

# Words safe to strip when they appear as the FIRST word of the cleaned text,
# because they duplicate the mapped action verb. Only single-word synonyms are
# listed — phrasal-verb triggers like "clean up" are intentionally excluded so
# stripping "clean" doesn't leave an orphaned "up".
LEADING_STRIP_WORDS: dict[str, set[str]] = {
    "fix":      {"fix", "repair"},
    "refactor": {"refactor"},
    "explain":  {"explain", "understand", "describe", "clarify"},
    "test":     {"test"},
    "delete":   {"delete", "remove", "drop", "eliminate"},
    "read":     {"find", "show", "list", "get", "fetch", "view", "read"},
    "create":   {"create", "add", "build", "implement", "make", "generate", "write"},
}

SPLIT_PATTERN = re.compile(
    r"\s+(?:and then|then|after that|also|next|finally|and)\s+",
    re.IGNORECASE,
)

TEMPLATES = {
    "create_feature": {
        "template": "Create [FEATURE_NAME] in [FILE_OR_MODULE].\n\n"
                    "Stack: [STACK_DESCRIPTION]\n"
                    "Scope: [SCOPE]\n\n"
                    "Follow existing project conventions. Minimal focused changes only.",
        "placeholders": ["FEATURE_NAME", "FILE_OR_MODULE", "STACK_DESCRIPTION", "SCOPE"],
        "usage": "Use when building a new feature or adding new functionality.",
    },
    "fix_bug": {
        "template": "Fix [BUG_DESCRIPTION] in [FILE_OR_MODULE].\n\n"
                    "Observed behavior: [OBSERVED]\n"
                    "Expected behavior: [EXPECTED]\n\n"
                    "Do not refactor unrelated code. Only fix the reported issue.",
        "placeholders": ["BUG_DESCRIPTION", "FILE_OR_MODULE", "OBSERVED", "EXPECTED"],
        "usage": "Use when debugging a specific issue or error.",
    },
    "refactor": {
        "template": "Refactor [FILE_OR_MODULE] to [GOAL].\n\n"
                    "Stack: [STACK_DESCRIPTION]\n\n"
                    "Preserve existing behavior. Do not change public APIs.",
        "placeholders": ["FILE_OR_MODULE", "GOAL", "STACK_DESCRIPTION"],
        "usage": "Use when improving code structure without changing behavior.",
    },
    "add_test": {
        "template": "Write tests for [FUNCTION_OR_MODULE].\n\n"
                    "Stack: [STACK_DESCRIPTION]\n"
                    "Test file: [TEST_FILE_PATH]\n\n"
                    "Use existing test framework. Cover happy path + one edge case.",
        "placeholders": ["FUNCTION_OR_MODULE", "STACK_DESCRIPTION", "TEST_FILE_PATH"],
        "usage": "Use when adding test coverage to existing code.",
    },
    "explain_code": {
        "template": "Explain [FILE_OR_FUNCTION].\n\n"
                    "Focus on: [ASPECT_TO_EXPLAIN]\n"
                    "Audience: [AUDIENCE]\n\n"
                    "Be concise. Use inline examples where helpful.",
        "placeholders": ["FILE_OR_FUNCTION", "ASPECT_TO_EXPLAIN", "AUDIENCE"],
        "usage": "Use when you need a clear explanation of code logic or architecture.",
    },
}

SCAN_TARGETS = [
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "tsconfig.json",
    ".eslintrc",
    ".eslintrc.json",
    "README.md",
]

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _strip_filler(text: str) -> str:
    cleaned = FILLER_PATTERNS.sub("", text)
    # Collapse gaps left by removed words, then re-run so newly adjacent phrases
    # (e.g. "i just need to" → "i need to" after "just" is removed) are also caught.
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = FILLER_PATTERNS.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    # Strip orphaned leading function words that survive both passes
    cleaned = re.sub(r"^(i|to|the)\b\s*", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def _strip_leading_verb(clean: str, intent: str) -> str:
    """Remove leading verb if it duplicates the mapped action verb phrase or a known synonym."""
    # Check full multi-word verb first (e.g. "write tests for")
    verb_phrase = INTENT_VERBS.get(intent, "").lower()
    if clean.lower().startswith(verb_phrase):
        return clean[len(verb_phrase):].strip()
    # Check single-word synonyms
    first_word = clean.split()[0] if clean else ""
    if first_word.lower() in LEADING_STRIP_WORDS.get(intent, set()):
        return clean[len(first_word):].strip()
    return clean


def _detect_intent(text: str) -> str:
    lower = text.lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return intent
    return "create"


def _action_verb(intent: str) -> str:
    return INTENT_VERBS.get(intent, "Create")


def _load_cache(cwd: str) -> Optional[dict]:
    cache_path = os.path.join(cwd, ".prompt_compiler_cache.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class CompilePromptInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    raw_intent: str = Field(..., description="Plain-language description of the task", min_length=1, max_length=2000)
    scope: Optional[str] = Field(default=None, description="Optional scope or file context (e.g., 'src/auth.py')", max_length=500)


class SplitTaskInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    raw_intent: str = Field(..., description="Plain-language compound task to split into steps", min_length=1, max_length=2000)


class GetTemplateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    template_name: str = Field(
        ...,
        description="Template to retrieve. One of: create_feature, fix_bug, refactor, add_test, explain_code",
    )


class ScanProjectInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    project_path: str = Field(..., description="Absolute path to the project root directory to scan", min_length=1)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="compile_prompt",
    annotations={
        "title": "Compile Prompt",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def compile_prompt(params: CompilePromptInput) -> str:
    """Transform raw plain-language intent into a precise, minimal Claude Code prompt.

    Reads .prompt_compiler_cache.json from the current working directory (if present)
    to include a project stack fingerprint. Detects intent, strips filler words, maps
    to an action verb, and adds an intent-appropriate constraint.

    Args:
        params (CompilePromptInput): Validated input containing:
            - raw_intent (str): The rough task description
            - scope (Optional[str]): Optional file or scope context

    Returns:
        str: JSON string with keys:
            {
                "detected_intent": str,   # create/fix/refactor/explain/test/delete/read
                "compiled_prompt": str,   # The final compact prompt
                "char_count": int,
                "worth_calling": bool,    # False = skipping this tool next time is fine
                "skip_reason": str | None,# Human-readable explanation when worth_calling=False
                "tips": list[str]
            }
            On error: {"error": str}
    """
    try:
        intent = _detect_intent(params.raw_intent)
        clean = _strip_filler(params.raw_intent)
        clean = _strip_leading_verb(clean, intent)
        verb = _action_verb(intent)

        cwd = os.getcwd()
        cache = _load_cache(cwd)
        stack_line = f"Stack: {cache['fingerprint']}" if cache and cache.get("fingerprint") else None

        lines: list[str] = []
        lines.append(f"{verb} {clean}.")
        if params.scope:
            lines.append(f"Scope: {params.scope}")
        if stack_line:
            lines.append(stack_line)
        constraint = INTENT_CONSTRAINTS.get(intent)
        if constraint:
            lines.append(f"\nConstraint: {constraint}")

        compiled = "\n".join(lines)

        # --- worth_calling heuristic ---
        # Measure noise removal: compare raw input length against the stripped
        # content only (not the compiled output, which adds constraint lines).
        raw_len = len(params.raw_intent)
        compiled_len = len(compiled)
        filler_matches = len(FILLER_PATTERNS.findall(params.raw_intent))
        # `clean` is the de-noised content; savings = fraction of input that was filler
        noise_ratio = (raw_len - len(clean)) / raw_len if raw_len > 0 else 0

        skip_reason: str | None = None
        if filler_matches == 0 and raw_len < 80 and not cache:
            skip_reason = (
                "Input is already concise with no filler and no project cache — "
                "calling compile_prompt adds tool-call overhead without measurable benefit."
            )
        elif noise_ratio < 0.10 and raw_len < 80:
            skip_reason = (
                f"Only {noise_ratio:.0%} of the input was filler noise. "
                "The tool-call cost likely exceeds the compression value."
            )
        worth_calling = skip_reason is None

        tips: list[str] = []
        if not cache:
            tips.append("Run scan_project first to auto-include your stack in compiled prompts.")
        if raw_len > 200:
            tips.append("Consider using split_task to break this into smaller steps.")
        if intent == "create" and not params.scope:
            tips.append("Add a scope (e.g., 'src/auth.py') to make the prompt more precise.")

        return json.dumps(
            {
                "detected_intent": intent,
                "compiled_prompt": compiled,
                "char_count": compiled_len,
                "worth_calling": worth_calling,
                "skip_reason": skip_reason,
                "tips": tips,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": f"compile_prompt failed: {type(e).__name__}: {e}"})


@mcp.tool(
    name="split_task",
    annotations={
        "title": "Split Task into Steps",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def split_task(params: SplitTaskInput) -> str:
    """Split a compound raw intent into ordered, atomic prompt steps.

    Splits on conjunctions (and then, then, after that, also, next, finally, and),
    detects intent per chunk, strips filler, and prepends the appropriate action verb.

    Args:
        params (SplitTaskInput): Validated input containing:
            - raw_intent (str): Compound task description

    Returns:
        str: JSON string with keys:
            {
                "total_steps": int,
                "note": str,
                "steps": list[{"step": int, "intent": str, "prompt": str}]
            }
            On error: {"error": str}
    """
    try:
        chunks = SPLIT_PATTERN.split(params.raw_intent)
        steps = []
        for i, chunk in enumerate(chunks, 1):
            chunk = chunk.strip()
            if not chunk:
                continue
            intent = _detect_intent(chunk)
            clean = _strip_filler(chunk)
            clean = _strip_leading_verb(clean, intent)
            verb = _action_verb(intent)
            steps.append(
                {
                    "step": i,
                    "intent": intent,
                    "prompt": f"{verb} {clean}.",
                }
            )

        return json.dumps(
            {
                "total_steps": len(steps),
                "note": "Execute steps sequentially. Verify each step before proceeding.",
                "steps": steps,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": f"split_task failed: {type(e).__name__}: {e}"})


@mcp.tool(
    name="get_template",
    annotations={
        "title": "Get Prompt Template",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_template(params: GetTemplateInput) -> str:
    """Retrieve a structured prompt template with [PLACEHOLDER] syntax.

    Available templates: create_feature, fix_bug, refactor, add_test, explain_code.

    Args:
        params (GetTemplateInput): Validated input containing:
            - template_name (str): Name of the template to retrieve

    Returns:
        str: JSON string with keys:
            {
                "template": str,
                "placeholders": list[str],
                "usage": str
            }
            On error: {"error": str}
    """
    try:
        entry = TEMPLATES.get(params.template_name)
        if entry is None:
            available = ", ".join(TEMPLATES.keys())
            return json.dumps(
                {"error": f"Unknown template '{params.template_name}'. Available: {available}"}
            )

        return json.dumps(
            {
                "template": entry["template"],
                "placeholders": entry["placeholders"],
                "usage": entry["usage"],
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": f"get_template failed: {type(e).__name__}: {e}"})


@mcp.tool(
    name="scan_project",
    annotations={
        "title": "Scan Project and Build Context Fingerprint",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def scan_project(params: ScanProjectInput) -> str:
    """Scan a project directory and build a compact stack fingerprint.

    Reads only config/manifest files (never source code or .env). Writes a
    .prompt_compiler_cache.json to the project root so compile_prompt can
    automatically include stack context.

    Files inspected: package.json, requirements.txt, pyproject.toml,
    tsconfig.json, .eslintrc, .eslintrc.json, README.md (first 3 lines only).
    Top-level folder names are listed (one level, no recursion).

    Args:
        params (ScanProjectInput): Validated input containing:
            - project_path (str): Absolute path to the project root

    Returns:
        str: JSON string with keys:
            {
                "fingerprint": str,   # compact ≤80-char description
                "files_read": list[str],
                "cache_path": str
            }
            On error: {"error": str}
    """
    try:
        root = params.project_path
        if not os.path.isdir(root):
            return json.dumps({"error": f"Directory not found: {root}"})

        parts: list[str] = []
        files_read: list[str] = []

        # --- package.json ---
        pkg_path = os.path.join(root, "package.json")
        if os.path.isfile(pkg_path):
            try:
                with open(pkg_path) as f:
                    pkg = json.load(f)
                files_read.append("package.json")
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                fw_candidates = []
                if "react" in deps:
                    fw_candidates.append("React")
                if "vue" in deps:
                    fw_candidates.append("Vue")
                if "next" in deps:
                    fw_candidates.append("Next.js")
                if "vite" in deps:
                    fw_candidates.append("Vite")
                if fw_candidates:
                    parts.append("+".join(fw_candidates))
                test_candidates = []
                if "jest" in deps:
                    test_candidates.append("Jest")
                if "vitest" in deps:
                    test_candidates.append("Vitest")
                if "mocha" in deps:
                    test_candidates.append("Mocha")
                if test_candidates:
                    parts.append("+".join(test_candidates))
                if "eslint" in deps:
                    parts.append("ESLint")
            except Exception:
                pass

        # --- requirements.txt ---
        req_path = os.path.join(root, "requirements.txt")
        if os.path.isfile(req_path):
            try:
                with open(req_path) as f:
                    req_lines = [line.strip().lower() for line in f if line.strip()]
                files_read.append("requirements.txt")
                py_fw = []
                for line in req_lines:
                    pkg_name = re.split(r"[=><!@\[]", line)[0].strip()
                    if pkg_name in ("fastapi",):
                        py_fw.append("FastAPI")
                    elif pkg_name in ("django",):
                        py_fw.append("Django")
                    elif pkg_name in ("flask",):
                        py_fw.append("Flask")
                    elif pkg_name in ("pytest",):
                        py_fw.append("pytest")
                if py_fw:
                    parts.append("+".join(py_fw))
            except Exception:
                pass

        # --- pyproject.toml ---
        ppt_path = os.path.join(root, "pyproject.toml")
        if os.path.isfile(ppt_path) and "requirements.txt" not in files_read:
            try:
                with open(ppt_path) as f:
                    content = f.read().lower()
                files_read.append("pyproject.toml")
                py_fw = []
                for name, label in [("fastapi", "FastAPI"), ("django", "Django"), ("flask", "Flask"), ("pytest", "pytest")]:
                    if name in content:
                        py_fw.append(label)
                if py_fw:
                    parts.append("+".join(py_fw))
            except Exception:
                pass

        # --- tsconfig.json ---
        ts_path = os.path.join(root, "tsconfig.json")
        if os.path.isfile(ts_path):
            try:
                with open(ts_path) as f:
                    ts = json.load(f)
                files_read.append("tsconfig.json")
                if ts.get("compilerOptions", {}).get("strict"):
                    parts.append("TS-strict")
            except Exception:
                pass

        # --- .eslintrc / .eslintrc.json ---
        for eslint_name in (".eslintrc", ".eslintrc.json"):
            eslint_path = os.path.join(root, eslint_name)
            if os.path.isfile(eslint_path):
                files_read.append(eslint_name)
                if "ESLint" not in parts:
                    parts.append("ESLint")
                break

        # --- Top-level folders ---
        try:
            top_dirs = sorted(
                d for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")
                and d not in ("node_modules", "__pycache__", ".git", "dist", "build", "out")
            )
            if top_dirs:
                parts.append("+".join(top_dirs[:6]) + " layout")
        except Exception:
            pass

        # --- README.md (first 3 lines) ---
        readme_path = os.path.join(root, "README.md")
        if os.path.isfile(readme_path):
            try:
                with open(readme_path) as f:
                    first_lines = [next(f, "").strip() for _ in range(3)]
                files_read.append("README.md")
                # Just scanning for presence; fingerprint is built from manifests
            except Exception:
                pass

        fingerprint = " / ".join(p for p in parts if p)
        if not fingerprint:
            fingerprint = "Unknown stack"
        # Clamp to 80 chars
        if len(fingerprint) > 80:
            fingerprint = fingerprint[:77] + "..."

        cache_data = {
            "fingerprint": fingerprint,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "project_path": root,
        }
        cache_path = os.path.join(root, ".prompt_compiler_cache.json")
        with open(cache_path, "w") as f:
            json.dump(cache_data, f, indent=2)

        return json.dumps(
            {
                "fingerprint": fingerprint,
                "files_read": files_read,
                "cache_path": cache_path,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": f"scan_project failed: {type(e).__name__}: {e}"})


if __name__ == "__main__":
    mcp.run()
