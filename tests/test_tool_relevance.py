"""Tool-list narrowing: real Ollama testing (qwen2.5:7b-instruct, qwen3:8b) showed small models hallucinate
or deny having tools when handed all 31 at once. These tests pin the fix: a small always-available core plus
keyword-matched extras, bounded well below the full count."""
from friday.tools.registry import ALWAYS_AVAILABLE, _terms


def test_always_available_tools_present_regardless_of_message(svc):
    for text in ["hi", "thanks", "zzz qqq unrelated nonsense", ""]:
        names = svc.registry.relevant(text)
        for core in ALWAYS_AVAILABLE:
            assert core in names, (text, core)
        assert len(names) == len(ALWAYS_AVAILABLE)   # nothing extra matched noise


def test_result_is_always_bounded_well_below_the_full_tool_count(svc):
    total = len(svc.registry.all())
    assert total > 25   # sanity: this only matters because the full list is large
    for text in ["open notepad and create a file saying hello list files read write delete move app close "
                 "powershell shell command briefing task memory finance trades journal spreadsheet report workflow",
                 "list files in Documents"]:
        names = svc.registry.relevant(text)
        assert len(names) < total
        assert len(names) == len(set(names))        # no duplicates


def test_headline_scenario_tools_are_reachable():
    # from registry fixture indirectly via svc below; kept as a plain assertion on the always-available constant
    assert {"app_open", "fs_write", "fs_list", "fs_read", "task_add", "task_list", "briefing"} <= set(ALWAYS_AVAILABLE)


def test_keyword_matching_surfaces_non_core_tools(svc):
    assert "finance_journal_analyze" in svc.registry.relevant("analyze my last 30 XAUUSD trades in journal.csv")
    assert "fs_delete" in svc.registry.relevant("delete this old file")
    assert "fs_move" in svc.registry.relevant("move this file to another folder")
    assert "app_close" in svc.registry.relevant("close chrome please")
    assert "shell_run" in svc.registry.relevant("run a powershell command to check disk space")
    assert "spreadsheet_summary" in svc.registry.relevant("summarize this spreadsheet for me")
    assert "knowledge_ingest" in svc.registry.relevant("ingest this document into the knowledge base")
    assert "workflow_run" in svc.registry.relevant("start business mode")
    assert "browser_read" in svc.registry.relevant("read this web page for me")


def test_tokenizer_splits_underscored_literals_in_descriptions():
    # "business_mode" inside a tool's description must match a user typing "business mode" as two words.
    assert _terms("business_mode") == {"business", "mode"}
    assert _terms("fs_delete") == {"fs", "delete"}


def test_relevant_names_all_resolve_to_real_tools(svc):
    for text in ["open notepad and create a file saying hello", "analyze xauusd trades", "what should I do next",
                 "prepare the cyzy operations report", "ollama is not connecting", "close telegram"]:
        for name in svc.registry.relevant(text):
            assert svc.registry.get(name) is not None, name
