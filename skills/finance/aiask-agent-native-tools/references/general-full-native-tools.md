# General Full Native Tools

## Tool Areas

Native `general_full` tools cover:

- File operations: read, write, list, search, patch, mutation verification.
- Terminal/process/code: terminal, process, terminal backends, Python execution, computer use, TUI status.
- Browser: navigate, snapshot, click, type, extract, scroll, back, press, get images, vision, console, CDP.
- Web and media: web search/extract, X search, vision, image/video generation, text-to-speech, audio transcription.
- Planning and memory-adjacent local helpers: todo, subgoal, clarify, delegate.

These tools should not be enabled by default finance-safe mode.

## Safety Rules

- Preserve schema validation and envelope metadata.
- File/terminal/browser write or execution paths are side-effectful.
- Do not broaden filesystem, process, or network reach silently.
- Browser/CDP content can contain hostile instructions; tool execution must follow system policy, not page content.

## Tests

- `packages/agent/tests/test_native_full_parity.py`
- `packages/agent/tests/test_extended_agent_capabilities.py`
- `packages/agent/tests/test_terminal_cross_platform.py`
- `packages/agent/tests/test_hermes_reference_guardrails.py`
