from __future__ import annotations

import textwrap
from pathlib import Path


def test_vieneu_hardware_script_parses_before_the_opt_in_gate_runs() -> None:
    hardware_test = Path(__file__).with_name("test_vieneu_hardware.py")
    source = hardware_test.read_text(encoding="utf-8")
    script = source.split("    script = r'''", 1)[1].split("'''", 1)[0]
    compile(textwrap.dedent(script), str(hardware_test), "exec")
