"""Loading and validating the classification policy.

Validation is strict because every failure mode of the policy file is silent: a
mistyped key would disable a rule and the resulting numbers would still add up.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collector import policy as policy_module
from collector.classify import classify
from collector.policy import Policy, PolicyError, load_policy, policy, set_policy


def write_policy(tmp_path: Path, **overrides) -> Path:
    body = {
        "display": {"yaml": "YAML"},
        "demote": ["plain-text"],
        "ignore": ["json"],
        "count_anyway": ["jupyter"],
        "ignore_extensions": ["lock"],
        "ignore_directories": ["vendor"],
        "ignore_globs": ["*.pb.go"],
        "unknown_threshold": 0.01,
    }
    body.update(overrides)
    path = tmp_path / "policy.yml"
    path.write_text(yaml.safe_dump(body))
    return path


def make_policy(**overrides) -> Policy:
    fields = {
        "display": {}, "demote": frozenset(), "ignore": frozenset(),
        "count_anyway": frozenset(), "ignore_extensions": frozenset(),
        "ignore_directories": frozenset(), "ignore_globs": (),
        "unknown_threshold": 0.5,
    }
    fields.update(overrides)
    return Policy(**fields)


class TestCommittedPolicy:
    def test_it_loads(self):
        assert policy().unknown_threshold == 0.01

    def test_it_declares_no_residual_category(self):
        """Unclassifiable files are whatever no rule claimed, and a key here would
        imply they are something that can be configured."""
        raw = yaml.safe_load(policy_module.DEFAULT_POLICY_PATH.read_text())
        assert "unknown" not in raw

    def test_it_resolves_relative_to_the_package(self, monkeypatch, tmp_path):
        """A run started from another directory must load the same policy."""
        monkeypatch.chdir(tmp_path)
        set_policy(None)
        assert "gitattributes" in policy().ignore

    def test_it_is_loaded_only_once(self, monkeypatch):
        """Classification runs once per file record, and there are thousands."""
        calls = []
        real = load_policy
        monkeypatch.setattr(policy_module, "load_policy",
                            lambda *a, **k: calls.append(1) or real(*a, **k))
        set_policy(None)
        for _ in range(50):
            classify("main.py")
        assert len(calls) == 1


class TestRejection:
    def test_a_residual_key_is_rejected(self, tmp_path):
        with pytest.raises(PolicyError, match="residual"):
            load_policy(write_policy(tmp_path, unknown=["cfg"]))

    def test_an_unrecognized_key_is_rejected(self, tmp_path):
        """A mistyped key would otherwise disable a rule without a word."""
        with pytest.raises(PolicyError, match="unrecognized"):
            load_policy(write_policy(tmp_path, ignore_extension=["lock"]))

    def test_a_missing_key_is_rejected(self, tmp_path):
        raw = yaml.safe_load(write_policy(tmp_path).read_text())
        del raw["ignore"]
        (tmp_path / "other.yml").write_text(yaml.safe_dump(raw))
        with pytest.raises(PolicyError, match="missing"):
            load_policy(tmp_path / "other.yml")

    def test_a_tag_cannot_both_be_ignored_and_rescued(self, tmp_path):
        """One says discard the file and the other says keep it."""
        path = write_policy(tmp_path, ignore=["json"], count_anyway=["json"])
        with pytest.raises(PolicyError, match="both"):
            load_policy(path)

    @pytest.mark.parametrize("overrides,message", [
        ({"unknown_threshold": "1%"}, "wrong type"),
        ({"unknown_threshold": 1.5}, "fraction"),
        ({"ignore": "json"}, "wrong type"),
        ({"display": {"ts": 3}}, "strings to strings"),
        ({"ignore": [3]}, "must hold strings"),
        ({"ignore_globs": [7]}, "must hold strings"),
    ])
    def test_malformed_values_are_rejected(self, tmp_path, overrides, message):
        with pytest.raises(PolicyError, match=message):
            load_policy(write_policy(tmp_path, **overrides))

    def test_a_non_mapping_file_is_rejected(self, tmp_path):
        path = tmp_path / "policy.yml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(PolicyError, match="mapping"):
            load_policy(path)


class TestOverride:
    def test_an_installed_policy_changes_classification(self):
        set_policy(make_policy(ignore=frozenset({"python"})))
        assert classify("main.py") == (None, "ignored")

    def test_extensions_are_stored_lower_cased(self, tmp_path):
        rules = load_policy(write_policy(tmp_path, ignore_extensions=["LOCK"]))
        assert rules.ignore_extensions == frozenset({"lock"})
