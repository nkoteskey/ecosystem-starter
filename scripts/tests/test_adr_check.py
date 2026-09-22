"""Unit tests for scripts/adr-check.py (ADR immutability, docs/DESIGN.md §5.4).

Stdlib `unittest` only, no network. Every fixture is a real temporary git
repo this test creates and tears down itself — the tool reads content via
`git show <ref>:<path>`, so a real repo with real commits is the only
faithful way to exercise it.

Run:
    python3 -m unittest scripts/tests/test_adr_check.py -v
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "adr-check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("adr_check", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adrc = _load_module()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def accepted_adr(title: str = "Some decision") -> str:
    return textwrap.dedent(
        f"""\
        # 0001. {title}

        - **Status:** Accepted
        - **Acceptance:** Prospective
        - **Date:** 2026-01-01
        - **Deciders:** maintainers

        > Derives from NORTH_STAR.md. NORTH_STAR wins on conflict.

        ## Context

        Original context text.

        ## Decision Drivers

        - driver one

        ## Considered Options

        1. **Option A** — desc.

        ## Decision

        **Chosen: Option A.**

        ## Consequences

        ### Negative

        - some cost
        """
    )


class GitRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test-fixture")
        _git(self.repo, "config", "user.name", "Test")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, content: str) -> None:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def remove(self, rel: str) -> None:
        (self.repo / rel).unlink()

    def commit(self, msg: str) -> str:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", msg)
        return _git(self.repo, "rev-parse", "HEAD").strip()

    def run_check(self, base: str, head: str = "HEAD", rules=None):
        return adrc.check(base, head, cwd=self.repo, rules=rules)


class TestStatusPointerOnly(GitRepoCase):
    def test_status_pointer_plus_new_superseding_adr_is_allowed(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace("- **Status:** Accepted", "- **Status:** Superseded by ADR-0002")
        self.write("adr/0001-some-decision.md", text)
        self.write(
            "adr/0002-new-decision.md",
            textwrap.dedent(
                """\
                # 0002. New decision

                - **Status:** Accepted
                - **Acceptance:** Prospective
                - **Date:** 2026-02-01
                - **Deciders:** maintainers
                - **Supersedes:** ADR-0001

                ## Context

                New context.

                ## Decision Drivers

                - x

                ## Considered Options

                1. **Option A** — desc.

                ## Decision

                **Chosen: Option A.**

                ## Consequences

                ### Negative

                - cost
                """
            ),
        )
        head = self.commit("supersede ADR-0001 with ADR-0002")

        ok, failures, checked = self.run_check(base, head)
        self.assertTrue(ok, failures)
        self.assertEqual(checked, 1)

    def test_status_pointer_without_new_superseding_adr_fails(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace("- **Status:** Accepted", "- **Status:** Superseded by ADR-0002")
        self.write("adr/0001-some-decision.md", text)
        head = self.commit("point to nonexistent ADR-0002")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertTrue(any("0001" in f for f in failures))


class TestSubstantiveEditRejected(GitRepoCase):
    def test_substantive_edit_without_marker_fails(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace("Original context text.", "Rewritten context text!")
        self.write("adr/0001-some-decision.md", text)
        head = self.commit("quietly rewrite the Context section")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertEqual(checked, 1)
        self.assertTrue(any("0001" in f for f in failures))

    def test_substantive_edit_with_marker_still_fails(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace("Original context text.", "Rewritten context text!")
        self.write("adr/0001-some-decision.md", text)
        head = self.commit("rewrite context [adr-nonsubstantive]")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)


class TestNonsubstantiveMarkerPath(GitRepoCase):
    def test_typo_fix_with_marker_and_identical_sections_is_allowed(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace(
            "> Derives from NORTH_STAR.md. NORTH_STAR wins on conflict.",
            "> Derives from `NORTH_STAR.md`. NORTH_STAR wins on conflict.",
        )
        self.write("adr/0001-some-decision.md", text)
        head = self.commit("fix broken link formatting [adr-nonsubstantive]")

        ok, failures, checked = self.run_check(base, head)
        self.assertTrue(ok, failures)
        self.assertEqual(checked, 1)

    def test_status_line_changed_under_marker_fails(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace("- **Status:** Accepted", "- **Status:** Accepted (amended)")
        self.write("adr/0001-some-decision.md", text)
        head = self.commit("tweak status wording [adr-nonsubstantive]")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertTrue(any("Status line changed" in f for f in failures))

    def test_title_changed_under_marker_fails(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace("# 0001. Some decision", "# 0001. Some decision (renamed)")
        self.write("adr/0001-some-decision.md", text)
        head = self.commit("retitle [adr-nonsubstantive]")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertTrue(any("title line changed" in f for f in failures))

    def test_heading_added_under_marker_fails(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text += "\n## Validation\n\nSome new heading.\n"
        self.write("adr/0001-some-decision.md", text)
        head = self.commit("add a Validation section [adr-nonsubstantive]")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertTrue(any("heading was added" in f for f in failures))

    def test_supersedes_line_changed_under_marker_fails(self):
        base_text = accepted_adr()
        base_text = base_text.replace(
            "- **Deciders:** maintainers\n",
            "- **Deciders:** maintainers\n- **Supersedes:** ADR-0000\n",
        )
        self.write("adr/0001-some-decision.md", base_text)
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace("- **Supersedes:** ADR-0000", "- **Supersedes:** ADR-0000 (typo fixed)")
        self.write("adr/0001-some-decision.md", text)
        head = self.commit("edit supersedes line [adr-nonsubstantive]")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertTrue(any("Supersedes:" in f for f in failures))

    def test_marker_on_unrelated_commit_does_not_unlock_the_file(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        self.write("README.md", "root readme\n")
        base = self.commit("base")

        self.write("README.md", "root readme v2\n")
        self.commit("unrelated change [adr-nonsubstantive]")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace("Original context text.", "Rewritten context text!")
        self.write("adr/0001-some-decision.md", text)
        head = self.commit("quietly rewrite context")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertTrue(any("0001" in f for f in failures))

    def test_marker_on_commit_touching_the_file_unlocks_it(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        self.write("README.md", "root readme\n")
        base = self.commit("base")

        text = (self.repo / "adr/0001-some-decision.md").read_text()
        text = text.replace(
            "> Derives from NORTH_STAR.md. NORTH_STAR wins on conflict.",
            "> Derives from `NORTH_STAR.md`. NORTH_STAR wins on conflict.",
        )
        self.write("adr/0001-some-decision.md", text)
        self.write("README.md", "root readme v2\n")
        head = self.commit("fix link + touch readme [adr-nonsubstantive]")

        ok, failures, checked = self.run_check(base, head)
        self.assertTrue(ok, failures)


class TestDeletedOrRenamedRejected(GitRepoCase):
    def test_deleted_adr_fails(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")
        self.remove("adr/0001-some-decision.md")
        head = self.commit("remove ADR-0001")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertTrue(any("DELETED" in f for f in failures))

    def test_renamed_adr_fails(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")
        _git(self.repo, "mv", "adr/0001-some-decision.md", "adr/0001-renamed-decision.md")
        head = self.commit("rename ADR-0001")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertTrue(any("RENAMED" in f for f in failures))

    def test_ignored_basenames_are_unconstrained(self):
        self.write("adr/README.md", "v1\n")
        self.write("adr/OPEN-QUESTIONS.md", "v1\n")
        self.write("adr/0000-template.md", "v1\n")
        base = self.commit("base")
        self.remove("adr/README.md")
        self.write("adr/OPEN-QUESTIONS.md", "v2\n")
        self.write("adr/0000-template.md", "v2\n")
        head = self.commit("edit/remove unconstrained files")

        ok, failures, checked = self.run_check(base, head)
        self.assertTrue(ok, failures)


class TestNewAdrNumbering(GitRepoCase):
    def test_new_adr_numbered_max_plus_one_is_allowed(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")
        self.write("adr/0002-another-decision.md", accepted_adr("Another decision"))
        head = self.commit("add ADR-0002")

        ok, failures, checked = self.run_check(base, head)
        self.assertTrue(ok, failures)

    def test_template_does_not_count_toward_numbering(self):
        self.write("adr/0000-template.md", "template\n")
        base = self.commit("base")
        self.write("adr/0001-first.md", accepted_adr("First"))
        head = self.commit("add ADR-0001")

        ok, failures, checked = self.run_check(base, head)
        self.assertTrue(ok, failures)

    def test_new_adr_with_gap_fails(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")
        self.write("adr/0005-skips-ahead.md", accepted_adr("Skips ahead"))
        head = self.commit("add ADR-0005, skipping numbers")

        ok, failures, checked = self.run_check(base, head)
        self.assertFalse(ok)
        self.assertTrue(any("numbering" in f for f in failures))

    def test_several_new_adrs_in_sequence_is_allowed(self):
        self.write("adr/0001-some-decision.md", accepted_adr())
        base = self.commit("base")
        self.write("adr/0002-second.md", accepted_adr("Second"))
        self.write("adr/0003-third.md", accepted_adr("Third"))
        head = self.commit("add ADR-0002 and ADR-0003")

        ok, failures, checked = self.run_check(base, head)
        self.assertTrue(ok, failures)


class TestConfigurableDir(GitRepoCase):
    def test_custom_adr_dir_is_honoured(self):
        rules = adrc.Rules(
            {
                "dir": "docs/decisions",
                "ignored_basenames": ["README.md"],
                "nonsubstantive_marker": "[decision-nonsubstantive]",
                "section_headings": ["## Context", "## Decision", "## Consequences"],
            }
        )
        self.write("docs/decisions/0001-some-decision.md", accepted_adr())
        base = self.commit("base")
        text = (self.repo / "docs/decisions/0001-some-decision.md").read_text()
        text = text.replace("Original context text.", "Rewritten!")
        self.write("docs/decisions/0001-some-decision.md", text)
        head = self.commit("rewrite")

        ok, failures, checked = self.run_check(base, head, rules=rules)
        self.assertFalse(ok)
        self.assertEqual(checked, 1)


if __name__ == "__main__":
    unittest.main()
