"""A strict, dependency-free parser and writer for the YAML subset used by the registry.

Supported: block mappings, block sequences (including ``- key: value`` items),
plain / single-quoted / double-quoted scalars, flow sequences of scalars
(``[a, 'b', "c"]``), ``{}``, folded and literal block scalars (``>``, ``>-``,
``|``, ``|-``), comments, and ``true``/``false``/``null``/integers.

Not supported (rejected with a line-numbered error): anchors, aliases, tags,
flow mappings, nested flow collections, multi-document streams, tabs.

Every file this module accepts is also valid YAML 1.2, so contributors can use
any editor or linter they like. Dates stay strings (``2026-12-11``).
"""

from __future__ import annotations

import json
import re

__all__ = ["YamlError", "loads", "dumps"]


class YamlError(ValueError):
    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-/]*)\s*:(?:\s+(.*))?$")
_INT_RE = re.compile(r"^-?\d+$")
_VALUE_OPENERS = set(":-[,")


def _find_closing(s: str, start: int) -> int | None:
    """Index of the quote that closes the quoted scalar opening at ``start``."""
    q = s[start]
    k = start + 1
    while k < len(s):
        c = s[k]
        if q == "'":
            if c == "'":
                if k + 1 < len(s) and s[k + 1] == "'":
                    k += 2
                    continue
                return k
        else:
            if c == "\\":
                k += 2
                continue
            if c == '"':
                return k
        k += 1
    return None


def _strip_comment(line: str) -> str:
    """Remove a trailing ``# comment`` that is outside quoted scalars."""
    k = 0
    prev_sig = ""  # previous non-space character
    while k < len(line):
        c = line[k]
        if c in "'\"" and (prev_sig == "" or prev_sig in _VALUE_OPENERS):
            end = _find_closing(line, k)
            if end is None:
                return line  # unterminated quote: leave it for the scalar parser to report
            k = end + 1
            prev_sig = c
            continue
        if c == "#" and (k == 0 or line[k - 1] in " \t"):
            return line[:k]
        if not c.isspace():
            prev_sig = c
        k += 1
    return line


def _is_seq_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _split_flow(inner: str, lineno: int) -> list[str]:
    parts, cur, k = [], [], 0
    while k < len(inner):
        c = inner[k]
        if c in "'\"" and not "".join(cur).strip():
            end = _find_closing(inner, k)
            if end is None:
                raise YamlError("unterminated quoted string in flow sequence", lineno)
            cur.append(inner[k : end + 1])
            k = end + 1
            continue
        if c in "[]{}":
            raise YamlError("nested flow collections are not supported", lineno)
        if c == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        k += 1
    parts.append("".join(cur))
    if parts and parts[-1].strip() == "":
        parts.pop()  # tolerate a trailing comma
    return parts


