# Learning, MoA, RL, And Voice

## Learning Loop

Implementation: `learning_loop.py`.

Routes:

- `/v1/learning/status`
- `/v1/learning/review`
- `/v1/learning/apply`

Tools:

- `agent_learning_status`
- `agent_learning_review`
- `agent_learning_apply`
- `agent_skill_reflect`

Learning may create pending memory or skill proposals. Applying proposals must remain explicit and auditable.

## Mixture Of Agents

Implementation: `moa.py`.

Tool:

- `agent_moa`

MoA uses configured reference models and an aggregator model through the current model client. Preserve per-reference failure reporting and aggregation metadata.

## RL Atropos

Implementation: `rl_atropos.py`.

Routes:

- `/v1/rl/environments`
- `/v1/rl/config`
- `/v1/rl/runs`
- `/v1/rl/runs/{run_id}`
- `/v1/rl/runs/{run_id}/stop`
- `/v1/rl/runs/{run_id}/results`
- `/v1/rl/runs/{run_id}/logs`

Tools include environment listing/selection, config get/edit, training start/status/stop/results, run list, and inference test.

Training and inference may depend on optional external commands/services. Missing backends should be explicit, not hidden.

## Voice

Implementation: `voice.py`.

Voice config supports OpenAI, iFlytek, and local branches depending on STT/TTS provider settings. Some branches are placeholders or external dependency paths; preserve explicit status/errors rather than implying full implementation.
