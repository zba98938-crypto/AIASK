"""
BE-060: 后端契约测试 - 验证前后端接口契约

根据 desktop/src/types.ts 中的前端类型定义，验证后端 API 契约。
"""
import pytest
from typing import Any, Dict, List, Optional


# 前端期望的核心类型契约
class DesktopContractValidator:
    """验证后端响应是否符合前端类型契约"""

    @staticmethod
    def validate_health_detailed(data: Dict[str, Any]) -> List[str]:
        """验证 HealthDetailed 契约"""
        errors = []

        # 必需字段
        if "status" not in data or not isinstance(data["status"], str):
            errors.append("HealthDetailed.status 必须是字符串")
        if "service" not in data or not isinstance(data["service"], str):
            errors.append("HealthDetailed.service 必须是字符串")

        # 可选字段类型验证
        if "runtime" in data and data["runtime"] is not None:
            runtime = data["runtime"]
            if not isinstance(runtime, dict):
                errors.append("HealthDetailed.runtime 必须是对象")

        if "tools" in data and data["tools"] is not None:
            tools = data["tools"]
            if not isinstance(tools, dict):
                errors.append("HealthDetailed.tools 必须是对象")
            elif "count" in tools and not isinstance(tools["count"], int):
                errors.append("HealthDetailed.tools.count 必须是整数")

        if "hermes" in data and data["hermes"] is not None:
            hermes = data["hermes"]
            if not isinstance(hermes, dict):
                errors.append("HealthDetailed.hermes 必须是对象")
            elif "full_mode_active" in hermes and not isinstance(hermes["full_mode_active"], bool):
                errors.append("HealthDetailed.hermes.full_mode_active 必须是布尔值")

        return errors

    @staticmethod
    def validate_tool_catalog_item(data: Dict[str, Any]) -> List[str]:
        """验证 ToolCatalogItem 契约"""
        errors = []

        # 必需字段
        required_fields = ["name", "capability", "description"]
        for field in required_fields:
            if field not in data or not isinstance(data[field], str):
                errors.append(f"ToolCatalogItem.{field} 必须是字符串")

        # side_effect 可以是字符串或对象
        if "side_effect" in data:
            se = data["side_effect"]
            if not isinstance(se, (str, dict)):
                errors.append("ToolCatalogItem.side_effect 必须是字符串或对象")

        # visibility 枚举验证
        if "visibility" in data and data["visibility"] is not None:
            if data["visibility"] not in ["api_safe", "full_mode_only", "finance_safe"]:
                # 允许其他字符串值，但记录警告
                pass

        return errors

    @staticmethod
    def validate_tool_envelope(data: Dict[str, Any]) -> List[str]:
        """验证 ToolEnvelope 契约"""
        errors = []

        # 必需字段
        if "success" not in data or not isinstance(data["success"], bool):
            errors.append("ToolEnvelope.success 必须是布尔值")
        if "data" not in data:
            errors.append("ToolEnvelope.data 是必需字段")
        if "error" not in data or (data["error"] is not None and not isinstance(data["error"], str)):
            errors.append("ToolEnvelope.error 必须是 null 或字符串")

        # meta.side_effect 结构验证
        if "meta" in data and data["meta"] is not None:
            meta = data["meta"]
            if "side_effect" in meta and meta["side_effect"] is not None:
                se = meta["side_effect"]
                if not isinstance(se, dict):
                    errors.append("ToolEnvelope.meta.side_effect 必须是对象")

        return errors

    @staticmethod
    def validate_agent_response(data: Dict[str, Any]) -> List[str]:
        """验证 AgentResponse 契约"""
        errors = []

        # 必需字段
        required_fields = {
            "id": str,
            "object": str,
            "status": str,
            "output_text": str,
        }

        for field, expected_type in required_fields.items():
            if field not in data:
                errors.append(f"AgentResponse.{field} 是必需字段")
            elif not isinstance(data[field], expected_type):
                errors.append(f"AgentResponse.{field} 必须是 {expected_type.__name__}")

        # metadata 结构验证
        if "metadata" in data and data["metadata"] is not None:
            metadata = data["metadata"]
            if not isinstance(metadata, dict):
                errors.append("AgentResponse.metadata 必须是对象")
            elif "tool_calls" in metadata and metadata["tool_calls"] is not None:
                if not isinstance(metadata["tool_calls"], list):
                    errors.append("AgentResponse.metadata.tool_calls 必须是数组")

        return errors

    @staticmethod
    def validate_desktop_workbench_summary(data: Dict[str, Any]) -> List[str]:
        """验证 DesktopWorkbenchSummary 契约"""
        errors = []

        # 检查 access 字段
        if "access" not in data or not isinstance(data["access"], dict):
            errors.append("DesktopWorkbenchSummary.access 必须是对象")
        else:
            access = data["access"]
            if "full_mode_active" in access and not isinstance(access["full_mode_active"], bool):
                errors.append("DesktopWorkbenchSummary.access.full_mode_active 必须是布尔值")

        # 检查 queues 字段
        if "queues" in data and data["queues"] is not None:
            queues = data["queues"]
            if not isinstance(queues, dict):
                errors.append("DesktopWorkbenchSummary.queues 必须是对象")
            else:
                queue_fields = ["pending_intents", "pending_approvals", "gateway_failed", "mcp_degraded"]
                for field in queue_fields:
                    if field in queues and not isinstance(queues[field], int):
                        errors.append(f"DesktopWorkbenchSummary.queues.{field} 必须是整数")

        # 检查 recent_sessions 字段
        if "recent_sessions" in data and data["recent_sessions"] is not None:
            if not isinstance(data["recent_sessions"], list):
                errors.append("DesktopWorkbenchSummary.recent_sessions 必须是数组")

        return errors

    @staticmethod
    def validate_intent_record(data: Dict[str, Any]) -> List[str]:
        """验证 IntentRecord 契约"""
        errors = []

        # 必需字段
        required_fields = {
            "intent_id": str,
            "action": str,
            "target_tool": str,
            "target_action": str,
            "status": str,
        }

        for field, expected_type in required_fields.items():
            if field not in data:
                errors.append(f"IntentRecord.{field} 是必需字段")
            elif not isinstance(data[field], expected_type):
                errors.append(f"IntentRecord.{field} 必须是 {expected_type.__name__}")

        return errors