def _scalar(s: str, lineno: int):
    s = s.strip()
    if s == "":
        return None
    c = s[0]
    if c == "'":
        end = _find_closing(s, 0)
        if end is None or end != len(s) - 1:
            raise YamlError("bad single-quoted string", lineno)
        return s[1:end].replace("''", "'")
    if c == '"':
        end = _find_closing(s, 0)
        if end is None or end != len(s) - 1:
            raise YamlError("bad double-quoted string", lineno)
        try:
            return json.loads(s)
        except ValueError as exc:  # pragma: no cover - rare escapes
            raise YamlError(f"unsupported escape in double-quoted string ({exc})", lineno) from exc
    if c == "[":
        if not s.endswith("]"):
            raise YamlError("unterminated flow sequence", lineno)
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_scalar(p, lineno) for p in _split_flow(inner, lineno)]
    if c == "{":
        if s == "{}":
            return {}
        raise YamlError("flow mappings are not supported; use block style", lineno)
    if c in "&*!|>%@`":
        raise YamlError(f"unsupported YAML feature or unquoted indicator {c!r}; quote the value", lineno)
    if ": " in s or s.endswith(":"):
        raise YamlError("plain values containing ': ' must be quoted", lineno)
    if s in ("null", "Null", "NULL", "~"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(s):
        return int(s)
    return s


class _Parser:
    def __init__(self, text: str):
        self.raw = text.splitlines()
        self.i = 0

    def peek(self):
        i = self.i
        while i < len(self.raw):
            raw = self.raw[i]
            lead = raw[: len(raw) - len(raw.lstrip())]
            if "\t" in lead:
                raise YamlError("tabs are not allowed for indentation", i + 1)
            content = _strip_comment(raw).rstrip()
            stripped = content.strip()
            if stripped == "" or (stripped in ("---", "...") and not lead):
                i += 1
                continue
            indent = len(content) - len(content.lstrip(" "))
            return i, indent, stripped
        return None

    def parse(self):
        pk = self.peek()
        if pk is None:
            return None
        value = self.parse_value(pk[1])
        rest = self.peek()
        if rest is not None:
            raise YamlError("unexpected content (check indentation)", rest[0] + 1)
        return value

    def parse_value(self, indent: int):
        pk = self.peek()
        if pk is None:
            return None
        if _is_seq_item(pk[2]):
            return self.parse_seq(pk[1])
        if _KEY_RE.match(pk[2]) or pk[2][0] in "'\"" and self._quoted_key(pk[2]):
            return self.parse_map(pk[1])
        # a lone scalar document / value
        self.i = pk[0] + 1
        return _scalar(pk[2], pk[0] + 1)

    @staticmethod
    def _quoted_key(content: str):
        end = _find_closing(content, 0)
        if end is None:
            return None
        after = content[end + 1 :]
        if after.startswith(":") and (len(after) == 1 or after[1] == " "):
            return _scalar(content[: end + 1], 0), after[1:].strip()
        return None

    def _split_key(self, content: str, lineno: int):
        if content[0] in "'\"":
            kv = self._quoted_key(content)
            if kv is None:
                raise YamlError("expected 'key: value'", lineno)
            return kv
        m = _KEY_RE.match(content)
        if not m:
            raise YamlError(f"expected 'key: value', got {content!r}", lineno)
        return m.group(1), (m.group(2) or "").strip()

    def parse_map(self, indent: int, first: tuple[int, str] | None = None):
        result: dict = {}
        while True:
            if first is not None:
                lineno, content = first
                first = None
            else:
                pk = self.peek()
                if pk is None:
                    break
                i, ind, content = pk
                if ind < indent:
                    break
                if ind > indent:
                    raise YamlError("unexpected indentation", i + 1)
                if _is_seq_item(content):
                    break
                self.i = i + 1
                lineno = i + 1
            key, rest = self._split_key(content, lineno)
            if key in result:
                raise YamlError(f"duplicate key {key!r}", lineno)
            if rest == "":
                pk = self.peek()
                if pk is not None and (pk[1] > indent or (pk[1] == indent and _is_seq_item(pk[2]))):
                    result[key] = self.parse_value(pk[1])
                else:
                    result[key] = None
            elif rest[0] in "|>":
                result[key] = self.block_scalar(rest, indent, lineno)
            else:
                result[key] = _scalar(rest, lineno)
        return result

    def parse_seq(self, indent: int):
        items: list = []
        while True:
            pk = self.peek()
            if pk is None:
                break
            i, ind, content = pk
            if ind < indent:
                break
            if ind > indent:
                raise YamlError("unexpected indentation", i + 1)
            if not _is_seq_item(content):
                break
            self.i = i + 1
            after = content[1:]
            rest = after.lstrip(" ")
            col = ind + 1 + (len(after) - len(rest))
            if rest == "":
                nxt = self.peek()
                items.append(self.parse_value(nxt[1]) if nxt is not None and nxt[1] > indent else None)
            elif _is_seq_item(rest):
                raise YamlError("inline nested sequences ('- - x') are not supported", i + 1)
            elif _KEY_RE.match(rest) or (rest[0] in "'\"" and self._quoted_key(rest)):
                items.append(self.parse_map(col, first=(i + 1, rest)))
            else:
                items.append(_scalar(rest, i + 1))
        return items

    def block_scalar(self, header: str, parent_indent: int, lineno: int) -> str:
        m = re.fullmatch(r"([|>])([+-]?)", header)
        if not m:
            raise YamlError(f"unsupported block scalar header {header!r}", lineno)
        style, chomp = m.groups()
        lines: list[str] = []
        block_indent = None
        i = self.i
        while i < len(self.raw):
            raw = self.raw[i]
            if raw.strip() == "":
                lines.append("")
                i += 1
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if ind <= parent_indent:
                break
            if block_indent is None:
                block_indent = ind
            if ind < block_indent:
                break
            lines.append(raw[block_indent:].rstrip())
            i += 1
        self.i = i
        while lines and lines[-1] == "":
            lines.pop()
        if style == "|":
            text = "\n".join(lines)
        else:
            chunks, cur = [], []
            for ln in lines:
                if ln == "":
                    chunks.append(" ".join(cur))
                    cur = []
                else:
                    cur.append(ln)
            chunks.append(" ".join(cur))
            text = "\n".join(chunks)
        if chomp != "-" and text:
            text += "\n"
        return text


def loads(text: str):
    """Parse registry YAML text into Python objects."""
    return _Parser(text).parse()


# ---------------------------------------------------------------- writer

_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~", "y", "n"}
_PLAIN_START = re.compile(r"^[A-Za-z0-9_./(]")
_NUMBERISH = re.compile(r"^[-+]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _plain_ok(s: str, flow: bool) -> bool:
    if not s or s != s.strip() or "\n" in s:
        return False
    if s.lower() in _RESERVED:
        return False
    if _NUMBERISH.match(s) and not _DATE.match(s):
        return False
    if not _PLAIN_START.match(s):
        return False
    if ": " in s or " #" in s or s.endswith(":") or "\t" in s:
        return False
    if flow and any(ch in s for ch in ",[]{}"):
        return False
    return True


def _quote(s: str) -> str:
    if "\n" in s or "\t" in s:
        return json.dumps(s, ensure_ascii=False)
    return "'" + s.replace("'", "''") + "'"


def _fmt_scalar(v, flow: bool = False) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    return s if _plain_ok(s, flow) else _quote(s)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines


def _emit(obj, indent: int, out: list[str]) -> None:
    pad = " " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = _fmt_scalar(k)
            if isinstance(v, dict) and v:
                out.append(f"{pad}{key}:")
                _emit(v, indent + 2, out)
            elif isinstance(v, list) and v:
                flow = "[" + ", ".join(_fmt_scalar(x, flow=True) for x in v) + "]"
                if all(not isinstance(x, (dict, list)) for x in v) and len(pad) + len(key) + 2 + len(flow) <= 100:
                    out.append(f"{pad}{key}: {flow}")
                else:
                    out.append(f"{pad}{key}:")
                    _emit(v, indent + 2, out)
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}{key}: {'{}' if isinstance(v, dict) else '[]'}")
            elif (
                isinstance(v, str)
                and len(v) + len(pad) + len(key) > 96
                and "\n" not in v
                and "  " not in v
                and v == v.strip()
            ):
                out.append(f"{pad}{key}: >-")
                for line in _wrap(v, max(40, 96 - indent)):
                    out.append(f"{pad}  {line}")
            else:
                out.append(f"{pad}{key}: {_fmt_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item:
                sub: list[str] = []
                _emit(item, indent + 2, sub)
                sub[0] = f"{pad}- " + sub[0][indent + 2 :]
                out.extend(sub)
            elif isinstance(item, list):
                raise ValueError("nested lists are not supported by the registry writer")
            else:
                out.append(f"{pad}- {_fmt_scalar(item)}")
    else:
        out.append(pad + _fmt_scalar(obj))


def dumps(obj, header: str | None = None) -> str:
    """Serialize dicts/lists/scalars to registry-style YAML."""
    out: list[str] = []
    if header:
        out.extend("# " + line if line else "#" for line in header.splitlines())
    _emit(obj, 0, out)
    return "\n".join(out) + "\n"
