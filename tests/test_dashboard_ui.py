from pathlib import Path


APP_JS = Path(__file__).parents[1] / "static" / "app.js"


def test_dashboard_keeps_demo_graph_separate_from_live_wfg():
    source = APP_JS.read_text(encoding="utf-8")

    assert "let demoWfg = null;" in source
    assert 'const wfg = demoWfg || state.wfg || {};' in source
    assert 'demoWfg = d.graph && typeof d.graph === "object" ? d.graph : null;' in source
    assert 'demoWfg = null;' in source


def test_dashboard_uses_api_cycle_for_demo_graph():
    source = APP_JS.read_text(encoding="utf-8")

    assert "new Set(wfg.cycle)" in source
    assert "detectCycle(edges)" in source
    assert "Last deadlock demo graph" in source
