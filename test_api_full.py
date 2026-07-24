"""
完整测试 API 功能（读取、写入、操作）
"""
import requests
import json
import sys
import io

# 设置 UTF-8 输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API_BASE = "http://localhost:18001/api/v1"
API_KEY = "test_api_key_12345678"
headers = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}

def test_get(endpoint, description):
    """测试 GET 请求"""
    print(f"\n{'='*60}")
    print(f"测试: {description}")
    print(f"GET {endpoint}")
    print('='*60)
    try:
        resp = requests.get(f"{API_BASE}{endpoint}", headers=headers, timeout=5)
        print(f"状态码: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"响应数据:")
            print(json.dumps(data, indent=2, ensure_ascii=False)[:500])  # 限制输出长度
            print("✓ 成功")
            return data
        else:
            print(f"错误: {resp.text}")
            print("✗ 失败")
            return None
    except Exception as e:
        print(f"异常: {e}")
        print("✗ 失败")
        return None

def test_post(endpoint, data, description):
    """测试 POST 请求"""
    print(f"\n{'='*60}")
    print(f"测试: {description}")
    print(f"POST {endpoint}")
    print(f"数据: {json.dumps(data, ensure_ascii=False)}")
    print('='*60)
    try:
        resp = requests.post(f"{API_BASE}{endpoint}", headers=headers, json=data, timeout=5)
        print(f"状态码: {resp.status_code}")
        if resp.status_code in [200, 201]:
            result = resp.json()
            print(f"响应: {json.dumps(result, indent=2, ensure_ascii=False)[:300]}")
            print("✓ 成功")
            return result
        else:
            print(f"错误: {resp.text[:300]}")
            print("✗ 失败")
            return None
    except Exception as e:
        print(f"异常: {e}")
        print("✗ 失败")
        return None

print("\n" + "="*60)
print("AWBotNest API 完整功能测试")
print("="*60)

# 1. 平台状态
status = test_get("/status", "获取平台状态")

# 2. 插件列表
plugins = test_get("/plugins", "获取所有插件")

# 3. 账号列表
accounts = test_get("/accounts", "获取所有账号")

# 4. 日志查询（最近10条）
logs = test_get("/logs?limit=10", "获取最近日志")

# 5. 测试插件详情（如果有插件）
if plugins and plugins.get('plugins'):
    plugin_id = plugins['plugins'][0]['id']
    test_get(f"/plugins/{plugin_id}", f"获取插件详情: {plugin_id}")

# 6. 测试配置 API（如果插件有配置）
if plugins and plugins.get('plugins'):
    for plugin in plugins['plugins']:
        if plugin.get('has_config'):
            plugin_id = plugin['id']
            test_get(f"/plugins/{plugin_id}/config", f"获取插件配置: {plugin_id}")
            break

# 7. 测试发送消息 API
test_post("/messages/send", {
    "text": "API 测试消息 - 来自自动化测试",
    "parse_mode": "HTML"
}, "发送测试消息到默认 Bot")

# 8. 测试 Webhook 端点（如果配置了 WEBHOOK_SECRET）
# test_post("/webhook?apikey=xxx", {"message": "test"}, "测试 Webhook")

# 9. 测试获取 Chat 信息（需要 chat_id）
# test_get("/chats/me", "获取当前用户信息")

print("\n" + "="*60)
print("测试完成")
print("="*60)
