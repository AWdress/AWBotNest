import asyncio
import base64
from types import SimpleNamespace

import kernel.ai as ai_module
from kernel.ai import PluginAI, normalize_ai_settings


def test_old_ai_settings_get_longer_image_timeout():
    settings = normalize_ai_settings({"timeout_seconds": 60})

    assert settings["timeout_seconds"] == 60
    assert settings["image_timeout_seconds"] == 300


def test_ai_timeouts_stay_within_supported_range():
    settings = normalize_ai_settings({
        "timeout_seconds": 999,
        "image_timeout_seconds": 5,
    })

    assert settings["timeout_seconds"] == 300
    assert settings["image_timeout_seconds"] == 30


def test_generate_image_uses_image_timeout(tmp_path, monkeypatch):
    settings = normalize_ai_settings({
        "providers": [{
            "id": "provider",
            "name": "Provider",
            "enabled": True,
            "base_url": "https://example.com/v1",
            "api_key": "key",
        }],
        "models": [
            {
                "id": "image-model",
                "alias": "image",
                "name": "Image",
                "enabled": True,
                "provider_id": "provider",
                "model": "image-v1",
                "capabilities": ["image"],
            },
            {
                "id": "other-image-model",
                "alias": "other-image",
                "name": "Other image",
                "enabled": True,
                "provider_id": "provider",
                "model": "image-v2",
                "capabilities": ["image"],
            },
        ],
        "capabilities": {
            "image": {"default_model": "other-image-model", "fallback_model": ""},
        },
        "plugin_permissions": {
            "demo": {
                "enabled": True,
                "capabilities": ["image"],
                "models": {"image": "image-model"},
            },
        },
        "timeout_seconds": 60,
        "image_timeout_seconds": 300,
    })
    captured_timeouts = []
    captured_models = []
    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    class FakeImages:
        async def generate(self, **kwargs):
            captured_models.append(kwargs["model"])
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(pixel).decode())],
            )

    class FakeClient:
        images = FakeImages()

        async def close(self):
            return None

    def fake_client(provider, timeout):
        captured_timeouts.append(timeout)
        return FakeClient()

    monkeypatch.setattr(ai_module, "load_ai_settings", lambda: settings)
    monkeypatch.setattr(ai_module, "_client", fake_client)

    result = asyncio.run(
        PluginAI("demo", tmp_path).generate_image("test", model="other-image"),
    )

    assert result.exists()
    assert captured_timeouts == [300]
    assert captured_models == ["image-v1"]


def test_plugin_model_assignment_rejects_wrong_capability():
    settings = normalize_ai_settings({
        "providers": [{
            "id": "provider",
            "name": "Provider",
            "enabled": True,
            "base_url": "https://example.com/v1",
        }],
        "models": [{
            "id": "text-model",
            "alias": "text",
            "name": "Text",
            "enabled": True,
            "provider_id": "provider",
            "model": "text-v1",
            "capabilities": ["text"],
        }],
        "plugin_permissions": {
            "demo": {
                "models": {
                    "text": "text-model",
                    "image": "text-model",
                },
            },
        },
    })

    assigned = settings["plugin_permissions"]["demo"]["models"]
    assert assigned["text"] == "text-model"
    assert assigned["image"] == ""
