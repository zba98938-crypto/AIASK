"""Gateway Security — 入站消息安全控制。

提供：
  - 发送者白名单机制
  - 速率限制（防滥用）
  - 消息内容安全过滤
  - 平台级信息暴露控制
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# 白名单格式：platform:sender_id,platform:sender_id,...
# 空白名单 = 允许所有用户（开发模式）
ALLOWED_SENDERS_RAW = os.getenv("AIASK_GATEWAY_ALLOWED_SENDERS", "").strip()

# 速率限制：每用户每分钟最大消息数
RATE_LIMIT_PER_MINUTE = int(os.getenv("AIASK_GATEWAY_RATE_LIMIT", "20"))

# 平台安全级别
PLATFORM_SECURITY_LEVELS: dict[str, str] = {
    "wecom": "full",        # 企业微信：可展示完整信息
    "feishu": "full",       # 飞书：可展示完整信息
    "email": "full",        # 邮件：可展示完整信息
    "weixin": "masked",     # 个人微信：脱敏展示
    "telegram": "masked",   # Telegram：脱敏展示
    "discord": "masked",    # Discord：脱敏展示
    "qqbot": "minimal",     # QQ：最小信息展示
    "slack": "full",        # Slack：可展示完整信息
}


# ---------------------------------------------------------------------------
# Sender Whitelist
# ---------------------------------------------------------------------------

class SenderWhitelist:
    """发送者白名单管理。"""

    def __init__(self, raw_config: str = ALLOWED_SENDERS_RAW):
        self._entries: set[str] = set()
        self._enabled = False
        self._parse(raw_config)

    def _parse(self, raw: str):
        """Parse whitelist from comma-separated platform:sender_id format."""
        if not raw:
            self._enabled = False
            return
        self._enabled = True
        for entry in raw.split(","):
            entry = entry.strip()
            if entry:
                self._entries.add(entry.lower())

    @property
    def enabled(self) -> bool:
        return self._enabled

    def is_allowed(self, platform: str, sender_id: str) -> bool:
        """Check if a sender is allowed to interact with the agent."""
        if not self._enabled:
            return True  # No whitelist = allow all (dev mode)
        key = f"{platform.lower()}:{sender_id.lower()}"
        # Also check wildcard patterns
        platform_wildcard = f"{platform.lower()}:*"
        return key in self._entries or platform_wildcard in self._entries

    def add(self, platform: str, sender_id: str):
        """Dynamically add a sender to the whitelist."""
        self._entries.add(f"{platform.lower()}:{sender_id.lower()}")
        self._enabled = True

    def remove(self, platform: str, sender_id: str):
        """Remove a sender from the whitelist."""
        self._entries.discard(f"{platform.lower()}:{sender_id.lower()}")

    def list_entries(self) -> list[str]:
        """List all whitelist entries."""
        return sorted(self._entries)


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------

class MessageRateLimiter:
    """Per-user message rate limiting."""

    def __init__(self, max_per_minute: int = RATE_LIMIT_PER_MINUTE):
        self._max = max_per_minute
        self._windows: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, platform: str, sender_id: str) -> bool:
        """Check if the sender is within rate limits."""
        key = f"{platform}:{sender_id}"
        now = time.time()
        window_start = now - 60.0

        # Clean old entries
        self._windows[key] = [t for t in self._windows[key] if t > window_start]

        if len(self._windows[key]) >= self._max:
            return False

        self._windows[key].append(now)
        return True

    def get_remaining(self, platform: str, sender_id: str) -> int:
        """Get remaining allowed messages in current window."""
        key = f"{platform}:{sender_id}"
        now = time.time()
        window_start = now - 60.0
        self._windows[key] = [t for t in self._windows[key] if t > window_start]
        return max(0, self._max - len(self._windows[key]))


# ---------------------------------------------------------------------------
# Content Security Filter
# ---------------------------------------------------------------------------

class ContentSecurityFilter:
    """入站消息内容安全过滤。"""

    # Patterns that might indicate prompt injection attempts
    SUSPICIOUS_PATTERNS = [
        "ignore previous instructions",
        "ignore all previous",
        "you are now",
        "new instructions:",
        "system prompt:",
        "override:",
        "jailbreak",
        "DAN mode",
    ]

    def check_content(self, content: str) -> dict[str, Any]:
        """Check message content for security issues."""
        result = {
            "safe": True,
            "warnings": [],
            "blocked": False,
            "sanitized_content": content,
        }

        content_lower = content.lower()

        # Check for suspicious patterns
        for pattern in self.SUSPICIOUS_PATTERNS:
            if pattern.lower() in content_lower:
                result["warnings"].append(f"Suspicious pattern detected: '{pattern}'")
                result["safe"] = False

        # Check for extremely long messages (potential DoS)
        if len(content) > 10000:
            result["warnings"].append("Message exceeds 10000 characters")
            result["sanitized_content"] = content[:10000] + "\n[消息已截断]"

        return result


# ---------------------------------------------------------------------------
# Platform Security Level
# ---------------------------------------------------------------------------

class PlatformSecurityManager:
    """管理不同平台的信息暴露级别。"""

    def __init__(self, levels: dict[str, str] | None = None):
        self._levels = levels or PLATFORM_SECURITY_LEVELS

    def get_level(self, platform: str) -> str:
        """Get security level for a platform."""
        return self._levels.get(platform, "masked")

    def should_mask(self, platform: str) -> bool:
        """Check if content should be masked for this platform."""
        return self.get_level(platform) in ("masked", "minimal")

    def mask_sensitive(self, content: str, platform: str) -> str:
        """Mask sensitive information based on platform security level."""
        level = self.get_level(platform)

        if level == "full":
            return content

        if level == "minimal":
            # Hide all numbers that look like amounts or account numbers
            import re
            content = re.sub(r'\d{6,}', '******', content)
            content = re.sub(r'¥[\d,.]+', '¥***', content)
            content = re.sub(r'\$[\d,.]+', '$***', content)
            return content

        if level == "masked":
            # Partially mask account numbers and large amounts
            import re
            content = re.sub(r'(\d{4})\d{4,}(\d{4})', r'\1****\2', content)
            return content

        return content


# ---------------------------------------------------------------------------
# Unified Gateway Security
# ---------------------------------------------------------------------------

class GatewaySecurity:
    """统一网关安全管理器。"""

    def __init__(self):
        self.whitelist = SenderWhitelist()
        self.rate_limiter = MessageRateLimiter()
        self.content_filter = ContentSecurityFilter()
        self.platform_security = PlatformSecurityManager()

    def check_inbound(self, platform: str, sender_id: str, content: str) -> dict[str, Any]:
        """Perform all security checks on an inbound message.

        Returns:
            dict with keys: allowed, reason, warnings
        """
        # 1. Whitelist check
        if not self.whitelist.is_allowed(platform, sender_id):
            return {
                "allowed": False,
                "reason": "sender_not_whitelisted",
                "warnings": [],
            }

        # 2. Rate limit check
        if not self.rate_limiter.is_allowed(platform, sender_id):
            return {
                "allowed": False,
                "reason": "rate_limit_exceeded",
                "warnings": [],
            }

        # 3. Content security check
        content_check = self.content_filter.check_content(content)

        return {
            "allowed": True,
            "reason": None,
            "warnings": content_check.get("warnings", []),
            "content_safe": content_check["safe"],
            "sanitized_content": content_check.get("sanitized_content", content),
        }

    def mask_outbound(self, content: str, platform: str) -> str:
        """Mask sensitive information in outbound messages."""
        return self.platform_security.mask_sensitive(content, platform)

    def status(self) -> dict[str, Any]:
        """Get security status summary."""
        return {
            "whitelist_enabled": self.whitelist.enabled,
            "whitelist_entries": len(self.whitelist.list_entries()),
            "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
            "platform_levels": dict(PLATFORM_SECURITY_LEVELS),
        }
