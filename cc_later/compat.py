"""Python version compatibility shims."""

from __future__ import annotations

import sys

# tomllib was added in 3.11; try the backport tomli first
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        # Minimal TOML subset parser for config files.
        # Handles: strings, ints, bools, arrays, tables. No inline tables, no multiline.
        import re
        from typing import Any

        class _MinimalTOML:
            """Parse a minimal subset of TOML sufficient for cc-later config."""

            @staticmethod
            def load(fh: Any) -> dict[str, Any]:
                return _MinimalTOML.loads(fh.read().decode("utf-8") if isinstance(fh.read(0), bytes) else "")

            @staticmethod
            def loads(text: str) -> dict[str, Any]:
                # Re-read from buffer
                pass

            class TOMLDecodeError(ValueError):
                pass

        def _parse_toml(text: str) -> dict:
            result: dict = {}
            current_table: dict = result
            current_path: list[str] = []

            for line_num, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                # Table header [section]
                table_match = re.match(r"^\[([^\]]+)\]$", stripped)
                if table_match:
                    key = table_match.group(1).strip()
                    current_path = key.split(".")
                    current_table = result
                    for part in current_path:
                        if part not in current_table:
                            current_table[part] = {}
                        current_table = current_table[part]
                    continue

                # Key = value
                eq_pos = stripped.find("=")
                if eq_pos == -1:
                    continue

                key = stripped[:eq_pos].strip()
                value_str = stripped[eq_pos + 1:].strip()

                # Strip inline comments (not inside strings)
                if value_str and value_str[0] not in ('"', "'", "["):
                    comment_pos = value_str.find("#")
                    if comment_pos > 0:
                        value_str = value_str[:comment_pos].strip()

                current_table[key] = _parse_value(value_str)

            return result

        def _parse_value(s: str) -> Any:
            s = s.strip()
            if not s:
                return ""

            # Boolean
            if s == "true":
                return True
            if s == "false":
                return False

            # String (double-quoted)
            if s.startswith('"') and s.endswith('"'):
                return s[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")

            # String (single-quoted)
            if s.startswith("'") and s.endswith("'"):
                return s[1:-1]

            # Array
            if s.startswith("["):
                return _parse_array(s)

            # Integer (with underscores)
            try:
                return int(s.replace("_", ""))
            except ValueError:
                pass

            # Float
            try:
                return float(s)
            except ValueError:
                pass

            return s

        def _parse_array(s: str) -> list:
            s = s.strip()
            if s == "[]":
                return []
            # Remove brackets
            inner = s[1:-1].strip()
            if not inner:
                return []

            items = []
            current = ""
            depth = 0
            in_string = False
            string_char = ""

            for ch in inner:
                if in_string:
                    current += ch
                    if ch == string_char:
                        in_string = False
                    continue
                if ch in ('"', "'"):
                    in_string = True
                    string_char = ch
                    current += ch
                elif ch == "[":
                    depth += 1
                    current += ch
                elif ch == "]":
                    depth -= 1
                    current += ch
                elif ch == "," and depth == 0:
                    items.append(_parse_value(current.strip()))
                    current = ""
                else:
                    current += ch

            if current.strip():
                items.append(_parse_value(current.strip()))

            return items

        class _TOMLModule:
            TOMLDecodeError = ValueError

            @staticmethod
            def load(fh: Any) -> dict:
                data = fh.read()
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                return _parse_toml(data)

        tomllib = _TOMLModule()  # type: ignore[assignment]
