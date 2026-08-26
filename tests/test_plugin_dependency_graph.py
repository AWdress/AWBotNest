from kernel.plugin_runtime import PluginRuntime
from kernel.registry import PluginMeta


def test_dependency_graph_includes_python_requirements(monkeypatch):
    plugin = PluginMeta(
        id="awpulse",
        name="AWPulse 色花堂助手",
        version="1.1.9",
        requirements=["cloakbrowser>=0.4.9", "requests>=2.32.0"],
        requires_plugins=["helper"],
    )
    helper = PluginMeta(id="helper", name="Helper")
    monkeypatch.setattr("kernel.plugin_runtime.registry.scan", lambda: [plugin, helper])

    graph = PluginRuntime(accounts=None).dependency_graph()

    assert {
        (edge["type"], edge["to"], edge["missing"])
        for edge in graph["edges"]
        if edge["from"] == "awpulse"
    } == {
        ("plugin", "helper", False),
        ("python", "cloakbrowser>=0.4.9", False),
        ("python", "requests>=2.32.0", False),
    }
