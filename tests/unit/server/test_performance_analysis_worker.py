"""启动要求事实导出能力，不启动服务端分析任务。"""

import pytest

from axile.server import performance_analysis as analysis


@pytest.mark.parametrize(
    "name",
    [
        "browser_analysis_state",
        "browser_analysis_facts",
        "analysis_context",
        "analysis_calendar_save",
        "account_public",
        "close_database",
    ],
)
def test_browser_fact_capabilities_required(monkeypatch, name):
    analysis.ensure_native_capabilities()
    monkeypatch.delattr(analysis._core, name)
    with pytest.raises(RuntimeError, match=name):
        analysis.ensure_native_capabilities()


def test_obsolete_fact_schema_rejected(monkeypatch):
    monkeypatch.setattr(analysis._core, "ANALYSIS_FACT_SCHEMA", 39)
    with pytest.raises(RuntimeError, match="rebuild the wheel"):
        analysis.ensure_native_capabilities()


def test_obsolete_migration_head_rejected(monkeypatch):
    monkeypatch.setattr(analysis._core, "PUBLIC_HEAD", "0037")
    with pytest.raises(RuntimeError, match="migration head 0038"):
        analysis.ensure_native_capabilities()


def test_no_backend_analysis_manager():
    assert not hasattr(analysis, "AnalysisManager")
