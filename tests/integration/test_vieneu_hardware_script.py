from __future__ import annotations

import ast
import textwrap
from pathlib import Path


def test_vieneu_hardware_script_parses_before_the_opt_in_gate_runs() -> None:
    hardware_test = Path(__file__).with_name("test_vieneu_hardware.py")
    source = hardware_test.read_text(encoding="utf-8")
    # Inspect the literal's value independently of formatter quote choices.
    script = next(
        node.value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "script" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )
    compile(textwrap.dedent(script), str(hardware_test), "exec")
