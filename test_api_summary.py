"""
AWBotNest API 可用性测试报告
"""
import requests
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API_BASE = "http://localhost:18001/api/v1"
API_KEY = "test_api_key_12345678"
headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

print("\n" + "="*70)
print("AWBotNest API 可用性测试报告")
print("="*70)

results = []

def test_endpoint(method, endpoint, data=None, expected_status=[200], description=""):
    """测试单个端点"""
    try:
        if method == "GET":
            resp = requests.get(f"{API_BASE}{endpoint}", headers=headers, timeout=5)
        elif method == "POST":
            resp = requests.post(f"{API_BASE}{endpoint}", headers=headers, json=data, timeout=5)
        else:
            return {"status": "SKIP", "reason": "不支持的方法"}

        if resp.status_code in expected_status:
            return {"status": "✓ 可用", "code": resp.status_code}
        elif resp.status_code == 404:
            return {"status": "✗ 不存在", "code": 404}
        elif resp.status_code == 400:
            return {"status": "⚠ 需要参数", "code": 400, "error": resp.json().get("detail", "")}
        else:
            return {"status": "✗ 错误", "code": resp.status_code, "error": resp.text[:100]}
    except Exception as e:
        return {"status": "✗ 异常", "error": str(e)[:50]}

# 测试各类端点
tests = [
    ("GET", "/status", None, [200], "平台状态"),
    ("GET", "/plugins", None, [200], "插件列表"),
    ("GET", "/plugins/auto_lottery", None, [200], "插件详情"),
    ("GET", "/plugins/auto_lottery/config", None, [200], "插件配置"),
    ("GET", "/accounts", None, [200], "账号列表"),
    ("GET", "/logs?limit=10", None, [200], "日志查询"),
    ("GET", "/settings", None, [200], "系统设置"),
    ("POST", "/messages/send", {"chat_id": "test", "text": "test"}, [200, 400], "发送消息"),
    ("GET", "/chats/123456", None, [200, 400, 404], "Chat 信息"),
]

print("\n{:<10} {:<35} {:<15} {}".format("方法", "端点", "状态", "说明"))
print("-"*70)

for method, endpoint, data, expected, desc in tests:
    result = test_endpoint(method, endpoint, data, expected, desc)
    status = result["status"]
    code = result.get("code", "")
    error = result.get("error", "")

    status_display = f"{status} ({code})" if code else status
    print("{:<10} {:<35} {:<15} {}".format(method, endpoint, status_display, desc))

    if error:
        print(f"           └─ 错误: {error}")

print("\n" + "="*70)
print("总结")
print("="*70)
print("✓ 可用     - API 端点正常工作")
print("⚠ 需要参数 - API 存在但需要正确的参数")
print("✗ 不存在   - API 端点未定义")
print("✗ 错误     - API 执行出错")
print("="*70)
