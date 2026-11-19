"""Standalone test for _strip_prompt_weighting in fixer_desktop.py.

Importing fixer_desktop has heavy top-level side effects, so instead of importing the
module this pulls just the _strip_prompt_weighting function out of the source with ast
and execs it in an isolated namespace (so it tests the REAL source, with no drift and
no side effects). Run: python test_weighting.py
"""
import ast
import pathlib
import re
import sys

SRC = pathlib.Path(__file__).with_name("fixer_desktop.py").read_text(encoding="utf-8")
_ns = {"re": re}
for _node in ast.parse(SRC).body:
    if isinstance(_node, ast.FunctionDef) and _node.name == "_strip_prompt_weighting":
        exec(compile(ast.Module(body=[_node], type_ignores=[]), "fixer_desktop.py", "exec"), _ns)
        break
else:
    print("FAIL: could not find _strip_prompt_weighting in fixer_desktop.py")
    sys.exit(1)

strip = _ns["_strip_prompt_weighting"]

CASES = [
    # (input, expected, label)
    ("(large breasts:1.3)", "large breasts", "single float weight"),
    ("((x:1.2):1.1)", "x", "nested weights"),
    ("((bokeh))", "bokeh", "nested emphasis, no numbers"),
    ("(a:1.1), (b:1.2), c", "a, b, c", "multiple weighted groups in one string"),
    (r"\(literal\)", "(literal)", "escaped parens stay literal"),
    (r"a \(b\) c", "a (b) c", "escaped parens mid-string"),
    ("[deemphasis]", "deemphasis", "de-emphasis brackets"),
    ("a cat sitting on a mat", "a cat sitting on a mat", "plain string unchanged"),
    ("red, green, blue", "red, green, blue", "comma list stays clean"),
    ("a photo, (large breasts:1.3), [blurry], ((bokeh:1.2):1.1), \\(c\\)",
     "a photo, large breasts, blurry, bokeh, (c)", "everything at once stays clean"),
    ("(x:2)", "x", "integer weight"),
    ("(score_9:1.5), (score_8:1.4)", "score_9, score_8", "underscore tags with weights"),
    ("", "", "empty string"),
]

failures = 0
for text, expected, label in CASES:
    got = strip(text)
    ok = got == expected
    if not ok:
        failures += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print(f"       in:  {text!r}")
    print(f"       out: {got!r}")
    if not ok:
        print(f"       want:{expected!r}")

print()
print(f"{len(CASES) - failures}/{len(CASES)} passed")
sys.exit(1 if failures else 0)
