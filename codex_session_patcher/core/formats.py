# -*- coding: utf-8 -*-
"""
会话格式策略 — 支持 Codex CLI 和 Claude Code 两种 JSONL 格式
"""
from __future__ import annotations

import copy
import json
import logging
import os
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, List, Any, Tuple

logger = logging.getLogger(__name__)


class SessionFormat(Enum):
    CODEX = "codex"
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    GROK = "grok"


# ─── 策略基类 ─────────────────────────────────────────────────────────────────

class FormatStrategy(ABC):
    """格式特定操作的抽象基类"""

    @abstractmethod
    def get_assistant_messages(self, lines: List[Dict[str, Any]]) -> List[Tuple[int, Dict[str, Any]]]:
        """返回 [(行索引, 消息数据), ...]"""

    @abstractmethod
    def get_thinking_items(self, lines: List[Dict[str, Any]]) -> List[Tuple[int, Dict[str, Any]]]:
        """返回需要整行删除的 thinking/reasoning 条目"""

    @abstractmethod
    def extract_text_content(self, msg: Dict[str, Any]) -> str:
        """从一条助手消息中提取纯文本"""

    @abstractmethod
    def update_text_content(self, msg: Dict[str, Any], new_text: str) -> Dict[str, Any]:
        """替换消息中的文本内容，返回深拷贝"""

    def remove_thinking_from_message(self, msg: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """移除消息 content 数组中嵌入的 thinking 块。
        返回 (更新后的消息, 被移除的数量)。
        默认实现：不做任何事（Codex 的 thinking 是独立行）。
        """
        return msg, 0


# ─── Codex 策略 ───────────────────────────────────────────────────────────────

class CodexFormatStrategy(FormatStrategy):
    """Codex CLI 格式：response_item + payload 包装"""

    def get_assistant_messages(self, lines):
        messages = []
        for idx, line in enumerate(lines):
            line_type = line.get('type')
            payload = line.get('payload', {})

            # response_item: role=assistant（主消息结构）
            if line_type == 'response_item':
                if payload.get('type') == 'message' and payload.get('role') == 'assistant':
                    messages.append((idx, line))

            # event_msg: agent_message（assistant 回复的冗余副本，resume 时展示用）
            elif line_type == 'event_msg':
                pt = payload.get('type')
                if pt == 'agent_message' and payload.get('message'):
                    messages.append((idx, line))
                elif pt == 'task_complete' and payload.get('last_agent_message'):
                    messages.append((idx, line))

        return messages

    def get_thinking_items(self, lines):
        items = []
        for idx, line in enumerate(lines):
            if line.get('type') == 'response_item':
                payload = line.get('payload', {})
                if payload.get('type') == 'reasoning':
                    items.append((idx, line))
        return items

    def extract_text_content(self, msg):
        line_type = msg.get('type')
        payload = msg.get('payload', {})

        # event_msg/agent_message
        if line_type == 'event_msg':
            pt = payload.get('type')
            if pt == 'agent_message':
                return payload.get('message', '')
            if pt == 'task_complete':
                return payload.get('last_agent_message', '')
            return ''

        # response_item/assistant
        content = payload.get('content', [])
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'output_text':
                    texts.append(item.get('text', ''))
            return '\n'.join(texts)
        return ''

    def update_text_content(self, msg, new_text):
        updated = copy.deepcopy(msg)
        line_type = updated.get('type')
        payload = updated.get('payload', {})

        # event_msg/agent_message 和 event_msg/task_complete
        if line_type == 'event_msg':
            pt = payload.get('type')
            if pt == 'agent_message':
                payload['message'] = new_text
            elif pt == 'task_complete':
                payload['last_agent_message'] = new_text
            return updated

        # response_item/assistant
        content = payload.get('content', [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'output_text':
                    item['text'] = new_text
        else:
            payload['content'] = [{'type': 'output_text', 'text': new_text}]
        return updated


# ─── Claude Code 策略 ─────────────────────────────────────────────────────────

class ClaudeCodeFormatStrategy(FormatStrategy):
    """Claude Code 格式：顶层 type=assistant，message.content 包含 thinking/text 等"""

    def get_assistant_messages(self, lines):
        messages = []
        for idx, line in enumerate(lines):
            if line.get('type') == 'assistant':
                msg = line.get('message', {})
                if msg.get('role') == 'assistant':
                    messages.append((idx, line))
        return messages

    def get_thinking_items(self, lines):
        # Claude Code 的 thinking 嵌入在 message.content[] 中，不是独立行
        return []

    def extract_text_content(self, msg):
        message = msg.get('message', {})
        content = message.get('content', [])
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    texts.append(item.get('text', ''))
            return '\n'.join(texts)
        return ''

    def update_text_content(self, msg, new_text):
        updated = copy.deepcopy(msg)
        message = updated.get('message', {})
        content = message.get('content', [])
        if isinstance(content, list):
            replaced = False
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    item['text'] = new_text
                    replaced = True
                    break
            if not replaced:
                content.append({'type': 'text', 'text': new_text})
        else:
            message['content'] = [{'type': 'text', 'text': new_text}]
        return updated

    def remove_thinking_from_message(self, msg):
        updated = copy.deepcopy(msg)
        message = updated.get('message', {})
        content = message.get('content', [])
        if not isinstance(content, list):
            return updated, 0
        original_len = len(content)
        message['content'] = [
            item for item in content
            if not (isinstance(item, dict) and item.get('type') == 'thinking')
        ]
        removed = original_len - len(message['content'])
        return updated, removed


# ─── OpenCode 策略 ───────────────────────────────────────────────────────────

class OpenCodeFormatStrategy(FormatStrategy):
    """OpenCode 格式：从 SQLite 转换后的 dict，结构与 Claude Code 类似。

    消息格式（由 sqlite_adapter 转换）:
    {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "..."},
            {"type": "thinking", "text": "..."},  # reasoning 映射为 thinking
        ]},
        "_oc_msg_id": "msg_xxx",
        "_oc_parts": [...]
    }
    """

    def get_assistant_messages(self, lines: List[Dict[str, Any]]) -> List[Tuple[int, Dict[str, Any]]]:
        messages = []
        for idx, line in enumerate(lines):
            if line.get('type') == 'assistant':
                msg = line.get('message', {})
                if msg.get('role') == 'assistant':
                    messages.append((idx, line))
        return messages

    def get_thinking_items(self, lines: List[Dict[str, Any]]) -> List[Tuple[int, Dict[str, Any]]]:
        # OpenCode 的 thinking 嵌入在 message.content[] 中（与 Claude Code 相同）
        return []

    def extract_text_content(self, msg: Dict[str, Any]) -> str:
        message = msg.get('message', {})
        content = message.get('content', [])
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    texts.append(item.get('text', ''))
            return '\n'.join(texts)
        return ''

    def update_text_content(self, msg: Dict[str, Any], new_text: str) -> Dict[str, Any]:
        updated = copy.deepcopy(msg)
        message = updated.get('message', {})
        content = message.get('content', [])
        if isinstance(content, list):
            replaced = False
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    item['text'] = new_text
                    replaced = True
                    break
            if not replaced:
                content.append({'type': 'text', 'text': new_text})
        else:
            message['content'] = [{'type': 'text', 'text': new_text}]
        return updated

    def remove_thinking_from_message(self, msg: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        updated = copy.deepcopy(msg)
        message = updated.get('message', {})
        content = message.get('content', [])
        if not isinstance(content, list):
            return updated, 0
        original_len = len(content)
        message['content'] = [
            item for item in content
            if not (isinstance(item, dict) and item.get('type') == 'thinking')
        ]
        removed = original_len - len(message['content'])
        return updated, removed

# ─── Grok 策略 ───────────────────────────────────────────────────────────────

class GrokFormatStrategy(FormatStrategy):
    """Grok 格式：chat_history.jsonl 中的 assistant 消息，content 可能为空（tool_calls 时使用 synthetic_reason 或 content 字符串）"""

    def get_assistant_messages(self, lines: List[Dict[str, Any]]) -> List[Tuple[int, Dict[str, Any]]]:
        messages = []
        for idx, line in enumerate(lines):
            if line.get('type') == 'assistant':
                messages.append((idx, line))
        return messages

    def get_thinking_items(self, lines: List[Dict[str, Any]]) -> List[Tuple[int, Dict[str, Any]]]:
        # Grok reasoning (synthetic_reason) is embedded on assistant rows —
        # strip via remove_thinking_from_message, never delete whole turns.
        return []

    def extract_text_content(self, msg: Dict[str, Any]) -> str:
        content = msg.get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    texts.append(item.get('text', ''))
            return '\n'.join(texts)
        # fallback for empty content: synthetic_reason or other
        synthetic = msg.get('synthetic_reason')
        if synthetic:
            return synthetic
        tool_call_summary = msg.get('tool_calls', [])
        if tool_call_summary:
            return f"[tool call: {len(tool_call_summary)} calls]"
        return ''

    def update_text_content(self, msg: Dict[str, Any], new_text: str) -> Dict[str, Any]:
        updated = copy.deepcopy(msg)
        content = updated.get('content')
        if isinstance(content, str):
            updated['content'] = new_text
            return updated
        if isinstance(content, list):
            replaced = False
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    item['text'] = new_text
                    replaced = True
                    break
            if not replaced:
                content.append({'type': 'text', 'text': new_text})
        else:
            updated['content'] = new_text
        return updated

    def remove_thinking_from_message(self, msg: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        updated = copy.deepcopy(msg)
        if 'synthetic_reason' in updated:
            removed = 1
            del updated['synthetic_reason']
            return updated, removed
        return updated, 0


# ─── 工厂 & 工具函数 ──────────────────────────────────────────────────────────

def get_format_strategy(fmt: SessionFormat) -> FormatStrategy:
    if fmt == SessionFormat.CODEX:
        return CodexFormatStrategy()
    elif fmt == SessionFormat.CLAUDE_CODE:
        return ClaudeCodeFormatStrategy()
    elif fmt == SessionFormat.OPENCODE:
        return OpenCodeFormatStrategy()
    elif fmt == SessionFormat.GROK:
        return GrokFormatStrategy()
    raise ValueError(f"未知的会话格式: {fmt}")


def detect_session_format(file_path: str) -> SessionFormat:
    """通过读取 JSONL 文件的前 20 行来判断格式。

    Claude Code nests under ``message``; Grok uses top-level ``content`` plus
    optional ``model_id`` / ``tool_calls`` / ``synthetic_reason``. Path fallback
    covers ``~/.grok/sessions/`` trees when content is ambiguous.
    """
    # Path is authoritative for known roots (avoids misclassifying Grok system rows as Claude).
    path_guess = _detect_format_from_path(file_path)
    if path_guess != SessionFormat.CODEX or "/.grok/sessions/" in os.path.expanduser(file_path):
        # Non-default path guess (or explicit Grok tree) wins immediately.
        if path_guess == SessionFormat.GROK or "/.grok/sessions/" in os.path.expanduser(file_path):
            return SessionFormat.GROK
        if path_guess in (SessionFormat.CLAUDE_CODE, SessionFormat.OPENCODE):
            return path_guess

    saw_claude = False
    saw_grok = False
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for i, raw_line in enumerate(f):
                if i >= 20:
                    break
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                line_type = data.get('type', '')
                if line_type == 'response_item' or line_type == 'event_msg':
                    return SessionFormat.CODEX
                if line_type in ('file-history-snapshot', 'last-prompt'):
                    return SessionFormat.CLAUDE_CODE
                if line_type in ('assistant', 'user', 'system'):
                    # Claude Code: nested message.role
                    if isinstance(data.get('message'), dict):
                        saw_claude = True
                        continue
                    # Grok signals on top-level assistant rows
                    if line_type == 'assistant' and (
                        data.get('model_id')
                        or data.get('tool_calls') is not None
                        or data.get('synthetic_reason')
                        or 'content' in data
                    ):
                        saw_grok = True
                        continue
                    # bare system/user with top-level content — likely Grok, keep scanning
                    if 'content' in data and 'message' not in data:
                        saw_grok = True
                        continue
    except Exception:
        logger.warning("检测会话格式失败: %s", file_path, exc_info=True)
        return _detect_format_from_path(file_path)

    if saw_grok and not saw_claude:
        return SessionFormat.GROK
    if saw_claude:
        return SessionFormat.CLAUDE_CODE
    return _detect_format_from_path(file_path)


def _detect_format_from_path(file_path: str) -> SessionFormat:
    """根据文件所在目录推测格式"""
    expanded = os.path.expanduser(file_path)
    codex_dir = os.path.expanduser("~/.codex/")
    claude_dir = os.path.expanduser("~/.claude/")
    opencode_dir = os.path.expanduser("~/.local/share/opencode/")
    grok_dir = os.path.expanduser("~/.grok/sessions/")
    if expanded.startswith(codex_dir):
        return SessionFormat.CODEX
    if expanded.startswith(claude_dir):
        return SessionFormat.CLAUDE_CODE
    if expanded.startswith(opencode_dir) or expanded.endswith('.db'):
        return SessionFormat.OPENCODE
    if expanded.startswith(grok_dir) or "/.grok/sessions/" in expanded or expanded.endswith("/.grok/sessions"):
        return SessionFormat.GROK
    return SessionFormat.CODEX  # 默认回退


def decode_claude_project_path(encoded: str) -> str:
    """将 Claude Code 的编码目录名转回文件系统路径。
    例："-Users-foo-bar" → "/Users/foo/bar"
    """
    if not encoded or not encoded.startswith('-'):
        return encoded
    # 去掉首个 '-'，然后将 '-' 替换为 '/'
    return '/' + encoded[1:].replace('-', '/')
