"""Keep deploy and local requirement pins from drifting apart."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_NAME_SPEC = re.compile(r"^([A-Za-z0-9_.-]+)(.*)$")


def _parse_requirements(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _NAME_SPEC.match(line.replace(" ", ""))
        if not match:
            continue
        pins[match.group(1).lower()] = match.group(2)
    return pins


def test_lambda_and_full_requirements_agree_on_shared_packages():
    lambda_pins = _parse_requirements(_ROOT / "requirements-lambda.txt")
    full_pins = _parse_requirements(_ROOT / "requirements.txt")
    shared = set(lambda_pins) & set(full_pins)
    assert shared, "expected overlapping packages between lambda and full requirements"
    drifted = {
        name: (lambda_pins[name], full_pins[name])
        for name in sorted(shared)
        if lambda_pins[name] != full_pins[name]
    }
    assert drifted == {}, f"shared requirement pins drifted: {drifted}"


def test_lambda_requirements_cap_major_versions():
    pins = _parse_requirements(_ROOT / "requirements-lambda.txt")
    for name in ("pandas", "numpy", "matplotlib", "cryptography", "pymysql", "slack-sdk"):
        assert name in pins, name
        assert "<" in pins[name], f"{name} is missing a major-version ceiling: {pins[name]}"
