"""Lightweight AST/regex code indexer mapping Flagsmith feature keys to source files."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FLAG_CALL_RE = re.compile(
    r"""(?:is_feature_enabled|get_feature_value)\(\s*["']([A-Za-z0-9_.-]+)["']\s*\)"""
)


@dataclass(frozen=True, slots=True)
class CodeReference:
    """Location where a Flagsmith flag key is evaluated in source code."""

    flag_key: str
    file: str
    function: str
    start_line: int
    end_line: int


class CodeIndex:
    """Index of Flagsmith flag keys discovered under configured code paths."""

    def __init__(self, refs: list[CodeReference]) -> None:
        self.refs = refs

    @classmethod
    def build(cls, base_dir: Path, paths: list[str]) -> CodeIndex:
        """Scan Python files under configured relative or absolute paths."""
        refs: list[CodeReference] = []
        for raw_path in paths:
            target = Path(raw_path)
            if not target.is_absolute():
                target = base_dir / target
            if not target.exists():
                continue
            py_files = [target] if target.is_file() else sorted(target.rglob("*.py"))
            for py_file in py_files:
                refs.extend(_scan_python_file(py_file))
        return cls(refs)

    def matches_error_frames(
        self,
        flag_key: str,
        error_samples: list[dict[str, Any]],
    ) -> bool:
        """Return True if flag_key is evaluated in a file/function present in error frames."""
        matching_refs = [ref for ref in self.refs if ref.flag_key == flag_key]
        if not matching_refs:
            return False

        frame_pairs: list[tuple[str, str, int]] = []
        for sample in error_samples:
            for frame in sample.get("frames", []):
                fname = Path(str(frame.get("file", ""))).name.lower()
                func = str(frame.get("function", ""))
                line = int(frame.get("line", 0) or 0)
                if fname:
                    frame_pairs.append((fname, func, line))

        for ref in matching_refs:
            ref_name = Path(ref.file).name.lower()
            for fname, func, line in frame_pairs:
                if fname != ref_name:
                    continue
                if ref.function == func or (ref.start_line <= line <= ref.end_line):
                    return True
        return False


def _scan_python_file(py_file: Path) -> list[CodeReference]:
    try:
        source = py_file.read_text(encoding="utf-8")
    except OSError:
        return []

    func_spans: list[tuple[str, int, int]] = []
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", node.lineno) or node.lineno
                func_spans.append((node.name, node.lineno, end))
    except SyntaxError:
        pass

    refs: list[CodeReference] = []
    for idx, line_text in enumerate(source.splitlines(), start=1):
        for match in _FLAG_CALL_RE.finditer(line_text):
            flag_key = match.group(1)
            fn_name, start_line, end_line = _enclosing_func(idx, func_spans)
            refs.append(
                CodeReference(
                    flag_key=flag_key,
                    file=str(py_file),
                    function=fn_name,
                    start_line=start_line,
                    end_line=end_line,
                )
            )
    return refs


def _enclosing_func(line_no: int, spans: list[tuple[str, int, int]]) -> tuple[str, int, int]:
    best: tuple[str, int, int] = ("<module>", line_no, line_no)
    best_width = 10**9
    for name, start, end in spans:
        if start <= line_no <= end and (end - start) < best_width:
            best = (name, start, end)
            best_width = end - start
    return best
