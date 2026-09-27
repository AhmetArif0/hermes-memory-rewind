"""Packaging checks: what the catalog installs and what its docs page renders."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "memory-rewind"


def test_plugin_readme_matches_repo_readme():
    """The catalog docs page renders <subdir>/README.md; GitHub renders the root one. Keep them identical."""
    assert (PLUGIN / "README.md").read_bytes() == (ROOT / "README.md").read_bytes()


def test_manifest_declares_exactly_the_registered_hooks():
    manifest = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
    hooks_block = manifest.split("provides_hooks:")[1].split("config_schema:")[0]
    declared = sorted(line.strip()[2:] for line in hooks_block.splitlines() if line.strip().startswith("- "))
    source = (PLUGIN / "__init__.py").read_text(encoding="utf-8")
    registered = sorted(part.split('"')[1] for part in source.split("ctx.register_hook(")[1:])
    assert declared == registered == ["on_session_end", "on_session_start", "post_tool_call", "pre_llm_call"]