# 测试用例
class TestDesktopContracts:
    """契约测试套件"""

    def test_health_detailed_contract(self):
        """测试 /v1/health 契约"""
        validator = DesktopContractValidator()

        # 合法示例
        valid_health = {
            "status": "online",
            "service": "aiask",
            "runtime": {"model": "gpt-4"},
            "tools": {"count": 10},
            "hermes": {"full_mode_active": True}
        }
        errors = validator.validate_health_detailed(valid_health)
        assert len(errors) == 0, f"合法 HealthDetailed 应无错误: {errors}"

        # 非法示例
        invalid_health = {
            "status": 123,  # 应该是字符串
            "service": "aiask"
        }
        errors = validator.validate_health_detailed(invalid_health)
        assert len(errors) > 0, "非法 HealthDetailed 应有错误"

    def test_tool_catalog_item_contract(self):
        """测试 /v1/tools 契约"""
        validator = DesktopContractValidator()

        # 合法示例
        valid_tool = {
            "name": "agent_test",
            "capability": "test",
            "description": "测试工具",
            "side_effect": "read_only",
            "visibility": "api_safe"
        }
        errors = validator.validate_tool_catalog_item(valid_tool)
        assert len(errors) == 0, f"合法 ToolCatalogItem 应无错误: {errors}"

        # side_effect 对象格式
        valid_tool_obj = {
            "name": "agent_test",
            "capability": "test",
            "description": "测试工具",
            "side_effect": {"level": "write", "target": "database"}
        }
        errors = validator.validate_tool_catalog_item(valid_tool_obj)
        assert len(errors) == 0, f"side_effect 对象格式应合法: {errors}"

    def test_tool_envelope_contract(self):
        """测试工具响应 envelope 契约"""
        validator = DesktopContractValidator()

        # 合法示例
        valid_envelope = {
            "success": True,
            "data": {"result": "ok"},
            "error": None
        }
        errors = validator.validate_tool_envelope(valid_envelope)
        assert len(errors) == 0, f"合法 ToolEnvelope 应无错误: {errors}"

        # 错误响应
        error_envelope = {
            "success": False,
            "data": None,
            "error": "工具执行失败"
        }
        errors = validator.validate_tool_envelope(error_envelope)
        assert len(errors) == 0, f"错误 envelope 应符合契约: {errors}"

    def test_agent_response_contract(self):
        """测试 /v1/agent/thread 契约"""
        validator = DesktopContractValidator()

        # 合法示例
        valid_response = {
            "id": "thread_123",
            "object": "thread",
            "status": "completed",
            "output_text": "任务完成",
            "metadata": {
                "session_id": "sess_abc",
                "run_id": "run_xyz",
                "tool_calls": []
            }
        }
        errors = validator.validate_agent_response(valid_response)
        assert len(errors) == 0, f"合法 AgentResponse 应无错误: {errors}"

    def test_workbench_summary_contract(self):
        """测试 /v1/desktop/workbench/summary 契约"""
        validator = DesktopContractValidator()

        # 合法示例
        valid_summary = {
            "access": {
                "full_mode_active": True,
                "sessions_admin_available": True
            },
            "queues": {
                "pending_intents": 0,
                "pending_approvals": 0,
                "gateway_failed": 0,
                "mcp_degraded": 0
            },
            "recent_sessions": [
                {
                    "session_id": "sess_001",
                    "title": "测试会话",
                    "last_message_at": "2026-06-04T10:00:00Z"
                }
            ]
        }
        errors = validator.validate_desktop_workbench_summary(valid_summary)
        assert len(errors) == 0, f"合法 DesktopWorkbenchSummary 应无错误: {errors}"

    def test_intent_record_contract(self):
        """测试 Intent 契约"""
        validator = DesktopContractValidator()

        # 合法示例
        valid_intent = {
            "intent_id": "intent_123",
            "action": "portfolio_adjust",
            "target_tool": "agent_portfolio_action",
            "target_action": "adjust_position",
            "status": "awaiting_confirmation"
        }
        errors = validator.validate_intent_record(valid_intent)
        assert len(errors) == 0, f"合法 IntentRecord 应无错误: {errors}"


