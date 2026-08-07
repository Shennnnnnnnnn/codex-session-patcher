# -*- coding: utf-8 -*-
"""
CTF 配置安装器
"""
from __future__ import annotations

import os
import re
from typing import Optional

from ..config import load_config
from ..file_ops import (
    atomic_write_json,
    atomic_write_text,
    create_unique_backup,
    require_regular_or_missing,
)
from .templates import (
    CTF_CONFIG_TEMPLATE, SECURITY_MODE_PROMPT,
    CLAUDE_CODE_SECURITY_MODE_PROMPT, CLAUDE_CODE_CTF_README,
    OPENCODE_SECURITY_MODE_PROMPT, OPENCODE_CTF_CONFIG, OPENCODE_CTF_README,
    BUILTIN_TEMPLATES,
)
from .status import (
    check_ctf_status, CTFStatus, GLOBAL_MARKER, CTF_MARKER, PROFILE_MARKER,
    ensure_ctf_marker, has_ctf_marker,
    DEFAULT_CLAUDE_CTF_WORKSPACE, DEFAULT_OPENCODE_CTF_WORKSPACE,
)


def _write_workspace_prompt(path: str, content: str) -> tuple[str, Optional[str]]:
    """安全写入专用工作空间提示词，并拒绝接管无标记文件。"""
    managed_content = ensure_ctf_marker(content)
    backup_path = None
    if os.path.exists(path):
        require_regular_or_missing(path)
        with open(path, 'r', encoding='utf-8') as f:
            actual = f.read()
        if not has_ctf_marker(actual):
            raise ValueError(f"提示词文件没有本工具管理标记，已保留: {path}")
        if actual == managed_content:
            return managed_content, None
        backup_path = create_unique_backup(path)
    atomic_write_text(path, managed_content)
    return managed_content, backup_path


