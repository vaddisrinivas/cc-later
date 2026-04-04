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
        # Minimal TOML parser for cc-later config files.
        # Handles: strings, ints, bools, arrays (including multiline),
        # tables, inline tables. No multiline strings, no datetime.
        import re
        from typing import Any

        def _parse_toml(text: str) -> dict:
            result: dict = {}
            current_table: dict = result
            lines = text.splitlines()
            i = 0

            while i < len(lines):
                stripped = lines[i].strip()

                # Skip blank lines and comments
                if not stripped or stripped.startswith("#"):
                    i += 1
                    continue

                # Table header [section]
                table_match = re.match(r"^\[([^\]]+)\]$", stripped)
                if table_match:
                    key = table_match.group(1).strip()
                    parts = key.split(".")
                    current_table = result
                    for part in parts:
                        if part not in current_table:
                            current_table[part] = {}
                        current_table = current_table[part]
                    i += 1
                    continue

                # Key = value
                eq_pos = stripped.find("=")
                if eq_pos == -1:
                    i += 1
                    continue

                key = stripped[:eq_pos].strip()
                value_str = stripped[eq_pos + 1:].strip()

                # Strip inline comments (not inside strings or arrays)
                if value_str and value_str[0] not in ('"', "'", "[", "{"):
                    comment_pos = value_str.find("#")
                    if comment_pos > 0:
                        value_str = value_str[:comment_pos].strip()

                # Handle multiline arrays: accumulate lines until brackets balance
                if value_str.startswith("[") and value_str.count("[") > value_str.count("]"):
                    while i + 1 < len(lines) and value_str.count("[") > value_str.count("]"):
                        i += 1
                        continuation = lines[i].strip()
                        if continuation.startswith("#"):
                            continue
                        value_str += " " + continuation
                    # Strip comments from the assembled line
                    # (only outside strings)
                    value_str = _strip_trailing_comments(value_str)

                current_table[key] = _parse_value(value_str)
                i += 1

            return result

        def _strip_trailing_comments(s: str) -> str:
            """Remove # comments that aren't inside strings."""
            in_string = False
            string_char = ""
            for idx, ch in enumerate(s):
                if in_string:
                    if ch == string_char:
                        in_string = False
                    continue
                if ch in ('"', "'"):
                    in_string = True
                    string_char = ch
                elif ch == "#":
                    return s[:idx].strip()
            return s

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

            # Inline table
            if s.startswith("{"):
                return _parse_inline_table(s)

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
            # Remove outer brackets
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
                elif ch in ("[", "{"):
                    depth += 1
                    current += ch
                elif ch in ("]", "}"):
                    depth -= 1
                    current += ch
                elif ch == "," and depth == 0:
                    val = current.strip()
                    if val and not val.startswith("#"):
                        items.append(_parse_value(val))
                    current = ""
                else:
                    current += ch

            val = current.strip()
            if val and not val.startswith("#"):
                items.append(_parse_value(val))

            return items

        def _parse_inline_table(s: str) -> dict:
            """Parse {key = value, key2 = value2} inline tables."""
            s = s.strip()
            if s == "{}":
                return {}
            inner = s[1:-1].strip()
            if not inner:
                return {}

            result = {}
            # Split on commas (respecting nesting)
            pairs = []
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
                elif ch in ("[", "{"):
                    depth += 1
                    current += ch
                elif ch in ("]", "}"):
                    depth -= 1
                    current += ch
                elif ch == "," and depth == 0:
                    pairs.append(current.strip())
                    current = ""
                else:
                    current += ch

            if current.strip():
                pairs.append(current.strip())

            for pair in pairs:
                eq = pair.find("=")
                if eq == -1:
                    continue
                k = pair[:eq].strip()
                v = pair[eq + 1:].strip()
                result[k] = _parse_value(v)

            return result

        class _TOMLModule:
            TOMLDecodeError = ValueError

            @staticmethod
            def load(fh: Any) -> dict:
                data = fh.read()
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                return _parse_toml(data)

        tomllib = _TOMLModule()  # type: ignore[assignment]
