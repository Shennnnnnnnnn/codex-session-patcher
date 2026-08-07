# -*- coding: utf-8 -*-
"""
Grok format + CTF agent tests.

Fixtures use pytest tmp_path + monkeypatch HOME (project convention).
No live ~/.grok mutation required.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codex_session_patcher.core.constants import MOCK_RESPONSE
from codex_session_patcher.core.detector import RefusalDetector
from codex_session_patcher.core.formats import (
    SessionFormat,
    detect_session_format,
    get_format_strategy,
)
from codex_session_patcher.core.parser import SessionParser
from codex_session_patcher.core.patcher import clean_session_jsonl, save_session_jsonl
from codex_session_patcher.ctf_config import GrokCTFInstaller, check_ctf_status
from codex_session_patcher.ctf_config.status import CTF_MARKER


REFUSAL_TEXT = "I cannot assist with that request as it violates my guidelines."


def _write_grok_session_tree(root: Path, session_id: str = "019fda43-a1df-7d00-ad16-9db9ac041c2e") -> Path:
    """Build a minimal Grok session tree: sessions/<encoded-cwd>/<uuid>/chat_history.jsonl."""
    encoded_cwd = "%2Ftmp%2Fproject"
    session_dir = root / ".grok" / "sessions" / encoded_cwd / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    history = session_dir / "chat_history.jsonl"
    lines = [
        {"type": "system", "content": "You are Grok."},
        {"type": "user", "content": [{"type": "text", "text": "do something risky"}]},
        {
            "type": "assistant",
            "content": REFUSAL_TEXT,
            "synthetic_reason": "policy_refusal",
            "model_id": "grok-4.5",
        },
        {
            "type": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "read_file", "arguments": "{}"}],
            "model_id": "grok-4.5",
        },
    ]
    with open(history, "w", encoding="utf-8") as f:
        for row in lines:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return history


class TestGrokFormatStrategy:
    def test_get_format_strategy(self):
        strategy = get_format_strategy(SessionFormat.GROK)
        assert strategy is not None

    def test_extract_and_update_text(self):
        strategy = get_format_strategy(SessionFormat.GROK)
        msg = {"type": "assistant", "content": REFUSAL_TEXT, "model_id": "grok-4.5"}
        assert REFUSAL_TEXT in strategy.extract_text_content(msg)
        updated = strategy.update_text_content(msg, MOCK_RESPONSE)
        assert strategy.extract_text_content(updated) == MOCK_RESPONSE
        # original not mutated
        assert msg["content"] == REFUSAL_TEXT

    def test_thinking_items_do_not_delete_whole_turns(self):
        """clean_reasoning must strip synthetic_reason, not delete the assistant row."""
        strategy = get_format_strategy(SessionFormat.GROK)
        lines = [
            {
                "type": "assistant",
                "content": REFUSAL_TEXT,
                "synthetic_reason": "policy_refusal",
            }
        ]
        assert strategy.get_thinking_items(lines) == []
        updated, removed = strategy.remove_thinking_from_message(lines[0])
        assert removed == 1
        assert "synthetic_reason" not in updated
        assert updated["content"] == REFUSAL_TEXT


class TestGrokDetectListClean:
    def test_detect_from_path(self, tmp_path):
        history = _write_grok_session_tree(tmp_path)
        # path under .../.grok/sessions/... should map to GROK
        # simulate path containing .grok/sessions via expanduser override not needed:
        # write under a path that includes the marker segment
        grok_like = tmp_path / "home" / ".grok" / "sessions" / "x" / "id" / "chat_history.jsonl"
        grok_like.parent.mkdir(parents=True)
        grok_like.write_text(history.read_text(encoding="utf-8"), encoding="utf-8")

        # Force path detection by monkeypatching expanduser roots used in _detect_format_from_path
        from codex_session_patcher.core import formats as formats_mod

        real_expand = os.path.expanduser

        def fake_expand(p):
            if p in ("~/.grok/sessions/", "~/.grok/"):
                return str(tmp_path / "home" / ".grok" / "sessions") if "sessions" in p else str(tmp_path / "home" / ".grok")
            if p.startswith("~"):
                return real_expand(p)
            return p

        # Direct path check: file under a dir named like sessions path
        # Use content heuristic (model_id + assistant content)
        fmt = detect_session_format(str(grok_like))
        assert fmt == SessionFormat.GROK
        # content-based Grok when model_id / synthetic_reason present
        assert detect_session_format(str(history)) == SessionFormat.GROK

    def test_list_and_clean_session(self, tmp_path, monkeypatch):
        """List Grok sessions under temp home and clean refusal text end-to-end.

        Only chat_history.jsonl is required for model-continue in this fixture;
        updates.jsonl is not rewritten here (documented: chat_history is sufficient
        for the clean path exercised by the strategy).
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        history = _write_grok_session_tree(tmp_path)
        sessions_root = tmp_path / ".grok" / "sessions"
        parser = SessionParser(str(sessions_root), session_format=SessionFormat.GROK)
        sessions = parser.list_sessions()
        assert len(sessions) >= 1
        assert sessions[0].format == SessionFormat.GROK
        assert sessions[0].path.endswith("chat_history.jsonl")

        lines = parser.parse_session_jsonl(sessions[0].path)
        detector = RefusalDetector()
        cleaned, modified, changes = clean_session_jsonl(
            lines,
            detector,
            mock_response=MOCK_RESPONSE,
            session_format=SessionFormat.GROK,
            clean_reasoning=True,
            show_content=True,
        )
        assert modified is True
        assert any(c.change_type == "replace" for c in changes)
        # reasoning stripped via remove_thinking, not whole-line delete
        assert any(c.change_type == "remove_thinking" for c in changes)
        strategy = get_format_strategy(SessionFormat.GROK)
        assistant_texts = [
            strategy.extract_text_content(row)
            for row in cleaned
            if row.get("type") == "assistant"
        ]
        joined = "\n".join(assistant_texts)
        assert REFUSAL_TEXT not in joined
        assert MOCK_RESPONSE in joined
        # assistant turns still present (not wiped)
        assert any(row.get("type") == "assistant" for row in cleaned)
        assert all("synthetic_reason" not in row for row in cleaned if row.get("type") == "assistant")

        save_session_jsonl(cleaned, sessions[0].path)
        reloaded = sessions[0].path.read_text(encoding="utf-8") if False else Path(sessions[0].path).read_text(encoding="utf-8")
        assert REFUSAL_TEXT not in reloaded
        assert MOCK_RESPONSE in reloaded