class CTFConfigInstaller:
    """CTF 配置安装器"""

    DEFAULT_PROMPT_FILE = "ctf_optimized.md"
    INJECTION_MODE_APPEND = "append"
    INJECTION_MODE_REPLACE = "replace"

    def __init__(self):
        self.codex_dir = os.path.expanduser("~/.codex")
        self.config_path = os.path.join(self.codex_dir, "config.toml")
        self.profile_config_path = os.path.join(self.codex_dir, "ctf.config.toml")
        self.prompts_dir = os.path.join(self.codex_dir, "prompts")

    def _get_prompt_file(self) -> str:
        """从用户配置获取当前选中的模板文件名，没有则返回默认"""
        config = load_config()
        filename = config.get('ctf_prompts', {}).get('codex', {}).get('file') or self.DEFAULT_PROMPT_FILE
        if not isinstance(filename, str):
            raise ValueError("Codex 提示词文件名必须是字符串")
        if os.path.basename(filename) != filename or not filename.endswith('.md'):
            raise ValueError("Codex 提示词文件名必须是 prompts 目录内的 .md 文件")
        return filename

    def _get_prompt_content(self) -> str:
        """从用户配置获取当前选中的模板内容，没有则使用默认"""
        config = load_config()
        saved = config.get('ctf_prompts', {}).get('codex', {}).get('prompt')
        if saved:
            if not isinstance(saved, str):
                raise ValueError("Codex 提示词内容必须是字符串")
            return saved
        # 使用默认模板
        from .templates import BUILTIN_TEMPLATES
        for tpl in BUILTIN_TEMPLATES.get('codex', []):
            if tpl.get('default'):
                return tpl['prompt']
        return BUILTIN_TEMPLATES['codex'][0]['prompt']

    def _normalize_injection_mode(self, injection_mode: Optional[str]) -> str:
        """归一化 Codex 提示词注入模式。"""
        mode = injection_mode or self.INJECTION_MODE_APPEND
        if mode not in {self.INJECTION_MODE_APPEND, self.INJECTION_MODE_REPLACE}:
            raise ValueError("injection_mode 只支持 append 或 replace")
        return mode

    def _read_regular_text(self, path: str) -> str:
        """读取已确认是普通文件的 UTF-8 文本。"""
        require_regular_or_missing(path)
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def _profile_is_managed(self, content: Optional[str] = None) -> bool:
        """判断新版 Profile 文件是否带有本工具标记。"""
        if content is None:
            if not os.path.exists(self.profile_config_path):
                return False
            content = self._read_regular_text(self.profile_config_path)
        return PROFILE_MARKER in content.splitlines()

    def _global_is_managed(self, content: Optional[str] = None) -> bool:
        """判断全局配置是否包含本工具管理块。"""
        if content is None:
            if not os.path.exists(self.config_path):
                return False
            content = self._read_regular_text(self.config_path)
        return GLOBAL_MARKER in content

    def _validate_profile_installable(self) -> None:
        """安装前拒绝接管用户已有的 Profile 提示词键。"""
        if not os.path.exists(self.profile_config_path):
            return
        content = self._read_regular_text(self.profile_config_path)
        if self._profile_is_managed(content):
            return
        key = self._find_unmanaged_top_level_instruction_key(content.splitlines())
        if key:
            raise ValueError(
                f"ctf.config.toml 顶层已有未受管理的 {key}，为避免覆盖用户配置，已停止安装"
            )

    def _validate_profile_uninstallable(self) -> None:
        """卸载前拒绝修改没有管理标记的 Profile 提示词键。"""
        if not os.path.exists(self.profile_config_path):
            return
        content = self._read_regular_text(self.profile_config_path)
        if self._profile_is_managed(content):
            return
        key = self._find_unmanaged_top_level_instruction_key(content.splitlines())
        if key:
            raise ValueError(
                f"ctf.config.toml 顶层 {key} 没有本工具管理标记，已保留文件"
            )

    def _validate_legacy_profile_installable(self) -> None:
        """拒绝迁移或清理没有历史管理标记的旧版 ctf profile。"""
        if not os.path.exists(self.config_path):
            return
        lines = self._read_regular_text(self.config_path).splitlines(keepends=True)
        has_legacy_profile = any(line.strip() == '[profiles.ctf]' for line in lines)
        if has_legacy_profile and not self._legacy_profile_is_managed(lines):
            raise ValueError(
                "config.toml 中存在没有本工具历史标记的 [profiles.ctf]，已保留用户配置并停止安装"
            )

    def _ensure_profile_marker(self, content: str) -> str:
        """确保新版 Profile 文件包含唯一的所有权标记。"""
        if self._profile_is_managed(content):
            return content
        if content:
            return f"{PROFILE_MARKER}\n{content.lstrip(chr(10))}"
        return f"{PROFILE_MARKER}\n"

    def _extract_embedded_instructions(self, content: str) -> Optional[str]:
        """读取本工具生成的多行 developer_instructions 内容。"""
        match = re.search(
            r'(?ms)^\s*developer_instructions\s*=\s*"""\s*\n?(.*?)\n?\s*"""',
            content,
        )
        if not match:
            return None
        return match.group(1).replace('\\"\\"\\"', '"""').replace('\\\\', '\\')

    def _managed_config_owns_legacy_prompt(self, prompt_path: str, actual: str) -> bool:
        """识别升级前已由受管理配置引用、但尚无文件标记的提示词。"""
        contents = []
        if os.path.exists(self.profile_config_path):
            profile_content = self._read_regular_text(self.profile_config_path)
            if self._profile_is_managed(profile_content):
                contents.append(profile_content)
        if os.path.exists(self.config_path):
            global_content = self._read_regular_text(self.config_path)
            if self._global_is_managed(global_content):
                contents.append(global_content)

        if not contents:
            return False

        absolute_prompt = os.path.abspath(prompt_path)
        expected = {self._get_prompt_content().strip()}
        for template in BUILTIN_TEMPLATES.get('codex', []):
            prompt = template.get('prompt')
            if isinstance(prompt, str):
                expected.add(prompt.strip())
                lines = prompt.splitlines()
                if lines and CTF_MARKER in lines[0]:
                    expected.add('\n'.join(lines[1:]).lstrip().strip())

        for content in contents:
            embedded = self._extract_embedded_instructions(content)
            if embedded is not None:
                expected.add(embedded.strip())
            match = re.search(r'(?m)^\s*model_instructions_file\s*=\s*"([^"]+)"', content)
            if match and os.path.abspath(os.path.expanduser(match.group(1))) == absolute_prompt:
                return True

        return actual.strip() in expected

    def _validate_prompt_target(self, prompt_path: str) -> None:
        """拒绝覆盖无法确认由本工具管理的同名提示词。"""
        if not os.path.exists(prompt_path):
            return
        actual = self._read_regular_text(prompt_path)
        if has_ctf_marker(actual):
            return
        if self._managed_config_owns_legacy_prompt(prompt_path, actual):
            return
        raise ValueError(f"提示词文件没有本工具管理标记，已保留: {prompt_path}")

    def _write_managed_prompt(
        self,
        prompt_path: str,
        prompt_content: str,
        *,
        already_validated: bool = False,
    ) -> tuple[str, Optional[str]]:
        """验证所有权，必要时备份并写入带标记的提示词。"""
        managed_content = ensure_ctf_marker(prompt_content)
        if not already_validated:
            self._validate_prompt_target(prompt_path)

        backup_path = None
        if os.path.exists(prompt_path):
            actual = self._read_regular_text(prompt_path)
            if actual == managed_content:
                return managed_content, None
            backup_path = create_unique_backup(prompt_path)

        atomic_write_text(prompt_path, managed_content)
        return managed_content, backup_path

    def install(self, custom_prompt: str = None, injection_mode: str = INJECTION_MODE_APPEND) -> tuple[bool, str]:
        """
        安装 Profile 模式（自动禁用 Global 模式）

        Args:
            custom_prompt: 自定义提示词内容，为 None 时从配置/默认模板读取
            injection_mode: append 表示追加 developer_instructions；replace 表示替换内置提示词

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        try:
            injection_mode = self._normalize_injection_mode(injection_mode)
            details = []

            # 1. 确定目标并在任何修改前完成所有权预检
            prompt_file = self._get_prompt_file()
            prompt_path = os.path.join(self.prompts_dir, prompt_file)
            prompt_content = ensure_ctf_marker(custom_prompt or self._get_prompt_content())
            self._validate_profile_installable()
            self._validate_legacy_profile_installable()
            self._validate_prompt_target(prompt_path)

            # 2. 确保 prompts 目录存在
            os.makedirs(self.prompts_dir, exist_ok=True)

            # 3. 在模式切换前备份现有配置
            backup_paths = []
            if os.path.exists(self.config_path):
                backup_path = self._backup_config(self.config_path)
                if backup_path:
                    backup_paths.append(backup_path)
            if os.path.exists(self.profile_config_path):
                backup_path = self._backup_config(self.profile_config_path)
                if backup_path:
                    backup_paths.append(backup_path)

            # 4. 先禁用 Global 模式；失败时不得继续写入 Profile
            status = check_ctf_status()
            if status.global_installed:
                success, msg = self.uninstall_global()
                if not success:
                    return False, f"安装失败，无法禁用全局模式: {msg}"
                details.append("✓ 已自动禁用全局模式")

            # 5. 写入受管理 prompt 文件
            prompt_content, prompt_backup = self._write_managed_prompt(
                prompt_path, prompt_content, already_validated=True
            )
            if prompt_backup:
                backup_paths.append(prompt_backup)

            # 6. 写入新版 profile 配置，并清理旧版 profile 配置
            profile_added = self._update_config(prompt_content, prompt_file, injection_mode)

            # 构建详细消息
            details.append(f"✓ 已创建安全测试提示词: {prompt_path}")
            for backup_path in backup_paths:
                details.append(f"✓ 已备份原配置到: {backup_path}")
            if profile_added:
                details.append(f"✓ 已创建 ctf profile 配置: {self.profile_config_path}")
            else:
                details.append(f"✓ 已更新 ctf profile 配置: {self.profile_config_path}")
            if injection_mode == self.INJECTION_MODE_APPEND:
                details.append("✓ 注入方式: 追加规则（developer_instructions）")
            else:
                details.append("✓ 注入方式: 替换内置提示词（model_instructions_file）")
            details.append("使用 'codex -p ctf' 启动安全测试会话")

            return True, "\n".join(details)

        except Exception as e:
            return False, f"安装失败: {str(e)}"

    def uninstall(self) -> tuple[bool, str]:
        """
        卸载 Profile 模式

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        try:
            details = []

            # 在删除任何文件前确认 Profile 所有权。
            self._validate_profile_uninstallable()

            # 1. 删除本工具当前管理的 prompt 文件（仅当 Global 模式未启用时）
            status = check_ctf_status()
            if not status.global_installed:
                prompt_message = self._remove_current_managed_prompt()
                if prompt_message:
                    details.append(prompt_message)

            # 2. 移除新版 profile 文件和旧版 profile 配置
            removed = self._remove_ctf_profile()
            if removed:
                details.append(f"✓ 已移除 ctf profile 配置: {self.profile_config_path}")

            if not details:
                return True, "Profile 模式未安装"

            details.append("Profile 模式已禁用")
            return True, "\n".join(details)

        except Exception as e:
            return False, f"卸载失败: {str(e)}"

    def _backup_config(self, path: Optional[str] = None) -> Optional[str]:
        """备份现有配置文件"""
        target_path = path or self.config_path
        if not os.path.exists(target_path):
            return None
        return create_unique_backup(target_path)

    def _update_config(
        self,
        prompt_content: str = None,
        prompt_file: str = None,
        injection_mode: str = INJECTION_MODE_APPEND,
    ) -> bool:
        """更新新版 CTF profile 配置，并清理旧版 profile 配置

        Args:
            prompt_content: 提示词内容
            prompt_file: prompt 文件名
            injection_mode: append 或 replace

        Returns:
            bool: 是否添加了新的 profile 文件（False 表示更新已有文件）
        """
        injection_mode = self._normalize_injection_mode(injection_mode)
        instructions = prompt_content or self._get_prompt_content()
        filename = prompt_file or self._get_prompt_file()
        profile_added = not os.path.exists(self.profile_config_path)

        os.makedirs(self.codex_dir, exist_ok=True)
        profile_content = self._read_profile_config_source(instructions)
        if injection_mode == self.INJECTION_MODE_APPEND:
            profile_content = self._upsert_developer_instructions(profile_content, instructions)
        else:
            profile_content = self._upsert_model_instructions_file(profile_content, filename)
        atomic_write_text(self.profile_config_path, profile_content)

        self._remove_legacy_ctf_entries()

        return profile_added

    def _read_profile_config_source(self, prompt_content: str) -> str:
        """读取新版 profile 文件；若不存在，则从旧 [profiles.ctf] 迁移内容。"""
        if os.path.exists(self.profile_config_path):
            content = self._read_regular_text(self.profile_config_path)
            return self._ensure_profile_marker(content)

        legacy_lines = self._read_legacy_ctf_profile_body()
        if legacy_lines is not None:
            legacy_content = ''.join(legacy_lines).lstrip('\n')
            return '# Codex CTF profile managed by codex-session-patcher\n' + legacy_content

        return CTF_CONFIG_TEMPLATE.format(prompt=self._escape_multiline_basic_string(prompt_content))

    def _read_legacy_ctf_profile_body(self) -> Optional[list[str]]:
        """读取旧 profile 及其子表内容，迁移为新版 profile 文件结构。"""
        if not os.path.exists(self.config_path):
            return None

        lines = self._read_regular_text(self.config_path).splitlines(keepends=True)
        if not self._legacy_profile_is_managed(lines):
            return None

        body = []
        found = False
        index = 0

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            match = re.match(r'^\[profiles\.ctf(?:\.([^\]]+))?\]$', stripped)
            if not match:
                index += 1
                continue

            found = True
            suffix = match.group(1)
            if suffix:
                if body and body[-1].strip():
                    body.append('\n')
                body.append(f'[{suffix}]\n')

            index += 1
            while index < len(lines) and not lines[index].strip().startswith('['):
                if suffix == 'features' and re.match(r'^\s*js_repl\s*=', lines[index]):
                    index += 1
                    continue
                body.append(lines[index])
                index += 1

        return body if found else None

    def _upsert_developer_instructions(self, content: str, prompt_content: str) -> str:
        """只更新 profile 顶层 developer_instructions，保留其他设置。"""
        content = self._ensure_profile_marker(content)
        escaped_prompt = self._escape_multiline_basic_string(prompt_content)
        target_lines = ['developer_instructions = """\n', escaped_prompt, '\n"""\n']
        lines = content.splitlines(keepends=True)
        if not lines:
            return CTF_CONFIG_TEMPLATE.format(prompt=escaped_prompt)

        section_start = len(lines)
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('[') and not stripped.startswith('#'):
                section_start = index
                break

        top_level = lines[:section_start]
        rest = lines[section_start:]
        managed = any(line.strip() == PROFILE_MARKER for line in top_level)
        if not managed:
            key = self._find_unmanaged_top_level_instruction_key(top_level)
            if key:
                raise ValueError(
                    f"ctf.config.toml 顶层 {key} 没有本工具管理标记，已保留文件"
                )
            return False

        new_top_level = []
        replaced = False
        index = 0

        while index < len(top_level):
            line = top_level[index]

            if re.match(r'^\s*model_instructions_file\s*=', line):
                index += 1
                continue

            if re.match(r'^\s*developer_instructions\s*=', line):
                if not replaced:
                    new_top_level.extend(target_lines)
                    replaced = True
                index += 1
                if '"""' in line:
                    quote_count = line.count('"""')
                    while quote_count < 2 and index < len(top_level):
                        quote_count += top_level[index].count('"""')
                        index += 1
                continue

            new_top_level.append(line)
            index += 1

        if rest and new_top_level and new_top_level[-1].strip():
            new_top_level.append('\n')

        if not replaced:
            if new_top_level and not new_top_level[-1].endswith('\n'):
                new_top_level[-1] += '\n'
            new_top_level.extend(target_lines)

        profile_content = ''.join(new_top_level + rest)
        if not profile_content.endswith('\n'):
            profile_content += '\n'

        return profile_content

    def _upsert_model_instructions_file(self, content: str, prompt_file: str) -> str:
        """只更新 profile 顶层 model_instructions_file，保留其他设置。"""
        content = self._ensure_profile_marker(content)
        target_line = f'model_instructions_file = "~/.codex/prompts/{prompt_file}"\n'
        lines = content.splitlines(keepends=True)
        if not lines:
            return '# Codex CTF profile managed by codex-session-patcher\n' + target_line

        section_start = len(lines)
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('[') and not stripped.startswith('#'):
                section_start = index
                break

        top_level = lines[:section_start]
        rest = lines[section_start:]
        new_top_level = []
        inserted = False
        index = 0

        while index < len(top_level):
            line = top_level[index]

            if re.match(r'^\s*model_instructions_file\s*=', line):
                if not inserted:
                    new_top_level.append(target_line)
                    inserted = True
                index += 1
                continue

            if re.match(r'^\s*developer_instructions\s*=', line):
                index += 1
                if '"""' in line:
                    quote_count = line.count('"""')
                    while quote_count < 2 and index < len(top_level):
                        quote_count += top_level[index].count('"""')
                        index += 1
                continue

            new_top_level.append(line)
            index += 1

        if not inserted:
            if new_top_level and not new_top_level[-1].endswith('\n'):
                new_top_level[-1] += '\n'
            new_top_level.append(target_line)

        if rest and new_top_level and new_top_level[-1].strip():
            new_top_level.append('\n')

        profile_content = ''.join(new_top_level + rest)
        if not profile_content.endswith('\n'):
            profile_content += '\n'

        return profile_content

    def _escape_multiline_basic_string(self, value: str) -> str:
        """转义 TOML 多行 basic string 不能直接承载的字符。"""
        return value.replace('\\', '\\\\').replace('"""', '\\"\\"\\"')

    def _remove_ctf_profile(self) -> bool:
        """移除新版 Profile 顶层提示词配置和旧版 profile 配置。

        Returns:
            bool: 是否移除了 profile
        """
        removed = self._remove_profile_instruction_keys()

        return self._remove_legacy_ctf_entries() or removed

    def _remove_profile_instruction_keys(self) -> bool:
        """只移除 Profile 顶层提示词键，保留用户的其他设置。"""
        if not os.path.exists(self.profile_config_path):
            return False

        require_regular_or_missing(self.profile_config_path)
        with open(self.profile_config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        section_start = len(lines)
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('[') and not stripped.startswith('#'):
                section_start = index
                break

        top_level = lines[:section_start]
        rest = lines[section_start:]
        new_top_level = []
        removed = False
        index = 0
        while index < len(top_level):
            line = top_level[index]
            stripped = line.strip()
            if stripped == PROFILE_MARKER:
                removed = True
                index += 1
                continue
            if re.match(r'^\s*model_instructions_file\s*=', line):
                removed = True
                index += 1
                continue
            if re.match(r'^\s*developer_instructions\s*=', line):
                removed = True
                quote_count = line.count('"""')
                index += 1
                while quote_count < 2 and index < len(top_level):
                    quote_count += top_level[index].count('"""')
                    index += 1
                continue
            new_top_level.append(line)
            index += 1

        if not removed:
            return False

        remaining = ''.join(new_top_level + rest).strip()
        if remaining:
            atomic_write_text(self.profile_config_path, remaining + '\n')
        else:
            os.remove(self.profile_config_path)
        return True

    def _remove_current_managed_prompt(self) -> Optional[str]:
        """删除当前配置记录且内容仍匹配的提示词，保留未知文件。"""
        filename = self._get_prompt_file()
        prompt_path = os.path.abspath(os.path.join(self.prompts_dir, filename))
        prompts_dir = os.path.abspath(self.prompts_dir)
        if os.path.dirname(prompt_path) != prompts_dir or not os.path.exists(prompt_path):
            return None

        actual_content = self._read_regular_text(prompt_path)
        marked = has_ctf_marker(actual_content)
        legacy_managed = self._managed_config_owns_legacy_prompt(prompt_path, actual_content)
        if not marked and not legacy_managed:
            return f"⚠ 提示词文件没有本工具管理标记，已保留: {prompt_path}"
        if marked:
            os.remove(prompt_path)
            return f"✓ 已删除提示词文件: {prompt_path}"

        actual = actual_content.strip()

        expected = {ensure_ctf_marker(self._get_prompt_content()).strip()}
        for template in BUILTIN_TEMPLATES.get('codex', []):
            prompt = template.get('prompt')
            if isinstance(prompt, str):
                expected.add(ensure_ctf_marker(prompt).strip())

        if os.path.exists(self.profile_config_path):
            require_regular_or_missing(self.profile_config_path)
            with open(self.profile_config_path, 'r', encoding='utf-8') as f:
                profile_content = f.read()
            match = re.search(
                r'(?ms)^\s*developer_instructions\s*=\s*"""\s*\n?(.*?)\n?\s*"""',
                profile_content,
            )
            if match:
                embedded = match.group(1).replace('\\"\\"\\"', '"""').replace('\\\\', '\\')
                expected.add(embedded.strip())

        if actual not in expected:
            return f"⚠ 提示词文件已被外部修改，已保留: {prompt_path}"

        os.remove(prompt_path)
        return f"✓ 已删除提示词文件: {prompt_path}"

    def _remove_legacy_ctf_entries(self) -> bool:
        """从 base config.toml 移除 Codex 0.134.0+ 不再接受的旧 profile 写法。"""
        if not os.path.exists(self.config_path):
            return False

        lines = self._read_regular_text(self.config_path).splitlines(keepends=True)
        if not self._legacy_profile_is_managed(lines):
            return False

        new_lines = []
        removed = False
        current_section = None
        index = 0

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()

            if re.match(r'^\[profiles\.ctf(?:\.[^\]]+)?\]$', stripped):
                removed = True
                while new_lines and not new_lines[-1].strip():
                    new_lines.pop()
                if new_lines and 'codex-session-patcher' in new_lines[-1]:
                    new_lines.pop()
                index += 1
                while index < len(lines) and not lines[index].strip().startswith('['):
                    index += 1
                continue

            if stripped.startswith('['):
                current_section = stripped

            if current_section is None and re.match(r'^profile\s*=\s*["\']ctf["\'](?:\s*#.*)?$', stripped):
                removed = True
                index += 1
                continue

            new_lines.append(line)
            index += 1

        if not removed:
            return False

        while new_lines and not new_lines[0].strip():
            new_lines.pop(0)

        atomic_write_text(self.config_path, ''.join(new_lines))

        return removed

    def _legacy_profile_is_managed(self, lines: list[str]) -> bool:
        """旧版 profile 只有带历史工具标记时才允许迁移或删除。"""
        for index, line in enumerate(lines):
            if line.strip() != '[profiles.ctf]':
                continue
            marker_index = index - 1
            while marker_index >= 0 and not lines[marker_index].strip():
                marker_index -= 1
            return marker_index >= 0 and 'codex-session-patcher' in lines[marker_index]
        return False

    def install_global(self, injection_mode: str = INJECTION_MODE_APPEND) -> tuple[bool, str]:
        """
        全局模式安装：在 config.toml 顶层注入 Codex CTF 提示词配置
        自动禁用 Profile 模式

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        try:
            injection_mode = self._normalize_injection_mode(injection_mode)
            details = []

            # 1. 先检查用户手写的顶层提示词配置，避免生成重复 key。
            existing_content = ""
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    existing_content = f.read()
            existing_lines = existing_content.split('\n') if existing_content else []
            unmanaged_key = self._find_unmanaged_top_level_instruction_key(existing_lines)
            if unmanaged_key:
                return False, (
                    f"全局模式安装失败: config.toml 顶层已有 {unmanaged_key}。"
                    "为避免覆盖你的配置或生成重复 key，请先手动迁移或删除该配置。"
                )

            # 2. 确定目标并在任何修改前完成所有权预检
            prompt_file = self._get_prompt_file()
            prompt_path = os.path.join(self.prompts_dir, prompt_file)
            prompt_content = ensure_ctf_marker(self._get_prompt_content())
            self._validate_profile_uninstallable()
            self._validate_legacy_profile_installable()
            self._validate_prompt_target(prompt_path)

            # 3. 在任何配置变化前备份 base/profile 配置
            for path in (self.config_path, self.profile_config_path):
                if os.path.exists(path):
                    backup_path = self._backup_config(path)
                    if backup_path:
                        details.append(f"✓ 已备份原配置到: {backup_path}")

            # 4. 先禁用 Profile 模式，包括旧版 [profiles.ctf] 写法
            removed = self._remove_ctf_profile()
            if removed:
                details.append("✓ 已自动禁用 Profile 模式")

            # 5. 确保 prompts 目录存在，写入 prompt 文件
            os.makedirs(self.prompts_dir, exist_ok=True)
            prompt_content, prompt_backup = self._write_managed_prompt(
                prompt_path, prompt_content, already_validated=True
            )
            if prompt_backup:
                details.append(f"✓ 已备份原提示词到: {prompt_backup}")
            details.append(f"✓ 已写入安全测试提示词: {prompt_path}")

            # 6. 读取现有配置
            existing_content = ""
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    existing_content = f.read()

            lines = existing_content.split('\n') if existing_content else []

            # 7. 更新或插入受管理的提示词配置
            lines = self._remove_global_managed_block(lines)
            lines = self._insert_global_managed_block(lines, prompt_content, prompt_file, injection_mode)
            details.append("✓ 已注入全局配置")
            if injection_mode == self.INJECTION_MODE_APPEND:
                details.append("✓ 注入方式: 追加规则（developer_instructions）")
            else:
                details.append("✓ 注入方式: 替换内置提示词（model_instructions_file）")

            # 8. 写入配置
            new_content = '\n'.join(lines)
            new_content = new_content.strip() + '\n'

            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            atomic_write_text(self.config_path, new_content)

            details.append(f"✓ 配置文件: {self.config_path}")
            details.append("⚠ 所有新 Codex 会话将自动启用安全测试上下文")
            details.append("使用完毕请及时禁用全局模式")

            return True, "\n".join(details)

        except Exception as e:
            return False, f"全局模式安装失败: {str(e)}"

    def _find_unmanaged_top_level_instruction_key(self, lines: list[str]) -> Optional[str]:
        """查找非本工具管理的顶层提示词配置。"""
        unmanaged_lines = self._remove_global_managed_block(lines)
        for line in unmanaged_lines:
            stripped = line.strip()
            if stripped.startswith('[') and not stripped.startswith('#'):
                return None
            if not stripped or stripped.startswith('#'):
                continue
            if re.match(r'^developer_instructions\s*=', stripped):
                return "developer_instructions"
            if re.match(r'^model_instructions_file\s*=', stripped):
                return "model_instructions_file"
        return None

    def uninstall_global(self) -> tuple[bool, str]:
        """
        全局模式卸载：从 config.toml 移除标记行和 developer_instructions 块

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        try:
            if not os.path.exists(self.config_path):
                return True, "全局模式未安装"

            with open(self.config_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            new_lines, found = self._remove_global_managed_block([line.rstrip('\n') for line in lines], return_found=True)

            if not found:
                return True, "全局模式未安装"

            # 清理首部多余空行
            while new_lines and not new_lines[0].strip():
                new_lines.pop(0)

            atomic_write_text(self.config_path, '\n'.join(new_lines).strip() + '\n')

            details = []
            details.append(f"✓ 已从配置移除全局注入: {self.config_path}")
            details.append("新 Codex 会话将不再自动启用安全测试上下文")

            return True, "\n".join(details)

        except Exception as e:
            return False, f"全局模式卸载失败: {str(e)}"

    def _update_global_config(
        self,
        prompt_content: str,
        prompt_file: str = None,
        injection_mode: str = INJECTION_MODE_APPEND,
    ) -> bool:
        """更新已启用全局模式中的提示词配置。"""
        injection_mode = self._normalize_injection_mode(injection_mode)
        if not os.path.exists(self.config_path):
            return False

        with open(self.config_path, 'r', encoding='utf-8') as f:
            existing_content = f.read()

        lines = existing_content.split('\n') if existing_content else []
        lines, found = self._remove_global_managed_block(lines, return_found=True)
        if not found:
            return False

        lines = self._insert_global_managed_block(
            lines,
            prompt_content,
            prompt_file or self._get_prompt_file(),
            injection_mode,
        )
        new_content = '\n'.join(lines).strip() + '\n'
        atomic_write_text(self.config_path, new_content)

        return True

    def _insert_global_managed_block(
        self,
        lines: list[str],
        prompt_content: str,
        prompt_file: str,
        injection_mode: str,
    ) -> list[str]:
        """在第一个 section 前插入全局模式受管理配置块。"""
        if injection_mode == self.INJECTION_MODE_APPEND:
            target_lines = [
                f'{GLOBAL_MARKER} 安全测试模式（由 codex-session-patcher 管理）',
                'developer_instructions = """',
                self._escape_multiline_basic_string(prompt_content),
                '"""',
            ]
        else:
            target_lines = [
                f'{GLOBAL_MARKER} 安全测试模式（由 codex-session-patcher 管理）',
                f'model_instructions_file = "~/.codex/prompts/{prompt_file}"',
            ]

        insert_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('[') and not stripped.startswith('#'):
                insert_idx = i
                break
            insert_idx = i + 1

        while insert_idx > 0 and not lines[insert_idx - 1].strip():
            lines.pop(insert_idx - 1)
            insert_idx -= 1

        block = target_lines if insert_idx == 0 else [''] + target_lines
        for line in reversed(block):
            lines.insert(insert_idx, line)

        return lines

    def _remove_global_managed_block(self, lines: list[str], return_found: bool = False):
        """移除全局模式中由本工具标记管理的配置块。"""
        new_lines = []
        found = False
        index = 0

        while index < len(lines):
            line = lines[index]

            if GLOBAL_MARKER not in line:
                new_lines.append(line)
                index += 1
                continue

            found = True
            index += 1

            if index >= len(lines):
                continue

            next_line = lines[index].strip()

            if next_line.startswith('model_instructions_file'):
                index += 1
                continue

            if next_line.startswith('developer_instructions'):
                quote_count = lines[index].count('"""')
                index += 1
                while quote_count < 2 and index < len(lines):
                    quote_count += lines[index].count('"""')
                    index += 1
                continue

        if return_found:
            return new_lines, found
        return new_lines

    def get_status(self) -> CTFStatus:
        """获取当前配置状态"""
        return check_ctf_status()


class ClaudeCodeCTFInstaller:
    """Claude Code CTF 配置安装器"""

    def __init__(self):
        self.workspace_dir = DEFAULT_CLAUDE_CTF_WORKSPACE
        self.claude_dir = os.path.join(self.workspace_dir, ".claude")
        self.prompt_path = os.path.join(self.claude_dir, "CLAUDE.md")
        self.readme_path = os.path.join(self.workspace_dir, "README.md")
        self.settings_local = os.path.expanduser("~/.claude/settings.local.json")

    def install(self, custom_prompt: str = None, inject_permissions: bool = False) -> tuple[bool, str]:
        """
        安装 Claude Code CTF 配置

        Args:
            custom_prompt: 自定义提示词内容，为 None 时使用默认模板
            inject_permissions: 是否向 settings.local.json 注入宽松权限

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        try:
            details = []

            # 1. 创建工作空间目录
            os.makedirs(self.claude_dir, exist_ok=True)
            details.append(f"✓ 已创建工作空间: {self.workspace_dir}")

            # 2. 写入 .claude/CLAUDE.md
            prompt_content, prompt_backup = _write_workspace_prompt(
                self.prompt_path,
                custom_prompt or CLAUDE_CODE_SECURITY_MODE_PROMPT,
            )
            details.append(f"✓ 已创建 CLAUDE.md: {self.prompt_path}")
            if prompt_backup:
                details.append(f"✓ 已备份原 CLAUDE.md 到: {prompt_backup}")

            # 3. 写入 README
            atomic_write_text(self.readme_path, CLAUDE_CODE_CTF_README)
            details.append(f"✓ 已创建 README: {self.readme_path}")

            # 4. 可选：注入权限
            if inject_permissions:
                self._inject_permissions()
                details.append("✓ 已注入宽松权限到 settings.local.json")

            details.append("")
            details.append("使用方法: cd ~/.claude-ctf-workspace && claude")

            return True, "\n".join(details)

        except Exception as e:
            return False, f"安装失败: {str(e)}"

    def uninstall(self) -> tuple[bool, str]:
        """
        卸载 Claude Code CTF 配置

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        try:
            details = []

            # 1. 删除 .claude/CLAUDE.md（验证标记）
            if os.path.exists(self.prompt_path):
                try:
                    require_regular_or_missing(self.prompt_path)
                    with open(self.prompt_path, 'r', encoding='utf-8') as f:
                        content = f.read(500)
                    if CTF_MARKER in content:
                        os.remove(self.prompt_path)
                        details.append(f"✓ 已删除 CLAUDE.md: {self.prompt_path}")
                    else:
                        return False, "CLAUDE.md 不是由本工具创建的，跳过删除"
                except Exception as exc:
                    return False, f"无法验证 CLAUDE.md，已保留文件: {exc}"

            # 2. 只删除内容仍与本工具模板一致的 README
            if os.path.exists(self.readme_path):
                require_regular_or_missing(self.readme_path)
                with open(self.readme_path, 'r', encoding='utf-8') as f:
                    readme_content = f.read()
                if readme_content == CLAUDE_CODE_CTF_README:
                    os.remove(self.readme_path)
                    details.append(f"✓ 已删除 README: {self.readme_path}")
                else:
                    details.append(f"⚠ README 已被修改，已保留: {self.readme_path}")

            # 3. 尝试清理空目录（不删除用户自建的文件）
            try:
                if os.path.isdir(self.claude_dir) and not os.listdir(self.claude_dir):
                    os.rmdir(self.claude_dir)
                    details.append(f"✓ 已删除空目录: {self.claude_dir}")
                if os.path.isdir(self.workspace_dir) and not os.listdir(self.workspace_dir):
                    os.rmdir(self.workspace_dir)
                    details.append(f"✓ 已删除工作空间: {self.workspace_dir}")
            except OSError:
                pass  # 目录非空，保留

            # 4. 移除注入的权限
            self._remove_permissions()

            if not details:
                return True, "Claude Code CTF 配置未安装"

            return True, "\n".join(details)

        except Exception as e:
            return False, f"卸载失败: {str(e)}"

    def _inject_permissions(self):
        """向 settings.local.json 注入宽松的 Bash 权限"""
        import json

        data = {"permissions": {"allow": [], "deny": [], "ask": []}}
        if os.path.exists(self.settings_local):
            try:
                with open(self.settings_local, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as exc:
                raise ValueError(f"无法读取 Claude Code 权限配置: {exc}") from exc

        permissions = data.setdefault("permissions", {})
        allow = permissions.setdefault("allow", [])

        # 检查是否已注入
        marker = "__csp_ctf_marker__"
        if marker in allow:
            return

        # 备份
        self._backup_settings()

        # 注入权限
        allow.append(marker)
        allow.append("Bash(*)")

        atomic_write_json(self.settings_local, data)

    def _remove_permissions(self):
        """从 settings.local.json 移除注入的权限"""
        import json

        if not os.path.exists(self.settings_local):
            return

        try:
            with open(self.settings_local, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return

        permissions = data.get("permissions", {})
        allow = permissions.get("allow", [])

        marker = "__csp_ctf_marker__"
        if marker not in allow:
            return

        # 移除标记和注入的权限
        allow.remove(marker)
        if "Bash(*)" in allow:
            allow.remove("Bash(*)")

        atomic_write_json(self.settings_local, data)

    def _backup_settings(self) -> Optional[str]:
        """备份 settings.local.json"""
        if not os.path.exists(self.settings_local):
            return None
        return create_unique_backup(self.settings_local)

    def get_status(self) -> CTFStatus:
        """获取当前配置状态"""
        return check_ctf_status()


class OpenCodeCTFInstaller:
    """OpenCode CTF 配置安装器"""

    def __init__(self):
        self.workspace_dir = DEFAULT_OPENCODE_CTF_WORKSPACE
        self.agents_md_path = os.path.join(self.workspace_dir, "AGENTS.md")
        self.config_path = os.path.join(self.workspace_dir, "opencode.json")
        self.readme_path = os.path.join(self.workspace_dir, "README.md")

    def install(self, custom_prompt: str = None) -> tuple[bool, str]:
        """
        安装 OpenCode CTF 配置

        Args:
            custom_prompt: 自定义提示词内容，为 None 时使用默认模板

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        try:
            details = []

            # 1. 创建工作空间目录
            os.makedirs(self.workspace_dir, exist_ok=True)
            details.append(f"✓ 已创建工作空间: {self.workspace_dir}")

            # 2. 写入 AGENTS.md
            prompt_content, prompt_backup = _write_workspace_prompt(
                self.agents_md_path,
                custom_prompt or OPENCODE_SECURITY_MODE_PROMPT,
            )
            details.append(f"✓ 已创建 AGENTS.md: {self.agents_md_path}")
            if prompt_backup:
                details.append(f"✓ 已备份原 AGENTS.md 到: {prompt_backup}")

            # 3. 写入 opencode.json
            atomic_write_text(self.config_path, OPENCODE_CTF_CONFIG)
            details.append(f"✓ 已创建 opencode.json: {self.config_path}")

            # 4. 写入 README
            atomic_write_text(self.readme_path, OPENCODE_CTF_README)
            details.append(f"✓ 已创建 README: {self.readme_path}")

            details.append("")
            details.append("使用方法: cd ~/.opencode-ctf-workspace && opencode")

            return True, "\n".join(details)

        except Exception as e:
            return False, f"安装失败: {str(e)}"

    def uninstall(self) -> tuple[bool, str]:
        """
        卸载 OpenCode CTF 配置

        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        try:
            details = []

            # 1. 删除 AGENTS.md（验证标记）
            if os.path.exists(self.agents_md_path):
                try:
                    require_regular_or_missing(self.agents_md_path)
                    with open(self.agents_md_path, 'r', encoding='utf-8') as f:
                        content = f.read(500)
                    if CTF_MARKER in content:
                        os.remove(self.agents_md_path)
                        details.append(f"✓ 已删除 AGENTS.md: {self.agents_md_path}")
                    else:
                        return False, "AGENTS.md 不是由本工具创建的，跳过删除"
                except Exception as exc:
                    return False, f"无法验证 AGENTS.md，已保留文件: {exc}"

            # 2. 只删除内容仍与本工具模板一致的 opencode.json
            if os.path.exists(self.config_path):
                require_regular_or_missing(self.config_path)
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config_content = f.read()
                if config_content == OPENCODE_CTF_CONFIG:
                    os.remove(self.config_path)
                    details.append(f"✓ 已删除 opencode.json: {self.config_path}")
                else:
                    details.append(f"⚠ opencode.json 已被修改，已保留: {self.config_path}")

            # 3. 只删除内容仍与本工具模板一致的 README
            if os.path.exists(self.readme_path):
                require_regular_or_missing(self.readme_path)
                with open(self.readme_path, 'r', encoding='utf-8') as f:
                    readme_content = f.read()
                if readme_content == OPENCODE_CTF_README:
                    os.remove(self.readme_path)
                    details.append(f"✓ 已删除 README: {self.readme_path}")
                else:
                    details.append(f"⚠ README 已被修改，已保留: {self.readme_path}")

            # 4. 尝试清理空目录
            try:
                if os.path.isdir(self.workspace_dir) and not os.listdir(self.workspace_dir):
                    os.rmdir(self.workspace_dir)
                    details.append(f"✓ 已删除工作空间: {self.workspace_dir}")
            except OSError:
                pass

            if not details:
                return True, "OpenCode CTF 配置未安装"

            return True, "\n".join(details)

        except Exception as e:
            return False, f"卸载失败: {str(e)}"

    def get_status(self) -> CTFStatus:
        """获取当前配置状态"""
        return check_ctf_status()


class GrokCTFInstaller:
    """Grok CTF 配置安装器 — 写入 ~/.grok/agents/ctf.md 代理定义。

    激活方式（真实 Grok CLI）：
      grok --agent ctf
      GROK_AGENT=ctf grok
    """

    AGENT_NAME = "ctf"
    AGENT_FILENAME = "ctf.md"

    def __init__(self, home: str | None = None):
        # home 仅用于测试注入临时 HOME；生产默认 expanduser("~")
        self.home = os.path.expanduser(home or "~")
        self.agent_dir = os.path.join(self.home, ".grok", "agents")
        self.agent_path = os.path.join(self.agent_dir, self.AGENT_FILENAME)

    def _default_prompt(self) -> str:
        """复用已有优化 CTF 提示词作为 Grok agent body。"""
        from .templates import SECURITY_MODE_PROMPT_OPTIMIZED, OPENCODE_SECURITY_MODE_PROMPT
        base = SECURITY_MODE_PROMPT_OPTIMIZED or OPENCODE_SECURITY_MODE_PROMPT or (
            "You are operating in authorized security-testing / CTF mode.\n"
            "Only work within the authorized local/test scope.\n"
        )
        frontmatter = (
            "---\n"
            f"name: {self.AGENT_NAME}\n"
            "description: CTF / authorized security-testing agent managed by codex-session-patcher\n"
            "---\n\n"
        )
        body = base.lstrip()
        if body.startswith("<!--"):
            end = body.find("-->")
            if end >= 0:
                body = body[end + 3:].lstrip("\n")
        return frontmatter + body

    def install(self, custom_prompt: str = None) -> tuple[bool, str]:
        """安装带所有权标记的 Grok agent 定义。"""
        try:
            details = []
            os.makedirs(self.agent_dir, exist_ok=True)
            prompt = custom_prompt or self._default_prompt()
            managed, backup = _write_workspace_prompt(self.agent_path, prompt)
            details.append(f"✓ 已创建 Grok agent: {self.agent_path}")
            if backup:
                details.append(f"✓ 已备份原文件到: {backup}")
            details.append("")
            details.append("激活命令: grok --agent ctf")
            details.append("或: GROK_AGENT=ctf grok")
            return True, "\n".join(details)
        except Exception as e:
            return False, f"安装失败: {str(e)}"

    def uninstall(self) -> tuple[bool, str]:
        """仅删除带本工具标记的 agent 文件。"""
        try:
            if not os.path.exists(self.agent_path):
                return True, "Grok CTF 配置未安装"
            require_regular_or_missing(self.agent_path)
            with open(self.agent_path, "r", encoding="utf-8") as f:
                content = f.read(500)
            if CTF_MARKER not in content:
                return False, f"代理文件没有本工具管理标记，已保留: {self.agent_path}"
            os.remove(self.agent_path)
            return True, f"✓ 已删除代理文件: {self.agent_path}"
        except Exception as e:
            return False, f"卸载失败: {str(e)}"

    def get_status(self) -> CTFStatus:
        return check_ctf_status()

