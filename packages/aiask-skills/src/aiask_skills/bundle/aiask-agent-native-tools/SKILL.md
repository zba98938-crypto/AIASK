---
name: aiask-agent-native-tools
description: Use this skill when working on AIASK Agent `general_full` native tools such as file read/write/patch/search, terminal/process/code execution, browser automation/CDP, web search/extract, image/vision/audio/video tools, todos/subgoals, jobs/cron, terminal backends, process registry, TUI status, security scanning, or native capability parity.
version: 1.0.0
protocol_version: "1.0"
capabilities:
  - agent_tools.develop
required_mcp: []
side_effect: read_only
---

# AIASK Agent Native Tools

## Workflow

1. Read [references/general-full-native-tools.md](references/general-full-native-tools.md) before changing file, terminal, process, browser, web, media, todo, or code-execution tools.
2. Read [references/jobs-security-and-backends.md](references/jobs-security-and-backends.md) before changing jobs, cron, process registry, terminal backends, security scan, TUI, or cross-platform behavior.
3. Keep native tools gated by `general_full` policy and control-token UX.
4. Preserve `agent_*` names, JSON schemas, envelopes, and side-effect metadata.
5. Add negative tests when expanding filesystem, terminal, browser, or external execution permissions.

## Key Files

- `packages/agent/src/aiask_agent/general_tools.py`
- `packages/agent/src/aiask_agent/native_capabilities.py`
- `packages/agent/src/aiask_agent/tools/catalog.py`
- `packages/agent/src/aiask_agent/tools/schemas.py`
- `packages/agent/src/aiask_agent/scheduler.py`
- `packages/agent/src/aiask_agent/terminal_backends.py`
- `packages/agent/src/aiask_agent/security.py`
- `packages/agent/src/aiask_agent/process_registry.py`
