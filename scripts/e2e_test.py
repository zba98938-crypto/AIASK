#!/usr/bin/env python3
"""AIASK Desktop P0 端到端测试"""
import json, sys, urllib.request, urllib.error
from datetime import datetime

DESKTOP_API = "http://127.0.0.1:8001"
results, test_data = [], {}

def log(level, msg):
    colors = {"INFO": "\033[36m", "PASS": "\033[32m", "FAIL": "\033[31m"}
    print(f"{colors.get(level, '')}{level:5} {msg}\033[0m")

def call_api(method, url, data=None):
    try:
        headers = {"Content-Type": "application/json"}
        request_data = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=request_data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as response:
            return True, json.loads(response.read().decode())
    except Exception as e:
        return False, str(e)

def test(name, method, path, data=None, key=None):
    url = f"{DESKTOP_API}{path}"
    log("INFO", f"{name}: {method} {path}")
    ok, result = call_api(method, url, data)
    if ok:
        log("PASS", f"  ✓ 成功")
        results.append(("PASS", name))
        if key and key in result:
            test_data[name] = result[key]
        return result
    else:
        log("FAIL", f"  ✗ {result}")
        results.append(("FAIL", name))
        return None

print("=" * 60)
print("  AIASK Desktop P0 端到端测试")
print("=" * 60)

# 测试流程
test("健康检查", "GET", "/health")
test("创建线程", "POST", "/v1/threads", {"title": "E2E测试"}, "id")
test("列出线程", "GET", "/v1/threads")
test("添加MCP", "POST", "/v1/mcp/servers", {"name": "test", "command": "python"}, "id")
test("列出MCP", "GET", "/v1/mcp/servers")
test("添加技能", "POST", "/v1/skills", {"name": "test", "type": "custom", "path": "/test"}, "id")
test("列出技能", "GET", "/v1/skills")
test("创建策略", "POST", "/v1/users/default/strategies", {"name": "测试", "type": "momentum"}, "id")
test("创建股票池", "POST", "/v1/users/default/stock-pools", {"name": "测试池"}, "id")

# 清理
for key, path in [
    ("创建线程", "/v1/threads/"),
    ("添加MCP", "/v1/mcp/servers/"),
    ("添加技能", "/v1/skills/"),
    ("创建策略", "/v1/users/default/strategies/"),
    ("创建股票池", "/v1/users/default/stock-pools/")
]:
    if key in test_data:
        test(f"删除{key}", "DELETE", f"{path}{test_data[key]}")

# 统计
passed = sum(1 for s, _ in results if s == "PASS")
total = len(results)
print("\n" + "=" * 60)
print(f"总计: {total}, 通过: {passed}, 失败: {total - passed}")
print(f"通过率: {passed/total*100:.1f}%")
print("=" * 60)
sys.exit(0 if passed == total else 1)
