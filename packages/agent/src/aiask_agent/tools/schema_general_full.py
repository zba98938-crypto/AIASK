from __future__ import annotations

from typing import Any

from .schema_helpers import schema


GENERAL_FULL_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "agent_file_read": schema(
        {
            "path": {"type": "string"},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576},
        },
        required=["path"],
    ),
    "agent_file_write": schema(
        {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "create_parent_dirs": {"type": "boolean", "default": False},
            "checkpoint": {"type": "boolean", "default": True},
            "checkpoint_reason": {"type": "string"},
        },
        required=["path", "content"],
    ),
    "agent_file_list": schema(
        {
            "path": {"type": "string"},
            "recursive": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
        required=["path"],
    ),
    "agent_file_search": schema(
        {
            "path": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        required=["path", "query"],
    ),
    "agent_file_patch": schema(
        {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
            "count": {"type": "integer", "minimum": 1},
            "checkpoint": {"type": "boolean", "default": True},
            "checkpoint_reason": {"type": "string"},
        },
        required=["path", "old", "new"],
    ),
    "agent_file_mutation_verify": schema(
        {
            "path": {"type": "string"},
            "operation": {"type": "string", "enum": ["verify", "write", "patch"], "default": "verify"},
            "before_sha256": {"type": "string"},
            "include_diagnostics": {"type": "boolean", "default": True},
        },
        required=["path"],
    ),
    "agent_file_checkpoint": schema(
        {
            "path": {"type": "string"},
            "reason": {"type": "string"},
        },
        required=["path"],
    ),
    "agent_file_rollback": schema(
        {
            "checkpoint_id": {"type": "string"},
            "path": {"type": "string"},
            "reason": {"type": "string"},
        },
    ),
    "agent_code_graph_query": schema(
        {
            "action": {
                "type": "string",
                "enum": ["summary", "search", "endpoint", "explain", "affected"],
                "default": "summary",
            },
            "query": {"type": "string"},
            "node": {"type": "string"},
            "endpoint": {"type": "string"},
            "relation": {"type": "string", "default": "calls"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 5},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "graph_dir": {
                "type": "string",
                "description": "Optional curated graph directory under an allowed workspace root.",
            },
        }
    ),
    "agent_terminal": schema(
        {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "backend": {"type": "string", "enum": ["local", "docker", "ssh", "singularity", "modal", "daytona"], "default": "local"},
            "session_id": {"type": "string"},
            "pty": {"type": "boolean", "default": False},
            "stdin": {"type": "string"},
            "background": {"type": "boolean", "default": False},
            "notify_on_complete": {"type": "boolean", "default": False},
            "image": {"type": "string"},
            "resource_limits": {"type": "object"},
            "env_allowlist": {"type": "array", "items": {"type": "string"}},
            "approval_id": {"type": "string"},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 300},
            "max_output_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576},
        },
        required=["command"],
    ),
    "agent_process": schema(
        {
            "action": {"type": "string", "enum": ["list", "read", "kill", "wait", "watch"], "default": "list"},
            "process_id": {"type": "string"},
            "session_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "max_output_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576},
            "tail": {"type": "boolean", "default": True},
            "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 3600},
        }
    ),
    "agent_terminal_backends": schema(
        {
            "action": {"type": "string", "enum": ["list", "sessions"], "default": "list"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
    ),
    "agent_tui_status": schema({}),
    "agent_execute_python": schema(
        {
            "code": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 300},
            "max_output_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576},
            "max_tool_calls": {"type": "integer", "minimum": 0, "maximum": 50},
        },
        required=["code"],
    ),
    "agent_computer_use": schema(
        {
            "action": {
                "type": "string",
                "enum": ["status", "screenshot", "browser_click", "browser_type", "browser_key"],
                "default": "status",
            },
            "selector": {"type": "string"},
            "text": {"type": "string"},
            "key": {"type": "string"},
            "full_page": {"type": "boolean", "default": False},
            "include_base64": {"type": "boolean", "default": False},
        }
    ),
    "agent_browser_navigate": schema({"url": {"type": "string"}}, required=["url"]),
    "agent_browser_snapshot": schema({}),
    "agent_browser_click": schema({"selector": {"type": "string"}}, required=["selector"]),
    "agent_browser_type": schema(
        {"selector": {"type": "string"}, "text": {"type": "string"}},
        required=["selector", "text"],
    ),
    "agent_browser_extract": schema({"selector": {"type": "string"}}),
    "agent_browser_scroll": schema(
        {
            "selector": {"type": "string"},
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "default": "down"},
            "amount": {"type": "integer", "minimum": 1, "maximum": 5000},
        }
    ),
    "agent_browser_back": schema({}),
    "agent_browser_press": schema({"key": {"type": "string"}}, required=["key"]),
    "agent_browser_get_images": schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    "agent_browser_vision": schema({"prompt": {"type": "string"}}),
    "agent_browser_console": schema({"limit": {"type": "integer", "minimum": 1, "maximum": 500}}),
    "agent_browser_cdp": schema(
        {
            "method": {"type": "string"},
            "params": {"type": "object"},
        },
        required=["method"],
    ),
    "agent_web_search": schema(
        {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "provider": {"type": "string"},
            "source_id": {"type": "string"},
            "search_depth": {"type": "string", "enum": ["basic", "advanced"]},
            "include_answer": {"type": "boolean"},
            "search_type": {"type": "string"},
        },
        required=["query"],
    ),
    "agent_web_extract": schema(
        {
            "url": {"type": "string"},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
        },
        required=["url"],
    ),
    "agent_x_search": schema(
        {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "since_id": {"type": "string"},
            "next_token": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 10, "maximum": 100},
        },
        required=["query"],
    ),
    "agent_vision_analyze": schema(
        {
            "image_path": {"type": "string"},
            "image_url": {"type": "string"},
            "prompt": {"type": "string"},
            "provider": {"type": "string"},
            "model": {"type": "string"},
        }
    ),
    "agent_media_provider_catalog": schema({}),
    "agent_image_generate": schema(
        {
            "prompt": {"type": "string"},
            "model": {"type": "string"},
            "size": {"type": "string", "enum": ["1024x1024", "1024x1536", "1536x1024"]},
        },
        required=["prompt"],
    ),
    "agent_video_generate": schema(
        {
            "action": {"type": "string", "enum": ["status", "create", "status_check"], "default": "status"},
            "prompt": {"type": "string"},
            "provider": {"type": "string"},
            "model": {"type": "string"},
            "size": {"type": "string"},
            "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
            "job_id": {"type": "string"},
            "metadata": {"type": "object"},
        }
    ),
    "agent_text_to_speech": schema(
        {
            "text": {"type": "string"},
            "provider": {"type": "string"},
            "model": {"type": "string"},
            "voice": {"type": "string"},
            "format": {"type": "string", "enum": ["mp3", "opus", "aac", "flac", "wav", "pcm"]},
            "speed": {"type": "number", "minimum": 0.25, "maximum": 4.0},
        },
        required=["text"],
    ),
    "agent_transcribe_audio": schema(
        {
            "audio_path": {"type": "string"},
            "audio_url": {"type": "string"},
            "provider": {"type": "string"},
            "model": {"type": "string"},
            "language": {"type": "string"},
            "prompt": {"type": "string"},
            "response_format": {"type": "string"},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 104857600},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 300},
        }
    ),
    "agent_clarify": schema(
        {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        },
        required=["question"],
    ),
    "agent_todo_set": schema(
        {
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "merge": {"type": "boolean", "default": False},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    },
                },
            },
        },
        required=["items"],
    ),
    "agent_todo_list": schema({"session_id": {"type": "string"}}),
    "agent_todo": schema(
        {
            "action": {"type": "string", "enum": ["add", "update", "list", "clear", "status"], "default": "list"},
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "item_id": {"type": "string"},
            "content": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
            "items": {"type": "array", "items": {"type": "object"}},
        }
    ),
    "agent_subgoal": schema(
        {
            "action": {"type": "string", "enum": ["add", "update", "list", "clear", "status"], "default": "list"},
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "subgoal_id": {"type": "string"},
            "title": {"type": "string"},
            "criteria": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
        }
    ),
    "agent_skill_list": schema({}),
    "agent_skill_view": schema(
        {
            "name": {"type": "string"},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
        },
        required=["name"],
    ),
    "agent_skill_save": schema(
        {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "content": {"type": "string"},
        },
        required=["name", "content"],
    ),
    "agent_skill_manage": schema(
        {
            "action": {
                "type": "string",
                "enum": [
                    "search",
                    "install",
                    "update",
                    "uninstall",
                    "audit",
                    "snapshot",
                    "pin",
                    "unpin",
                    "set_enabled",
                    "archive",
                    "restore",
                    "rollback",
                    "install_finance_templates",
                ],
                "default": "search",
            },
            "name": {"type": "string"},
            "query": {"type": "string"},
            "content": {"type": "string"},
            "description": {"type": "string"},
            "enabled": {"type": "boolean"},
            "dry_run": {"type": "boolean", "default": True},
            "create_backup": {"type": "boolean", "default": False},
            "backup_id": {"type": "string"},
            "reason": {"type": "string"},
            "overwrite": {"type": "boolean", "default": False},
        }
    ),
    "agent_skill_pack_manage": schema(
        {
            "action": {"type": "string", "enum": ["list", "status", "install", "audit"], "default": "list"},
            "pack": {"type": "string"},
            "name": {"type": "string"},
            "overwrite": {"type": "boolean", "default": False},
        }
    ),
    "agent_plugin_list": schema({}),
    "agent_plugin_set_enabled": schema(
        {
            "name": {"type": "string"},
            "enabled": {"type": "boolean"},
            "description": {"type": "string"},
        },
        required=["name", "enabled"],
    ),
    "agent_plugin_manage": schema(
        {
            "action": {"type": "string", "enum": ["list", "enable", "disable", "upsert", "inspect"], "default": "list"},
            "name": {"type": "string"},
            "manifest": {"type": "object"},
            "description": {"type": "string"},
        }
    ),
    "agent_mcp_manage": schema(
        {
            "action": {
                "type": "string",
                "enum": [
                    "servers",
                    "tools",
                    "resources",
                    "prompts",
                    "test",
                    "discover",
                    "resource_read",
                    "prompt_get",
                    "oauth_start",
                    "oauth_callback",
                    "oauth_status",
                ],
                "default": "servers",
            },
            "server": {"type": "string"},
            "uri": {"type": "string"},
            "prompt": {"type": "string"},
            "name": {"type": "string"},
            "arguments": {"type": "object"},
            "redirect_uri": {"type": "string"},
            "scope": {"type": "string"},
            "code": {"type": "string"},
            "access_token": {"type": "string"},
            "refresh_token": {"type": "string"},
            "expires_in": {"type": "integer"},
            "token_type": {"type": "string"},
            "token": {"type": "object"},
        }
    ),
    "agent_model_manage": schema(
        {
            "action": {"type": "string", "enum": ["status", "providers", "credential_pool", "select", "record_attempt", "classify_error", "prompt_cache"], "default": "status"},
            "provider": {"type": "string"},
            "credential_id": {"type": "string"},
            "success": {"type": "boolean"},
            "error": {"type": "string"},
        }
    ),
    "agent_memory_manage": schema(
        {
            "action": {"type": "string", "enum": ["status", "catalog", "save", "search", "audit"], "default": "status"},
            "content": {"type": "string"},
            "query": {"type": "string"},
            "user_id": {"type": "string"},
            "symbol": {"type": "string"},
            "strategy_id": {"type": "string"},
            "research_topic": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_acp_manage": schema(
        {
            "action": {"type": "string", "enum": ["status", "register_mcp_server", "remove_mcp_server", "readiness"], "default": "status"},
            "name": {"type": "string"},
            "transport": {"type": "string", "enum": ["stdio", "sse", "streamable_http", "http"]},
            "url": {"type": "string"},
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "domain": {"type": "string"},
            "tools": {"type": "array", "items": {"type": "object"}},
            "resources": {"type": "array", "items": {"type": "object"}},
            "prompts": {"type": "array", "items": {"type": "object"}},
            "oauth": {"type": "object"},
            "headers_from_env": {"type": "object"},
            "enabled": {"type": "boolean"},
            "description": {"type": "string"},
        }
    ),
    "agent_security_scan": schema(
        {
            "text": {"type": "string"},
            "path": {"type": "string"},
            "url": {"type": "string"},
            "include_env": {"type": "boolean", "default": False},
        }
    ),
    "agent_message_send": schema(
        {
            "action": {"type": "string", "enum": ["send", "list"], "default": "send"},
            "platform": {"type": "string"},
            "target": {
                "type": "string",
                "description": "Either a plain target with platform set, or Hermes-style platform:target[:thread_id].",
            },
            "message": {"type": "string"},
            "thread_id": {"type": "string"},
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "media_paths": {"type": "array", "items": {"type": "string"}},
        },
        required=[],
    ),
    "agent_gateway_status": schema({}),
    "agent_gateway_platforms": schema({"platform": {"type": "string"}}),
    "agent_gateway_send_message": schema(
        {
            "action": {"type": "string", "enum": ["send", "list"], "default": "send"},
            "platform": {"type": "string"},
            "target": {
                "type": "string",
                "description": "Either a plain target with platform set, or Hermes-style platform:target[:thread_id].",
            },
            "message": {"type": "string"},
            "thread_id": {"type": "string"},
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "media_paths": {"type": "array", "items": {"type": "string"}},
        },
        required=[],
    ),
    "agent_gateway_history": schema(
        {
            "platform": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
    ),
    "agent_gateway_pairing": schema(
        {
            "action": {"type": "string", "enum": ["status", "create"], "default": "status"},
            "platform": {"type": "string"},
            "user_id": {"type": "string"},
            "session_id": {"type": "string"},
        }
    ),
    "agent_gateway_directory": schema(
        {
            "action": {"type": "string", "enum": ["list", "resolve", "refresh", "upsert"], "default": "list"},
            "platform": {"type": "string"},
            "kind": {"type": "string"},
            "name": {"type": "string"},
            "target": {"type": "string"},
            "thread_id": {"type": "string"},
            "metadata": {"type": "object"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
    ),
    "agent_gateway_direct_deliver": schema(
        {
            "platform": {"type": "string"},
            "target": {
                "type": "string",
                "description": "Either a plain target with platform set, or Hermes-style platform:target[:thread_id].",
            },
            "message": {"type": "string"},
            "thread_id": {"type": "string"},
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "media_paths": {"type": "array", "items": {"type": "string"}},
        },
        required=["message"],
    ),
    "agent_learning_status": schema({}),
    "agent_learning_review": schema(
        {
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
    ),
    "agent_learning_apply": schema({"proposal_id": {"type": "string"}}, required=["proposal_id"]),
    "agent_skill_reflect": schema(
        {
            "name": {"type": "string"},
            "observation": {"type": "string"},
        },
        required=["name", "observation"],
    ),
    "agent_ha_list_entities": schema(
        {
            "domain": {"type": "string"},
            "area": {"type": "string"},
        }
    ),
    "agent_ha_get_state": schema({"entity_id": {"type": "string"}}, required=["entity_id"]),
    "agent_ha_list_services": schema({}),
    "agent_ha_list_events": schema({}),
    "agent_ha_list_registry": schema({"kind": {"type": "string", "enum": ["area", "device", "entity"]}}, required=["kind"]),
    "agent_ha_call_service": schema(
        {
            "domain": {"type": "string"},
            "service": {"type": "string"},
            "entity_id": {"type": "string"},
            "data": {"type": "object"},
            "rationale": {"type": "string"},
            "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 604800},
        },
        required=["domain", "service"],
    ),
    "agent_moa": schema(
        {
            "user_prompt": {"type": "string"},
            "max_reference_tokens": {"type": "integer", "minimum": 1},
        },
        required=["user_prompt"],
    ),
    "agent_feishu_doc_read": schema({"document_id": {"type": "string"}, "url": {"type": "string"}, "domain": {"type": "string", "enum": ["feishu", "lark"]}}),
    "agent_feishu_drive_list_comments": schema(
        {
            "file_token": {"type": "string"},
            "file_type": {"type": "string", "default": "docx"},
            "page_token": {"type": "string"},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
            "domain": {"type": "string", "enum": ["feishu", "lark"]},
        },
        required=["file_token"],
    ),
    "agent_feishu_drive_list_comment_replies": schema(
        {
            "file_token": {"type": "string"},
            "comment_id": {"type": "string"},
            "file_type": {"type": "string", "default": "docx"},
            "page_token": {"type": "string"},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
            "domain": {"type": "string", "enum": ["feishu", "lark"]},
        },
        required=["file_token", "comment_id"],
    ),
    "agent_feishu_drive_reply_comment": schema(
        {
            "file_token": {"type": "string"},
            "comment_id": {"type": "string"},
            "message": {"type": "string"},
            "domain": {"type": "string", "enum": ["feishu", "lark"]},
        },
        required=["file_token", "comment_id", "message"],
    ),
    "agent_feishu_drive_add_comment": schema(
        {
            "file_token": {"type": "string"},
            "message": {"type": "string"},
            "domain": {"type": "string", "enum": ["feishu", "lark"]},
        },
        required=["file_token", "message"],
    ),
    "agent_discord_channel_send": schema(
        {
            "channel_id": {"type": "string"},
            "message": {"type": "string"},
            "thread_id": {"type": "string"},
        },
        required=["channel_id", "message"],
    ),
    "agent_discord_server": schema(
        {
            "action": {
                "type": "string",
                "enum": [
                    "list_guilds",
                    "server_info",
                    "list_channels",
                    "channel_info",
                    "list_roles",
                    "member_info",
                    "search_members",
                    "fetch_messages",
                    "list_pins",
                    "pin_message",
                    "unpin_message",
                    "create_thread",
                    "add_role",
                    "remove_role",
                ],
            },
            "guild_id": {"type": "string"},
            "channel_id": {"type": "string"},
            "user_id": {"type": "string"},
            "role_id": {"type": "string"},
            "message_id": {"type": "string"},
            "query": {"type": "string"},
            "name": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "before": {"type": "string"},
            "after": {"type": "string"},
            "auto_archive_duration": {"type": "integer", "enum": [60, 1440, 4320, 10080]},
            "rationale": {"type": "string"},
            "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 604800},
        },
        required=["action"],
    ),
    "agent_rl_list_environments": schema({}),
    "agent_rl_select_environment": schema({"environment": {"type": "string"}}, required=["environment"]),
    "agent_rl_get_config": schema({}),
    "agent_rl_edit_config": schema({"config": {"type": "object"}, "patch": {"type": "object"}}),
    "agent_rl_start_training": schema(
        {
            "environment": {"type": "string"},
            "config": {"type": "object"},
        }
    ),
    "agent_rl_check_status": schema({"run_id": {"type": "string"}}, required=["run_id"]),
    "agent_rl_stop_training": schema({"run_id": {"type": "string"}}, required=["run_id"]),
    "agent_rl_get_results": schema({"run_id": {"type": "string"}}, required=["run_id"]),
    "agent_rl_list_runs": schema({"limit": {"type": "integer", "minimum": 1, "maximum": 1000}}),
    "agent_rl_test_inference": schema({"prompt": {"type": "string"}}, required=["prompt"]),
    "agent_webhook": schema(
        {
            "action": {"type": "string", "enum": ["subscribe", "list", "remove", "trigger"], "default": "list"},
            "webhook_id": {"type": "string"},
            "name": {"type": "string"},
            "events": {"type": "array", "items": {"type": "string"}},
            "prompt": {"type": "string"},
            "deliver": {"type": "string"},
            "deliver_mode": {"type": "string", "enum": ["agent", "direct_platform"]},
            "platform": {"type": "string"},
            "target": {"type": "string"},
            "thread_id": {"type": "string"},
            "secret": {"type": "string"},
            "event": {"type": "string"},
            "payload": {"type": "object"},
            "signature": {"type": "string"},
        }
    ),
    "agent_delegate_task": schema(
        {
            "task": {"type": "string"},
            "toolset": {"type": "string", "enum": ["finance_safe", "general_full"]},
            "max_iterations": {"type": "integer", "minimum": 1, "maximum": 12},
            "user_id": {"type": "string"},
            "role": {"type": "string", "description": "The specific financial role for the sub-agent (e.g. 'Macro Analyst')."},
            "system_prompt": {"type": "string", "description": "Optional custom prompt instructing the sub-agent."},
        },
        required=["task"],
    ),
    "agent_job_create": schema(
        {
            "name": {"type": "string"},
            "prompt": {"type": "string"},
            "schedule": {"type": "string"},
            "interval_seconds": {"type": "integer", "minimum": 1},
            "toolset": {"type": "string", "enum": ["finance_safe", "general_full"]},
            "enabled": {"type": "boolean", "default": True},
        },
        required=["name", "prompt"],
    ),
    "agent_job_list": schema({}),
    "agent_job_run": schema({"job_id": {"type": "string"}}, required=["job_id"]),
    "agent_cronjob": schema(
        {
            "action": {"type": "string", "enum": ["create", "list", "update", "pause", "resume", "remove", "trigger"], "default": "list"},
            "job_id": {"type": "string"},
            "name": {"type": "string"},
            "prompt": {"type": "string"},
            "schedule": {"type": "string"},
            "interval_seconds": {"type": "integer", "minimum": 1},
            "toolset": {"type": "string", "enum": ["finance_safe", "general_full"]},
            "enabled": {"type": "boolean"},
            "script": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}},
            "silent_pattern": {"type": "string"},
        }
    ),
    "agent_memory_save": schema(
        {
            "content": {"type": "string", "description": "The financial facts, strategy configuration, or market insights to remember."},
            "user_id": {"type": "string"},
            "symbol": {"type": "string"},
            "strategy_id": {"type": "string"},
            "research_topic": {"type": "string"},
        },
        required=["content"],
    ),
    "agent_memory_search": schema(
        {
            "query": {"type": "string"},
            "user_id": {"type": "string"},
            "symbol": {"type": "string"},
            "strategy_id": {"type": "string"},
            "research_topic": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_memory": schema(
        {
            "action": {"type": "string", "enum": ["save", "search", "status"], "default": "search"},
            "content": {"type": "string"},
            "query": {"type": "string"},
            "user_id": {"type": "string"},
            "symbol": {"type": "string"},
            "strategy_id": {"type": "string"},
            "research_topic": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_session_search": schema(
        {
            "query": {"type": "string"},
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
    "agent_session_handoff": schema(
        {
            "action": {"type": "string", "enum": ["request", "status", "list", "complete", "fail"], "default": "status"},
            "handoff_id": {"type": "string"},
            "session_id": {"type": "string"},
            "user_id": {"type": "string"},
            "target": {"type": "string"},
            "reason": {"type": "string"},
            "summary": {"type": "string"},
            "metadata": {"type": "object"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    ),
}
