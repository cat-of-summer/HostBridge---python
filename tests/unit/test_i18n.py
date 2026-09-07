"""The translation catalogues must stay honest.

A key that exists in English but not in Russian degrades silently -- the user simply sees
English in the middle of a Russian dialog -- so nothing but a test will catch it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from ui import i18n

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ("app", "client", "core", "daemon", "discover", "resolver", "system", "ui")


def _load(code: str) -> dict[str, str]:
    return json.loads((ROOT / "lang" / f"{code}.json").read_text(encoding="utf-8-sig"))


def _translation_keys_used_in_source() -> set[str]:
    """Every ``t("literal")`` in the tree. Dynamic keys are invisible here by design."""
    keys: set[str] = set()
    files = [ROOT / "main.py"]
    for directory in SOURCE_DIRS:
        files.extend(sorted((ROOT / directory).rglob("*.py")))

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "t" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
    return keys


def test_catalogues_declare_the_same_keys():
    english, russian = _load("en"), _load("ru")
    assert set(english) == set(russian)


def test_every_key_used_in_source_exists_in_english():
    missing = sorted(_translation_keys_used_in_source() - set(_load("en")))
    assert not missing, f"keys used in source but absent from lang/en.json: {missing}"


@pytest.mark.parametrize("code", i18n.SUPPORTED_LANGUAGES)
def test_every_catalogue_value_is_a_string(code):
    assert all(isinstance(value, str) for value in _load(code).values())


def test_placeholders_match_between_catalogues():
    import string

    english, russian = _load("en"), _load("ru")
    formatter = string.Formatter()

    def fields(template: str) -> set[str]:
        return {name for _, name, _, _ in formatter.parse(template) if name}

    mismatched = {
        key: (fields(english[key]), fields(russian[key]))
        for key in english
        if fields(english[key]) != fields(russian.get(key, ""))
    }
    assert not mismatched, f"placeholder mismatch between catalogues: {mismatched}"


def test_normalise_accepts_locale_shaped_values():
    assert i18n.normalise("ru_RU.UTF-8") == "ru"
    assert i18n.normalise("en-GB") == "en"
    assert i18n.normalise("de_DE") == ""
    assert i18n.normalise(None) == ""


def test_unknown_key_returns_the_key_itself():
    assert i18n.t("no.such.key") == "no.such.key"


def test_missing_russian_key_falls_back_to_english(monkeypatch):
    monkeypatch.delenv("HOSTBRIDGE_LANG", raising=False)
    i18n.set_language("ru")
    try:
        monkeypatch.setitem(i18n._catalogs, "ru", {})
        monkeypatch.setitem(i18n._catalogs, "en", {"probe": "fallback"})
        assert i18n.t("probe") == "fallback"
    finally:
        i18n.set_language("en")


def test_environment_override_beats_an_explicit_request(monkeypatch):
    monkeypatch.setenv("HOSTBRIDGE_LANG", "en")
    assert i18n.set_language("ru") == "en"


def test_formatting_a_template_with_a_wrong_argument_returns_the_template(monkeypatch):
    monkeypatch.setitem(i18n._catalogs, "en", {"probe": "hello {name}"})
    assert i18n.t("probe", wrong="x") == "hello {name}"