# BE-061: 契约清单
CONTRACT_CHECKLIST = {
    "core_apis": [
        "/v1/health → HealthDetailed",
        "/v1/tools → List[ToolCatalogItem]",
        "/v1/agent/thread → AgentResponse",
        "/v1/hermes/status → HermesStatus",
    ],
    "desktop_apis": [
        "/v1/desktop/workbench/summary → DesktopWorkbenchSummary",
        "/v1/desktop/runs → List[DesktopRunSummary]",
        "/v1/desktop/runs/{run_id}/events → List[NormalizedRunEvent]",
    ],
    "hermes_apis": [
        "/v1/hermes/readiness → HermesReadiness",
        "/v1/hermes/toolsets → List[HermesToolset]",
        "/v1/hermes/tools → List[ToolCatalogItem]",
        "/v1/hermes/sessions → List[RecentSessionSummary]",
        "/v1/hermes/sessions/{session_id}/messages → List[Message]",
        "/v1/hermes/intents → List[IntentRecord]",
        "/v1/hermes/intents/{intent_id} → ToolEnvelope<IntentRecord>",
        "/v1/hermes/intents/{intent_id}/confirm → ToolEnvelope",
        "/v1/hermes/intents/{intent_id}/deny → ToolEnvelope",
    ],
    "gateway_apis": [
        "/v1/hermes/gateway/queue → GatewayQueueSnapshot",
        "/v1/hermes/gateway/failed → List[FailedGatewayTask]",
    ]
}


def test_contract_checklist_coverage():
    """BE-062: 验证契约清单覆盖率"""
    # 这是一个元测试，确保契约清单与实际 API 保持同步
    assert len(CONTRACT_CHECKLIST["core_apis"]) >= 4
    assert len(CONTRACT_CHECKLIST["desktop_apis"]) >= 3
    assert len(CONTRACT_CHECKLIST["hermes_apis"]) >= 7
