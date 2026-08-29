from __future__ import annotations

from typing import Any

HERMES_BASELINE_VERSION = "0.16.0"
HERMES_RELEASE_TAG = "v2026.6.5"
HERMES_BASELINE = f"Hermes v{HERMES_BASELINE_VERSION} full runtime capability reference"
HERMES_CORE_PARITY_TRACK = "core_runtime"
HERMES_V014_DELTA_TRACK = "v0.14_delta"
HERMES_V014_DELTA_BASELINE = "Hermes v0.14.0 full runtime capability reference"
HERMES_V014_DELTA_RELEASE_TAG = "v2026.5.16"
HERMES_V016_DELTA_TRACK = "v0.16_delta"
HERMES_V016_DELTA_BASELINE = "Hermes v0.16.0 Surface Release capability reference"
HERMES_V016_DELTA_RELEASE_TAG = "v2026.6.5"

HERMES_TOOL_EQUIVALENTS: tuple[dict[str, Any], ...] = (
    {"hermes_tool": "browser_cdp", "aiask_tools": ["agent_browser_cdp"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_navigate", "aiask_tools": ["agent_browser_navigate"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_snapshot", "aiask_tools": ["agent_browser_snapshot"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_click", "aiask_tools": ["agent_browser_click"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_type", "aiask_tools": ["agent_browser_type"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_scroll", "aiask_tools": ["agent_browser_scroll"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_back", "aiask_tools": ["agent_browser_back"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_press", "aiask_tools": ["agent_browser_press"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_get_images", "aiask_tools": ["agent_browser_get_images"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_vision", "aiask_tools": ["agent_browser_vision"], "area": "browser", "live_required": False},
    {"hermes_tool": "browser_console", "aiask_tools": ["agent_browser_console"], "area": "browser", "live_required": False},
    {
        "hermes_tool": "computer_use",
        "aiask_tools": ["agent_computer_use"],
        "area": "computer_use",
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "notes": "AIASK exposes a gated browser-session computer-use facade without unrestricted OS desktop control.",
    },
    {"hermes_tool": "clarify", "aiask_tools": ["agent_clarify"], "area": "interaction", "live_required": False},
    {"hermes_tool": "execute_code", "aiask_tools": ["agent_execute_python"], "area": "code", "live_required": False},
    {"hermes_tool": "cronjob", "aiask_tools": ["agent_cronjob"], "area": "automation", "live_required": False},
    {"hermes_tool": "delegate_task", "aiask_tools": ["agent_delegate_task"], "area": "delegation", "live_required": False},
    {"hermes_tool": "discord_server", "aiask_tools": ["agent_discord_server"], "area": "platform", "live_env": ["DISCORD_BOT_TOKEN"]},
    {"hermes_tool": "feishu_doc_read", "aiask_tools": ["agent_feishu_doc_read"], "area": "platform", "live_env": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]},
    {"hermes_tool": "feishu_drive_list_comments", "aiask_tools": ["agent_feishu_drive_list_comments"], "area": "platform", "live_env": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]},
    {"hermes_tool": "feishu_drive_list_comment_replies", "aiask_tools": ["agent_feishu_drive_list_comment_replies"], "area": "platform", "live_env": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]},
    {"hermes_tool": "feishu_drive_reply_comment", "aiask_tools": ["agent_feishu_drive_reply_comment"], "area": "platform", "live_env": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]},
    {"hermes_tool": "feishu_drive_add_comment", "aiask_tools": ["agent_feishu_drive_add_comment"], "area": "platform", "live_env": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]},
    {"hermes_tool": "read_file", "aiask_tools": ["agent_file_read"], "area": "file", "live_required": False},
    {"hermes_tool": "write_file", "aiask_tools": ["agent_file_write"], "area": "file", "live_required": False},
    {"hermes_tool": "patch", "aiask_tools": ["agent_file_patch"], "area": "file", "live_required": False},
    {"hermes_tool": "search_files", "aiask_tools": ["agent_file_search"], "area": "file", "live_required": False},
    {"hermes_tool": "ha_list_entities", "aiask_tools": ["agent_ha_list_entities"], "area": "homeassistant", "live_env": ["HASS_URL", "HASS_TOKEN"]},
    {"hermes_tool": "ha_get_state", "aiask_tools": ["agent_ha_get_state"], "area": "homeassistant", "live_env": ["HASS_URL", "HASS_TOKEN"]},
    {"hermes_tool": "ha_list_services", "aiask_tools": ["agent_ha_list_services"], "area": "homeassistant", "live_env": ["HASS_URL", "HASS_TOKEN"]},
    {"hermes_tool": "ha_call_service", "aiask_tools": ["agent_ha_call_service"], "area": "homeassistant", "live_env": ["HASS_URL", "HASS_TOKEN"]},
    {"hermes_tool": "image_generate", "aiask_tools": ["agent_image_generate"], "area": "multimodal", "live_env": ["OPENAI_API_KEY"]},
    {"hermes_tool": "memory", "aiask_tools": ["agent_memory"], "area": "memory", "live_required": False},
    {"hermes_tool": "mixture_of_agents", "aiask_tools": ["agent_moa"], "area": "reasoning", "live_env": ["OPENAI_API_KEY"]},
    {"hermes_tool": "process", "aiask_tools": ["agent_process"], "area": "terminal", "live_required": False},
    {"hermes_tool": "rl_list_environments", "aiask_tools": ["agent_rl_list_environments"], "area": "rl", "live_required": False},
    {"hermes_tool": "rl_select_environment", "aiask_tools": ["agent_rl_select_environment"], "area": "rl", "live_required": False},
    {"hermes_tool": "rl_get_current_config", "aiask_tools": ["agent_rl_get_config"], "area": "rl", "live_required": False},
    {"hermes_tool": "rl_edit_config", "aiask_tools": ["agent_rl_edit_config"], "area": "rl", "live_required": False},
    {"hermes_tool": "rl_start_training", "aiask_tools": ["agent_rl_start_training"], "area": "rl", "live_env": ["TINKER_API_KEY", "WANDB_API_KEY"]},
    {"hermes_tool": "rl_check_status", "aiask_tools": ["agent_rl_check_status"], "area": "rl", "live_env": ["TINKER_API_KEY", "WANDB_API_KEY"]},
    {"hermes_tool": "rl_stop_training", "aiask_tools": ["agent_rl_stop_training"], "area": "rl", "live_env": ["TINKER_API_KEY", "WANDB_API_KEY"]},
    {"hermes_tool": "rl_get_results", "aiask_tools": ["agent_rl_get_results"], "area": "rl", "live_env": ["TINKER_API_KEY", "WANDB_API_KEY"]},
    {"hermes_tool": "rl_list_runs", "aiask_tools": ["agent_rl_list_runs"], "area": "rl", "live_required": False},
    {"hermes_tool": "rl_test_inference", "aiask_tools": ["agent_rl_test_inference"], "area": "rl", "live_env": ["AIASK_RL_INFERENCE_URL"]},
    {"hermes_tool": "send_message", "aiask_tools": ["agent_message_send"], "area": "delivery", "live_env": ["AIASK_GATEWAY_WEBHOOK_URL"]},
    {"hermes_tool": "session_search", "aiask_tools": ["agent_session_search"], "area": "memory", "live_required": False},
    {"hermes_tool": "skill_manage", "aiask_tools": ["agent_skill_manage"], "area": "skills", "live_required": False},
    {"hermes_tool": "skills_list", "aiask_tools": ["agent_skill_list"], "area": "skills", "live_required": False},
    {"hermes_tool": "skill_view", "aiask_tools": ["agent_skill_view"], "area": "skills", "live_required": False},
    {"hermes_tool": "terminal", "aiask_tools": ["agent_terminal"], "area": "terminal", "live_required": False},
    {"hermes_tool": "todo", "aiask_tools": ["agent_todo"], "area": "planning", "live_required": False},
    {"hermes_tool": "text_to_speech", "aiask_tools": ["agent_text_to_speech"], "area": "multimodal", "live_env": ["OPENAI_API_KEY"]},
    {"hermes_tool": "transcribe_audio", "aiask_tools": ["agent_transcribe_audio"], "area": "multimodal", "live_env": ["OPENAI_API_KEY"]},
    {"hermes_tool": "vision_analyze", "aiask_tools": ["agent_vision_analyze"], "area": "multimodal", "live_env": ["OPENAI_API_KEY"]},
    {
        "hermes_tool": "video_generate",
        "aiask_tools": ["agent_video_generate"],
        "area": "multimodal",
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "live_env": ["AIASK_VIDEO_API_URL", "AIASK_VIDEO_API_KEY"],
        "notes": "AIASK exposes a provider-gated video generation facade with safe unconfigured status.",
    },
    {"hermes_tool": "web_search", "aiask_tools": ["agent_web_search"], "area": "web", "live_required": False},
    {"hermes_tool": "web_extract", "aiask_tools": ["agent_web_extract"], "area": "web", "live_required": False},
    {
        "hermes_tool": "x_search",
        "aiask_tools": ["agent_x_search"],
        "area": "web",
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "live_env": ["X_BEARER_TOKEN|X_API_KEY"],
        "notes": "AIASK exposes an X/Twitter search facade gated by X_BEARER_TOKEN or X_API_KEY.",
    },
)

HERMES_GATEWAY_PLATFORM_EQUIVALENTS: tuple[dict[str, Any], ...] = (
    {"platform": "api_server", "aiask_adapter": "api_server", "live_required": False},
    {"platform": "bluebubbles", "aiask_adapter": "bluebubbles", "live_env": ["BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"]},
    {"platform": "dingtalk", "aiask_adapter": "dingtalk", "live_env": ["DINGTALK_BOT_WEBHOOK"]},
    {"platform": "discord", "aiask_adapter": "discord", "live_env": ["DISCORD_BOT_TOKEN"]},
    {"platform": "email", "aiask_adapter": "email", "live_env": ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"]},
    {"platform": "feishu", "aiask_adapter": "feishu", "live_env": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]},
    {"platform": "homeassistant", "aiask_adapter": "homeassistant", "live_env": ["HASS_URL", "HASS_TOKEN"]},
    {"platform": "matrix", "aiask_adapter": "matrix", "live_env": ["MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN"]},
    {"platform": "mattermost", "aiask_adapter": "mattermost", "live_env": ["MATTERMOST_URL", "MATTERMOST_TOKEN"]},
    {"platform": "signal", "aiask_adapter": "signal", "live_env": ["SIGNAL_CLI_PATH"]},
    {"platform": "slack", "aiask_adapter": "slack", "live_env": ["SLACK_BOT_TOKEN"]},
    {"platform": "sms", "aiask_adapter": "sms", "live_env": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"]},
    {"platform": "telegram", "aiask_adapter": "telegram", "live_env": ["TELEGRAM_BOT_TOKEN"]},
    {
        "platform": "line",
        "aiask_adapter": "line",
        "live_env": ["LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
    },
    {
        "platform": "simplex",
        "aiask_adapter": "simplex",
        "live_env": ["SIMPLEX_CLI_PATH"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
    },
    {
        "platform": "teams",
        "aiask_adapter": "teams",
        "live_env": ["MSGRAPH_TENANT_ID", "MSGRAPH_CLIENT_ID", "MSGRAPH_CLIENT_SECRET"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
    },
    {"platform": "webhook", "aiask_adapter": "webhook", "live_env": ["AIASK_GATEWAY_WEBHOOK_URL"]},
    {"platform": "wecom", "aiask_adapter": "wecom", "live_env": ["WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET"]},
    {"platform": "wecom_callback", "aiask_adapter": "wecom_callback", "live_env": ["WECOM_TOKEN", "WECOM_ENCODING_AES_KEY"]},
    {"platform": "weixin", "aiask_adapter": "weixin", "live_env": ["WEIXIN_APP_ID", "WEIXIN_APP_SECRET"]},
    {"platform": "whatsapp", "aiask_adapter": "whatsapp", "live_env": ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"]},
    {"platform": "qqbot", "aiask_adapter": "qqbot", "live_env": ["QQBOT_APP_ID", "QQBOT_TOKEN"]},
)


HERMES_FINANCIAL_PRODUCT_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {"reference": "web_search", "aiask": ["agent_web_search"], "area": "web", "required": True},
    {"reference": "web_extract", "aiask": ["agent_web_extract"], "area": "web", "required": True},
    {"reference": "terminal", "aiask": ["agent_terminal"], "area": "terminal", "required": True},
    {"reference": "process", "aiask": ["agent_process"], "area": "terminal", "required": True},
    {"reference": "read_file", "aiask": ["agent_file_read"], "area": "file", "required": True},
    {"reference": "write_file", "aiask": ["agent_file_write"], "area": "file", "required": True},
    {"reference": "patch", "aiask": ["agent_file_patch"], "area": "file", "required": True},
    {"reference": "search_files", "aiask": ["agent_file_search"], "area": "file", "required": True},
    {"reference": "vision_analyze", "aiask": ["agent_vision_analyze"], "area": "multimodal", "required": True},
    {"reference": "image_generate", "aiask": ["agent_image_generate"], "area": "multimodal", "required": True},
    {"reference": "transcribe_audio", "aiask": ["agent_transcribe_audio"], "area": "multimodal", "required": True},
    {"reference": "text_to_speech", "aiask": ["agent_text_to_speech"], "area": "multimodal", "required": True},
    {"reference": "skills_list", "aiask": ["agent_skill_list"], "area": "skills", "required": True},
    {"reference": "skill_view", "aiask": ["agent_skill_view"], "area": "skills", "required": True},
    {"reference": "skill_manage", "aiask": ["agent_skill_manage"], "area": "skills", "required": True},
    {"reference": "plugin_hooks", "aiask": ["agent_plugin_manage"], "area": "plugins", "required": True},
    {"reference": "browser_navigate", "aiask": ["agent_browser_navigate"], "area": "browser", "required": True},
    {"reference": "browser_snapshot", "aiask": ["agent_browser_snapshot"], "area": "browser", "required": True},
    {"reference": "browser_click", "aiask": ["agent_browser_click"], "area": "browser", "required": True},
    {"reference": "browser_type", "aiask": ["agent_browser_type"], "area": "browser", "required": True},
    {"reference": "browser_scroll", "aiask": ["agent_browser_scroll"], "area": "browser", "required": True},
    {"reference": "browser_back", "aiask": ["agent_browser_back"], "area": "browser", "required": True},
    {"reference": "browser_press", "aiask": ["agent_browser_press"], "area": "browser", "required": True},
    {"reference": "browser_get_images", "aiask": ["agent_browser_get_images"], "area": "browser", "required": True},
    {"reference": "browser_vision", "aiask": ["agent_browser_vision"], "area": "browser", "required": True},
    {"reference": "browser_console", "aiask": ["agent_browser_console"], "area": "browser", "required": True},
    {"reference": "browser_cdp", "aiask": ["agent_browser_cdp"], "area": "browser", "required": True},
    {"reference": "todo", "aiask": ["agent_todo"], "area": "planning", "required": True},
    {"reference": "memory", "aiask": ["agent_memory"], "area": "memory", "required": True},
    {"reference": "session_search", "aiask": ["agent_session_search"], "area": "memory", "required": True},
    {"reference": "clarify", "aiask": ["agent_clarify"], "area": "interaction", "required": True},
    {"reference": "execute_code", "aiask": ["agent_execute_python"], "area": "code", "required": True},
    {"reference": "delegate_task", "aiask": ["agent_delegate_task"], "area": "delegation", "required": True},
    {"reference": "cronjob", "aiask": ["agent_cronjob"], "area": "automation", "required": True},
    {"reference": "webhook", "aiask": ["agent_webhook"], "area": "automation", "required": True},
    {"reference": "send_message", "aiask": ["agent_message_send"], "area": "delivery", "required": True},
    {"reference": "mcp_client", "aiask": ["agent_mcp_manage"], "area": "mcp", "required": True},
    {"reference": "model_provider_registry", "aiask": ["agent_model_manage"], "area": "models", "required": True},
    {"reference": "credential_pool_rotation", "aiask": ["agent_model_manage"], "area": "models", "required": True},
    {"reference": "memory_provider", "aiask": ["agent_memory_manage"], "area": "memory", "required": True},
    {"reference": "acp_client_mcp_servers", "aiask": ["agent_acp_manage"], "area": "acp", "required": True},
    {"reference": "security_redaction_scan", "aiask": ["agent_security_scan"], "area": "security", "required": True},
    {"reference": "skill_packs", "aiask": ["agent_skill_pack_manage"], "area": "skills", "required": True},
    {"reference": "tui", "aiask": ["agent_tui_status"], "area": "interface", "required": True},
    {"reference": "terminal_backends", "aiask": ["agent_terminal_backends"], "area": "terminal", "required": True},
    {"reference": "platform_gateway", "aiask": ["agent_gateway_status", "agent_gateway_send_message", "agent_gateway_directory", "agent_gateway_direct_deliver"], "area": "delivery", "required": True},
    {"reference": "domestic_gateway_platforms", "aiask": ["agent_gateway_platforms"], "area": "delivery", "required": True},
    {"reference": "global_gateway_platforms", "aiask": ["agent_gateway_platforms", "agent_discord_channel_send"], "area": "delivery", "required": True},
    {"reference": "learning_loop", "aiask": ["agent_learning_status", "agent_learning_review", "agent_learning_apply", "agent_skill_reflect"], "area": "learning", "required": True},
    {"reference": "homeassistant", "aiask": ["agent_ha_list_entities", "agent_ha_get_state", "agent_ha_list_services", "agent_ha_list_events", "agent_ha_list_registry", "agent_ha_call_service"], "area": "homeassistant", "required": True, "live_env": ["HASS_URL", "HASS_TOKEN"]},
    {"reference": "rl_training", "aiask": ["agent_rl_list_environments", "agent_rl_start_training", "agent_rl_check_status", "agent_rl_stop_training"], "area": "rl", "required": True, "live_env": ["TINKER_API_KEY", "WANDB_API_KEY"]},
    {"reference": "atropos_config", "aiask": ["agent_rl_get_config", "agent_rl_edit_config", "agent_rl_test_inference"], "area": "rl", "required": True},
    {"reference": "moa", "aiask": ["agent_moa"], "area": "reasoning", "required": True, "live_env": ["OPENAI_API_KEY"]},
    {"reference": "feishu_doc", "aiask": ["agent_feishu_doc_read"], "area": "platform", "required": True, "live_env": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]},
    {"reference": "feishu_drive", "aiask": ["agent_feishu_drive_list_comments", "agent_feishu_drive_list_comment_replies", "agent_feishu_drive_reply_comment", "agent_feishu_drive_add_comment"], "area": "platform", "required": True, "live_env": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]},
    {"reference": "discord", "aiask": ["agent_discord_channel_send", "agent_discord_server"], "area": "platform", "required": True, "live_env": ["DISCORD_BOT_TOKEN"]},
    {"reference": "dynamic_mcp_tools", "aiask": ["agent_mcp_manage"], "area": "mcp", "required": True},
    {"reference": "dynamic_plugin_tools", "aiask": ["agent_plugin_manage"], "area": "plugins", "required": True},
)


HERMES_NATIVE_FEATURE_EQUIVALENTS: tuple[dict[str, Any], ...] = (
    {
        "feature": "feature_ledger",
        "area": "parity",
        "aiask_tools": ["agent_tool_catalog"],
        "description": "Feature-level parity/readiness ledger shared by status and readiness APIs.",
    },
    {
        "feature": "gateway_channel_directory",
        "area": "delivery",
        "aiask_tools": ["agent_gateway_directory"],
        "description": "Platform/channel/user directory with list, resolve, and refresh actions.",
    },
    {
        "feature": "gateway_direct_delivery",
        "area": "delivery",
        "aiask_tools": ["agent_gateway_direct_deliver", "agent_gateway_send_message"],
        "description": "Direct platform delivery path that bypasses the model queue.",
    },
    {
        "feature": "gateway_inbound_controls",
        "area": "delivery",
        "aiask_tools": ["agent_gateway_history", "agent_gateway_pairing"],
        "description": "Inbound deduplication, slash command detection, approval callbacks, and message records.",
    },
    {
        "feature": "gateway_media_parity",
        "area": "delivery",
        "aiask_tools": ["agent_gateway_send_message"],
        "description": "MEDIA tag extraction, adapter upload hooks, and failed/unsupported persistence.",
    },
    {
        "feature": "gateway_runtime_health",
        "area": "delivery",
        "aiask_tools": ["agent_gateway_status", "agent_gateway_platforms"],
        "description": "Adapter lifecycle, runtime lock/status, and credential health reporting.",
    },
    {
        "feature": "plugin_runners_and_hooks",
        "area": "plugins",
        "aiask_tools": ["agent_plugin_manage"],
        "description": "Plugin tool runners plus pre/post LLM, tool, session, terminal, and transform hooks.",
    },
    {
        "feature": "plugin_commands",
        "area": "plugins",
        "aiask_tools": ["agent_plugin_manage"],
        "description": "Manifest commands registered as agent_plugin_* command tools and testable through APIs.",
    },
    {
        "feature": "model_provider_registry",
        "area": "models",
        "aiask_tools": ["agent_model_manage"],
        "description": "Provider inventory, OpenAI-compatible configuration, fallback order, credential pool metadata, and attempt classification.",
    },
    {
        "feature": "credential_pool_rotation",
        "area": "models",
        "aiask_tools": ["agent_model_manage"],
        "description": "Credential pool status and least-recent-failure selection without exposing sensitive values.",
    },
    {
        "feature": "pluggable_memory_providers",
        "area": "memory",
        "aiask_tools": ["agent_memory_manage"],
        "description": "SQLite default memory provider with Honcho, vector, and custom provider readiness slots.",
    },
    {
        "feature": "acp_client_mcp_servers",
        "area": "acp",
        "aiask_tools": ["agent_acp_manage"],
        "description": "ACP client-provided MCP server registration and status through AIASK-native MCP aggregation.",
    },
    {
        "feature": "security_redaction_scan",
        "area": "security",
        "aiask_tools": ["agent_security_scan"],
        "description": "Sensitive-token pattern, protected path, private URL, and environment key scanning with redacted outputs.",
    },
    {
        "feature": "skill_pack_governance",
        "area": "skills",
        "aiask_tools": ["agent_skill_pack_manage"],
        "description": "AIASK-native rewritten Hermes-class skill pack listing, install, and audit without copying vendor skill text.",
    },
    {
        "feature": "tui_controller",
        "area": "interface",
        "aiask_tools": ["agent_tui_status"],
        "description": "Testable TUI controller for slash commands, state, SSE, approvals, and resume.",
    },
    {
        "feature": "terminal_self_protection",
        "area": "terminal",
        "aiask_tools": ["agent_terminal", "agent_process"],
        "description": "Terminal/process guard rails for AIASK server and gateway processes.",
    },
    {
        "feature": "terminal_watch_notify",
        "area": "terminal",
        "aiask_tools": ["agent_terminal", "agent_process"],
        "description": "Process watch snapshots and optional gateway notification on background completion.",
    },
    {
        "feature": "rl_native_scaffold_runner",
        "area": "rl",
        "aiask_tools": ["agent_rl_list_environments", "agent_rl_start_training", "agent_rl_check_status", "agent_rl_get_results"],
        "live_env": ["TINKER_API_KEY", "WANDB_API_KEY"],
        "description": "Native Atropos-style environment discovery, locked config, subprocess launch, logs, stop, and results.",
    },
    {
        "feature": "desktop_full_mode_readiness",
        "area": "desktop",
        "aiask_tools": ["agent_gateway_status", "agent_tui_status", "agent_terminal_backends"],
        "description": "Feature-level readiness visible to desktop Full Mode without treating missing live credentials as failure.",
    },
    {
        "feature": "desktop_capability_center",
        "area": "desktop",
        "aiask_tools": ["agent_tool_catalog", "agent_gateway_status", "agent_model_manage"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "description": "AIASK Desktop exposes a capability/readiness center backed by Agent HTTP. It is Tauri/React rather than Hermes Electron.",
    },
    {
        "feature": "desktop_native_self_update",
        "area": "desktop",
        "aiask_tools": ["agent_tool_catalog"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "partial",
        "description": "AIASK has a native desktop workbench and capability status surfaces, but no Hermes-style in-app installer/self-update lifecycle is implemented in Agent capability code.",
    },
    {
        "feature": "web_admin_control_surfaces",
        "area": "desktop",
        "aiask_tools": ["agent_mcp_manage", "agent_plugin_manage", "agent_gateway_status", "agent_memory_manage", "agent_model_manage"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "partial",
        "description": "AIASK covers the same admin domains through Desktop pages and Agent HTTP, but does not ship Hermes' separate web dashboard/OIDC surface.",
    },
    {
        "feature": "openai_compatible_http_api",
        "area": "api",
        "aiask_tools": ["agent_model_manage"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "description": "Agent exposes OpenAI-compatible responses/chat routes plus model-provider inspection through the AIASK provider registry.",
    },
    {
        "feature": "remote_gateway_connection_profiles",
        "area": "delivery",
        "aiask_tools": ["agent_gateway_status", "agent_gateway_directory", "agent_gateway_pairing"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "partial",
        "description": "AIASK has gateway adapters, pairing, directory, and delivery state; Hermes-style secure remote desktop connection profiles with OAuth/username-password WebSocket are not implemented.",
    },
    {
        "feature": "gateway_platform_breadth_v016",
        "area": "delivery",
        "aiask_tools": ["agent_gateway_platforms", "agent_gateway_send_message"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "partial",
        "description": "AIASK covers major Hermes messaging channels plus China-focused adapters; Google Chat, ntfy, Yuanbao, and exact Hermes naming parity remain gaps.",
    },
    {
        "feature": "model_picker_profiles_and_fallback",
        "area": "models",
        "aiask_tools": ["agent_model_manage"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "implemented",
        "description": "AIASK tracks provider inventory, fallback order, credential pools, OpenAI-compatible endpoints, Desktop provider presets, model-list fallback, and fuzzy provider/model filtering in the model workspace.",
    },
    {
        "feature": "prompt_caching_controls",
        "area": "models",
        "aiask_tools": ["agent_model_manage"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "implemented",
        "description": "AIASK exposes prompt-cache policy/status through AI config, readiness/provider status, and agent_model_manage; Anthropic Messages requests apply cache_control markers to the system prompt and recent non-system messages when enabled.",
    },
    {
        "feature": "tool_gateway_portal_setup",
        "area": "tools",
        "aiask_tools": ["agent_model_manage", "agent_web_search", "agent_image_generate", "agent_text_to_speech", "agent_browser_navigate"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "partial",
        "description": "AIASK exposes provider-gated web, browser, image, and TTS tools; it intentionally uses explicit provider credentials rather than a Nous Portal quick-setup gateway.",
    },
    {
        "feature": "browser_backend_matrix",
        "area": "browser",
        "aiask_tools": ["agent_browser_navigate", "agent_browser_snapshot", "agent_browser_cdp", "agent_browser_vision"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "partial",
        "description": "AIASK has local browser/CDP and vision-oriented browser tools; Browserbase, Browser Use, Firecrawl, Camofox, and cloud-browser breadth are not all native adapters.",
    },
    {
        "feature": "session_archive_search_and_links",
        "area": "session",
        "aiask_tools": ["agent_session_search", "agent_session_handoff"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "partial",
        "description": "AIASK supports session search, handoff records, control-token gated archive/unarchive APIs, default archived-session filtering, include-archived search/list flags, and Desktop archive/restore controls. Cross-profile links and full Hermes desktop session-management parity remain partial.",
    },
    {
        "feature": "undo_last_turns",
        "area": "session",
        "aiask_tools": ["agent_tui_status"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "implemented",
        "description": "AIASK implements Hermes-style '/undo [N]' for session context through a control-token gated soft-delete API and TUI command. External side effects, run events, approvals, and tool audit evidence are intentionally not rolled back.",
    },
    {
        "feature": "checkpoint_and_rollback",
        "area": "file",
        "aiask_tools": ["agent_file_write", "agent_file_patch", "agent_file_checkpoint", "agent_file_rollback", "agent_file_mutation_verify"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "implemented",
        "description": "AIASK creates pre-change checkpoints for workspace file writes/patches and exposes checkpoint/rollback tools for local file restoration. External side effects and non-file state are intentionally out of scope.",
    },
    {
        "feature": "context_reference_files_and_urls",
        "area": "context",
        "aiask_tools": ["agent_file_read", "agent_file_search", "agent_web_extract"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "implemented",
        "description": "AIASK injects Hermes-style context references into model turns: project roots auto-load SOUL.md, .hermes.md/HERMES.md, AGENTS.md, CLAUDE.md, and .cursorrules, while user prompts can include @file:/@path: workspace files and @url: public HTTP(S) references. URL targets keep SSRF/private-network guardrails and all resolved references are persisted as sources/artifacts.",
    },
    {
        "feature": "cron_jobs_with_skills_and_scripts",
        "area": "automation",
        "aiask_tools": ["agent_cronjob", "agent_job_create", "agent_job_run"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "description": "AIASK supports cron/job creation, pause/resume/remove/trigger, and skill-aware scheduled prompts/scripts through the native scheduler.",
    },
    {
        "feature": "batch_trajectory_generation",
        "area": "evals",
        "aiask_tools": ["agent_job_create", "agent_job_run", "agent_cronjob"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "partial",
        "description": "AIASK jobs can run scheduled/background work, but Hermes' batch trajectory generation/compression workflow for evals/training is not a first-class runtime feature.",
    },
    {
        "feature": "external_memory_provider_catalog_breadth",
        "area": "memory",
        "aiask_tools": ["agent_memory_manage", "agent_memory", "agent_session_search"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "implemented",
        "description": "AIASK has durable SQLite memory plus an explicit Hermes external-provider catalog for Honcho, OpenViking, Mem0, Hindsight, Holographic, RetainDB, ByteRover, and Supermemory. The catalog exposes required env names, configured/live-unverified state, and audit warnings without leaking secrets; third-party provider sync remains opt-in/live-unverified when credentials exist.",
    },
    {
        "feature": "dashboard_auth_oidc_username_password",
        "area": "security",
        "aiask_tools": ["agent_security_scan", "agent_gateway_status"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "partial",
        "description": "AIASK uses control-token/API guardrails and redaction scans; Hermes-style web dashboard OIDC plus username/password authentication is not a native Agent surface.",
    },
    {
        "feature": "media_provider_catalog_breadth",
        "area": "multimodal",
        "aiask_tools": ["agent_media_provider_catalog", "agent_image_generate", "agent_text_to_speech", "agent_transcribe_audio", "agent_video_generate"],
        "introduced_in": "0.16.0",
        "parity_track": HERMES_V016_DELTA_TRACK,
        "status": "implemented",
        "description": "AIASK exposes a read-only media provider catalog covering vision, image generation, video generation, TTS, STT, local/dependency-backed voice providers, required env names, configured states, and live-unverified semantics alongside the gated media tools.",
    },
    {
        "feature": "openai_compatible_local_proxy",
        "area": "models",
        "aiask_tools": ["agent_model_manage"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "status": "excluded_by_design",
        "excluded_reason": "AIASK uses an explicit credential pool with API keys; an OAuth-subscription local proxy is intentionally out of scope to avoid Claude Pro / ChatGPT Pro / SuperGrok TOS conflicts.",
        "description": "AIASK has OpenAI-compatible response/chat routes and model management; a Hermes v0.14-style local proxy for OAuth subscription providers is intentionally excluded by AIASK design.",
    },
    {
        "feature": "oauth_subscription_providers",
        "area": "models",
        "aiask_tools": ["agent_model_manage"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "status": "excluded_by_design",
        "excluded_reason": "AIASK ships a credential pool, fallback ordering, and least-recent-failure rotation; Claude Pro / ChatGPT Pro / SuperGrok OAuth subscription flows are intentionally out of scope.",
        "description": "AIASK tracks provider pools and credentials but intentionally does not implement Hermes v0.14 OAuth subscription proxying.",
    },
    {
        "feature": "run_approval_events",
        "area": "approval",
        "aiask_tools": ["agent_terminal", "agent_tui_status"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "description": "Long-running AIASK runs emit approval.pending events through stored run events and SSE/TUI reducers.",
    },
    {
        "feature": "vision_pixels_to_model",
        "area": "multimodal",
        "aiask_tools": ["agent_vision_analyze"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "live_env": ["OPENAI_API_KEY"],
        "description": "AIASK vision passes image URLs/base64 inputs to OpenAI Responses when a vision model is configured.",
    },
    {
        "feature": "write_time_lsp_diagnostics",
        "area": "file",
        "aiask_tools": ["agent_file_write", "agent_file_patch", "agent_file_mutation_verify"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "description": "AIASK file writes and patches run native syntax diagnostics for Python (py_compile), JSON, TOML, and YAML on disk. Cross-language LSP coverage (TypeScript/JS/Go/Rust) intentionally relies on the user's external LSP and is not bundled.",
    },
    {
        "feature": "per_turn_file_mutation_verifier",
        "area": "file",
        "aiask_tools": ["agent_file_mutation_verify"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "description": "AIASK verifies actual disk mutations after file writes and patches with stat, workspace, and sha256 evidence.",
    },
    {
        "feature": "pluggable_video_generation",
        "area": "multimodal",
        "aiask_tools": ["agent_video_generate"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "live_env": ["AIASK_VIDEO_API_URL", "AIASK_VIDEO_API_KEY"],
        "description": "AIASK exposes a provider-gated video generation facade with create and status-check actions.",
    },
    {
        "feature": "computer_use_backend",
        "area": "computer_use",
        "aiask_tools": ["agent_computer_use"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "description": "AIASK exposes a gated browser-session computer-use backend and intentionally excludes unrestricted OS control.",
    },
    {
        "feature": "x_search_oauth_tool",
        "area": "web",
        "aiask_tools": ["agent_x_search"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "live_env": ["X_BEARER_TOKEN|X_API_KEY"],
        "description": "AIASK exposes an X/Twitter search tool gated by X_BEARER_TOKEN or X_API_KEY.",
    },
    {
        "feature": "live_session_handoff",
        "area": "session",
        "aiask_tools": ["agent_session_handoff"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "description": "AIASK records native session handoff requests and lifecycle status in the session store.",
    },
    {
        "feature": "subgoal_control",
        "area": "planning",
        "aiask_tools": ["agent_subgoal", "agent_todo", "agent_tui_status"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "description": "AIASK exposes a dedicated session-scoped subgoal and criteria API alongside todos and run steering.",
    },
    {
        "feature": "lazy_dependency_and_supply_chain_checks",
        "area": "runtime",
        "aiask_tools": ["agent_security_scan"],
        "introduced_in": "0.14.0",
        "parity_track": HERMES_V014_DELTA_TRACK,
        "description": "AIASK ships an offline supply-chain advisory ledger (data/known_advisories.json) and exposes it through agent_security_scan(action=\"dependency_advisory\"); installed and currently-loaded packages are matched against curated CVE/EOL constraints without contacting the network.",
    },
)


def _live_status(live_env: list[str], environ: dict[str, str]) -> tuple[str, str]:
    if not live_env:
        return "not_required", "implemented"
    groups: dict[str, list[str]] = {}
    for key in live_env:
        if "|" in key:
            groups[key] = [part for part in key.split("|") if part]
    if groups:
        ready = all(any(str(environ.get(part, "")).strip() for part in parts) for parts in groups.values())
        plain = [key for key in live_env if "|" not in key]
        ready = ready and all(str(environ.get(key, "")).strip() for key in plain)
    else:
        ready = all(str(environ.get(key, "")).strip() for key in live_env)
    return ("ready", "implemented") if ready else ("skipped_missing_credentials", "live_unverified")


def _env_ready(live_env: list[str], environ: dict[str, str]) -> bool:
    return _live_status(live_env, environ)[0] == "ready"


def capability_matrix(registered_names: set[str] | list[str] | tuple[str, ...], *, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    names = {str(item) for item in registered_names}
    environ = dict(env or {})
    items: list[dict[str, Any]] = []
    for item in HERMES_FINANCIAL_PRODUCT_CAPABILITIES:
        mapped = list(item.get("aiask") or [])
        missing = [name for name in mapped if name not in names]
        live_env = list(item.get("live_env") or [])
        live_status, ready_status = _live_status(live_env, environ)
        status = "blocked" if missing or not mapped else ready_status
        items.append(
            {
                "reference": item["reference"],
                "area": item["area"],
                "required": bool(item.get("required", True)),
                "aiask_tools": mapped,
                "missing_aiask_tools": missing,
                "code_status": "present" if not missing else "missing",
                "mock_status": "passed" if not missing else "blocked",
                "live_status": live_status,
                "required_env": live_env,
                "status": status,
            }
        )
    return items


def hermes_native_feature_parity(registered_names: set[str] | list[str] | tuple[str, ...], *, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    names = {str(item) for item in registered_names}
    environ = dict(env or {})
    features: list[dict[str, Any]] = []
    for item in HERMES_NATIVE_FEATURE_EQUIVALENTS:
        mapped = list(item.get("aiask_tools") or [])
        missing = [name for name in mapped if name not in names]
        live_env = list(item.get("live_env") or [])
        live_status, ready_status = _live_status(live_env, environ)
        code_status = "present" if not missing else "missing"
        mock_status = "passed" if not missing else "blocked"
        declared_status = str(item.get("status") or "").strip()
        if missing:
            status = str(item.get("status_if_missing") or "partial")
        elif declared_status:
            status = declared_status
        else:
            status = ready_status
        if declared_status == "partial" and not missing:
            mock_status = "partial"
        if declared_status == "excluded_by_design" and not missing:
            mock_status = "excluded"
        feature_entry: dict[str, Any] = {
            "feature": item["feature"],
            "area": item["area"],
            "description": item.get("description", ""),
            "aiask_tools": mapped,
            "missing_aiask_tools": missing,
            "code_status": code_status,
            "mock_status": mock_status,
            "live_status": live_status,
            "required_env": live_env,
            "status": status,
            "introduced_in": item.get("introduced_in"),
            "parity_track": item.get("parity_track", HERMES_CORE_PARITY_TRACK),
        }
        if item.get("excluded_reason"):
            feature_entry["excluded_reason"] = item.get("excluded_reason")
        features.append(feature_entry)
    return features


def hermes_tool_parity(registered_names: set[str] | list[str] | tuple[str, ...], *, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    names = {str(item) for item in registered_names}
    environ = dict(env or {})
    items: list[dict[str, Any]] = []
    for item in HERMES_TOOL_EQUIVALENTS:
        mapped = list(item["aiask_tools"])
        missing = [name for name in mapped if name not in names]
        live_env = list(item.get("live_env") or [])
        live_required = bool(item.get("live_required", True)) and bool(live_env)
        live_ready = _env_ready(live_env, environ)
        if missing:
            status = str(item.get("status_if_missing") or "blocked")
            code_status = "missing"
            mock_status = "blocked"
        else:
            code_status = "present"
            mock_status = "passed"
            status = "live_unverified" if live_required and not live_ready else "implemented"
        live_status = "not_required" if not live_required else "ready" if live_ready else "skipped_missing_credentials"
        items.append(
            {
                "hermes_tool": item["hermes_tool"],
                "area": item["area"],
                "aiask_tools": mapped,
                "missing_aiask_tools": missing,
                "code_status": code_status,
                "mock_status": mock_status,
                "live_status": live_status,
                "required_env": live_env,
                "status": status,
                "introduced_in": item.get("introduced_in"),
                "parity_track": item.get("parity_track", HERMES_CORE_PARITY_TRACK),
                "notes": item.get("notes"),
            }
        )
    return items


def gateway_platform_parity(*, adapters: set[str] | list[str] | tuple[str, ...], env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    available = {str(item) for item in adapters}
    environ = dict(env or {})
    items: list[dict[str, Any]] = []
    for item in HERMES_GATEWAY_PLATFORM_EQUIVALENTS:
        live_env = list(item.get("live_env") or [])
        missing_adapter = item["aiask_adapter"] not in available
        live_ready = _env_ready(live_env, environ)
        live_required = bool(live_env)
        status = str(item.get("status_if_missing") or "blocked") if missing_adapter else "implemented"
        items.append(
            {
                "platform": item["platform"],
                "aiask_adapter": item["aiask_adapter"],
                "code_status": "missing" if missing_adapter else "present",
                "mock_status": "blocked" if missing_adapter else "passed",
                "live_status": "not_required" if not live_required else "ready" if live_ready else "skipped_missing_credentials",
                "required_env": live_env,
                "not_integrable_reason": item.get("not_integrable_reason"),
                "status": status,
                "introduced_in": item.get("introduced_in"),
                "parity_track": item.get("parity_track", HERMES_CORE_PARITY_TRACK),
            }
        )
    return items


def _track_summary(
    items: list[dict[str, Any]],
    *,
    track: str,
    baseline: str,
    release_tag: str,
) -> dict[str, Any]:
    track_items = [item for item in items if item.get("parity_track") == track]
    missing = [item for item in track_items if item.get("code_status") == "missing"]
    partial = [
        item
        for item in track_items
        if item.get("status") in {"partial", "live_unverified"} and item.get("code_status") != "missing"
    ]
    excluded = [
        item
        for item in track_items
        if item.get("status") == "excluded_by_design" and item.get("code_status") != "missing"
    ]
    implemented = [
        item
        for item in track_items
        if item.get("status") == "implemented" and item.get("code_status") == "present"
    ]
    return {
        "baseline": baseline,
        "release_tag": release_tag,
        "total": len(track_items),
        "implemented_count": len(implemented),
        "partial_count": len(partial),
        "missing_count": len(missing),
        "excluded_by_design_count": len(excluded),
        "implemented": implemented,
        "partial": partial,
        "missing": missing,
        "excluded_by_design": excluded,
    }


def parity_summary(
    registered_names: set[str] | list[str] | tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    gateway_adapters: set[str] | list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    matrix = capability_matrix(registered_names, env=env)
    required = [item for item in matrix if item["required"]]
    implemented = [item for item in required if item["status"] in {"implemented", "live_unverified"} and not item["missing_aiask_tools"]]
    complete = [item for item in required if item["status"] == "implemented" and not item["missing_aiask_tools"]]
    tool_mapping = hermes_tool_parity(registered_names, env=env)
    platform_mapping = gateway_platform_parity(adapters=gateway_adapters, env=env)
    feature_mapping = hermes_native_feature_parity(registered_names, env=env)
    missing_tools = [item for item in tool_mapping if item["code_status"] == "missing"]
    missing_platforms = [item for item in platform_mapping if item["code_status"] == "missing"]
    missing_features = [item for item in feature_mapping if item["code_status"] == "missing"]
    core_missing_tools = [item for item in missing_tools if item.get("parity_track") == HERMES_CORE_PARITY_TRACK]
    core_missing_platforms = [item for item in missing_platforms if item.get("parity_track") == HERMES_CORE_PARITY_TRACK]
    core_missing_features = [item for item in missing_features if item.get("parity_track") == HERMES_CORE_PARITY_TRACK]
    tracked_items = [*tool_mapping, *platform_mapping, *feature_mapping]
    v014_delta = _track_summary(
        tracked_items,
        track=HERMES_V014_DELTA_TRACK,
        baseline=HERMES_V014_DELTA_BASELINE,
        release_tag=HERMES_V014_DELTA_RELEASE_TAG,
    )
    v016_delta = _track_summary(
        tracked_items,
        track=HERMES_V016_DELTA_TRACK,
        baseline=HERMES_V016_DELTA_BASELINE,
        release_tag=HERMES_V016_DELTA_RELEASE_TAG,
    )
    excluded_features = [item for item in feature_mapping if item.get("status") == "excluded_by_design"]
    live_unverified = [item for item in (*tool_mapping, *platform_mapping, *feature_mapping) if item["status"] == "live_unverified"]
    strict_complete = (
        not missing_tools
        and not missing_platforms
        and not missing_features
        and not live_unverified
        and not v014_delta["partial"]
        and not v016_delta["partial"]
    )
    core_code_status = "present" if not core_missing_tools and not core_missing_platforms and not core_missing_features else "missing"
    code_status = "present" if not missing_tools and not missing_platforms and not missing_features else "missing"
    mock_status = "passed" if code_status == "present" else "blocked"
    live_status = "ready" if strict_complete else "live_unverified" if live_unverified else "blocked"
    implemented_features = [
        item
        for item in feature_mapping
        if item["code_status"] == "present" and item["mock_status"] in {"passed", "partial", "excluded"}
    ]
    return {
        "object": "aiask.capability_parity",
        "baseline": HERMES_BASELINE,
        "baseline_version": HERMES_BASELINE_VERSION,
        "baseline_release_tag": HERMES_RELEASE_TAG,
        "scope": "hermes_full_runtime",
        "legacy_scope": "financial_product_runtime",
        "embedded_vendor_runtime": False,
        "required_count": len(required),
        "covered_count": len(implemented),
        "complete_count": len(complete),
        "coverage_ratio": round(len(implemented) / len(required), 4) if required else 1.0,
        "complete_ratio": round(len(complete) / len(required), 4) if required else 1.0,
        "strict_hermes_tool_count": len(tool_mapping),
        "strict_gateway_platform_count": len(platform_mapping),
        "missing_hermes_tools": missing_tools,
        "missing_gateway_platforms": missing_platforms,
        "missing_features": missing_features,
        "core_missing_hermes_tools": core_missing_tools,
        "core_missing_gateway_platforms": core_missing_platforms,
        "core_missing_features": core_missing_features,
        "implemented_features_count": len(implemented_features),
        "feature_count": len(feature_mapping),
        "live_unverified_count": len(live_unverified),
        "code_status": code_status,
        "core_code_status": core_code_status,
        "mock_status": mock_status,
        "live_status": live_status,
        "strict_status": "complete" if strict_complete else "in_progress",
        "status": "complete" if len(complete) == len(required) and strict_complete else "in_progress",
        "v014_delta": v014_delta,
        "v016_delta": v016_delta,
        "excluded_by_design_count": len(excluded_features),
        "excluded_by_design_features": excluded_features,
        "matrix": matrix,
        "hermes_tool_mapping": tool_mapping,
        "gateway_platform_mapping": platform_mapping,
        "feature_mapping": feature_mapping,
    }