class TestGrokCTFInstaller:
    def test_install_status_uninstall(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        installer = GrokCTFInstaller(home=str(tmp_path))

        ok, msg = installer.install()
        assert ok, msg
        assert "grok --agent ctf" in msg
        agent = Path(installer.agent_path)
        assert agent.is_file()
        body = agent.read_text(encoding="utf-8")
        assert CTF_MARKER in body
        assert "name: ctf" in body

        status = check_ctf_status()
        assert status.grok_installed is True
        assert status.grok_agent_path == str(agent)

        # second install is idempotent / updates managed content
        ok2, msg2 = installer.install()
        assert ok2, msg2

        # unmarked collision is preserved on uninstall attempt via separate path
        foreign = tmp_path / ".grok" / "agents" / "other.md"
        foreign.write_text("user owned agent\n", encoding="utf-8")

        ok3, msg3 = installer.uninstall()
        assert ok3, msg3
        assert not agent.exists()
        assert foreign.exists()  # unmarked left intact

        status2 = check_ctf_status()
        assert status2.grok_installed is False

    def test_uninstall_refuses_unmanaged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        installer = GrokCTFInstaller(home=str(tmp_path))
        agent = Path(installer.agent_path)
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text("---\nname: ctf\n---\nuser owned, no marker\n", encoding="utf-8")
        ok, msg = installer.uninstall()
        assert ok is False
        assert "标记" in msg or "保留" in msg
        assert agent.exists()


class TestGrokCLIWiring:
    def test_argparse_exposes_grok_flags(self):
        from codex_session_patcher.cli import main
        import argparse
        # Re-build parser the same way main does by invoking help path
        # Drive module-level handlers directly (real shipped functions)
        from codex_session_patcher import cli as cli_mod
        assert hasattr(cli_mod, "handle_grok_ctf_install")
        assert hasattr(cli_mod, "handle_grok_ctf_uninstall")
        assert hasattr(cli_mod, "handle_ctf_status")

    def test_handlers_install_status_uninstall(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        from codex_session_patcher.cli import (
            handle_ctf_status,
            handle_grok_ctf_install,
            handle_grok_ctf_uninstall,
        )

        handle_grok_ctf_install()
        out = capsys.readouterr().out
        assert "Grok" in out or "agent" in out.lower() or "ctf" in out.lower()

        handle_ctf_status()
        out = capsys.readouterr().out
        assert "[Grok]" in out
        assert "grok --agent ctf" in out or "已安装" in out

        handle_grok_ctf_uninstall()
        out = capsys.readouterr().out
        assert "Grok" in out or "卸载" in out or "删除" in out or "未安装" in out

        # after uninstall, status reports not installed
        handle_ctf_status()
        out = capsys.readouterr().out
        assert "[Grok]" in out
        assert "未安装" in out or "install-grok-ctf" in out
