"""
Spec/code drift guard.

knowledge/flags/flag_catalog.md is the spec; signals/flags.py is the
implementation. This test parses the flag ids out of the catalog markdown and
asserts the sets match, both directions. Parked flags (### headings under
"## Parked") must be absent from the code — a parked flag that quietly gains
an implementation, or a live flag that quietly loses its catalog entry, fails
CI here.

Conventions this parser relies on (documented in the catalog itself):
- live flags are level-2 headings:   ## `flag_id`
- parked flags are level-3 headings: ### `flag_id` — parked <date>
"""

import re
from pathlib import Path

from signals.flags import FLAG_RULES

_CATALOG = (Path(__file__).resolve().parents[2]
            / "knowledge" / "flags" / "flag_catalog.md")

_LIVE_RE = re.compile(r"^## `([a-z_]+)`", re.MULTILINE)
_PARKED_RE = re.compile(r"^### `([a-z_]+)`", re.MULTILINE)


def _catalog_text() -> str:
    return _CATALOG.read_text(encoding="utf-8")


def test_catalog_exists():
    assert _CATALOG.is_file(), f"flag catalog missing at {_CATALOG}"


def test_every_catalog_flag_is_implemented():
    live = set(_LIVE_RE.findall(_catalog_text()))
    missing = live - set(FLAG_RULES)
    assert not missing, (
        f"flags specified in the catalog but absent from flags.py: {missing}"
    )


def test_every_implemented_flag_is_in_catalog():
    live = set(_LIVE_RE.findall(_catalog_text()))
    undocumented = set(FLAG_RULES) - live
    assert not undocumented, (
        f"flags registered in flags.py but absent from the catalog: {undocumented}"
    )


def test_parked_flags_absent_from_code():
    text = _catalog_text()
    parked = set(_PARKED_RE.findall(text))
    assert parked, "Parked section is empty or headings changed format"
    implemented = parked & set(FLAG_RULES)
    assert not implemented, (
        f"parked flags must not be implemented, found: {implemented}"
    )


def test_parked_and_live_are_disjoint():
    text = _catalog_text()
    overlap = set(_LIVE_RE.findall(text)) & set(_PARKED_RE.findall(text))
    assert not overlap, f"flags listed as both live and parked: {overlap}"
