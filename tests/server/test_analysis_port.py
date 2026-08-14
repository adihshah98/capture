"""The contract capture.server depends on from capture.analyze.

These are the fast tier: no database, no network, no disk. capture.analyze is
replaced with a recorder, which is the point of having a port at all — before it
existed, nothing about cohorts could be exercised without Postgres and a model.

What is pinned here is the seam itself, not the analysis: that every entry point
takes a Project and converts it to whatever capture.analyze wants, and that the
two values tests monkeypatch are read at call time rather than copied at import.
"""

import ast
import re
import unittest
from pathlib import Path
from unittest import mock

from capture import analysis_port as port
from capture.projects import Project

DEFAULT = Project(
    id="11111111-1111-1111-1111-111111111111",
    name="Default",
    org_id=None,
    domains=(),
    storage_prefix="",
    is_default=True,
)

NAMED = Project(
    id="22222222-2222-2222-2222-222222222222",
    name="Brand",
    org_id="org_1",
    domains=("example.com",),
    storage_prefix="cap_abc",
    is_default=False,
)

# Owner modules the port calls through. Patching them here means a test that
# reaches the real analysis package fails loudly instead of doing work.
_PORT_MODULES = (
    "storage",
    "runtime",
    "sampling",
    "session",
    "cohort_jobs",
    "cohort_state",
    "cuts",
    "gemini",
    "labels",
)


class PortCase(unittest.TestCase):
    def setUp(self):
        for name in _PORT_MODULES:
            mock.patch.object(port, name).start()
        self.addCleanup(mock.patch.stopall)
        self.storage = port.storage
        self.runtime = port.runtime
        self.sampling = port.sampling
        self.cuts = port.cuts


class TestProjectIsTheIdentity(PortCase):
    """capture.analyze addresses Postgres by project.id; Path only for recordings."""

    def test_cohort_calls_are_addressed_by_project_id(self):
        port.cohort(NAMED, "c1")
        self.storage.read_cohort.assert_called_once_with(NAMED.id, "c1")

    def test_status_updates_are_addressed_by_project_id(self):
        cohort = {"id": "c1", "status": "idle"}
        port.set_cohort_status(NAMED, cohort, "running")
        self.storage.set_cohort_status.assert_called_once_with(
            NAMED.id, cohort, "running", batch=None, clear_batch=False
        )

    def test_the_default_project_uses_its_uuid(self):
        port.cohort(DEFAULT, "c1")
        self.storage.read_cohort.assert_called_once_with(DEFAULT.id, "c1")

    def test_sampling_calls_are_addressed_by_project_id(self):
        conn = object()
        port.pool_facets(conn, NAMED, min_duration_ms=5000)
        self.sampling.pool_facets.assert_called_once_with(conn, NAMED.id, min_duration_ms=5000)

    def test_classified_rows_are_addressed_by_project_id(self):
        conn = object()
        port.classified_rows(conn, NAMED, ["s1", "s2"])
        self.sampling.classified_rows.assert_called_once_with(conn, NAMED.id, ["s1", "s2"])

    def test_session_jobs_are_addressed_by_project_id(self):
        port.job(NAMED, "sess1")
        self.storage.read_job.assert_called_once_with(NAMED.id, "sess1")

    def test_two_projects_never_share_a_job_key(self):
        port.job(DEFAULT, "sess1")
        port.job(NAMED, "sess1")
        first, second = (c[0][:2] for c in self.storage.read_job.call_args_list)
        self.assertNotEqual(first, second)
        self.assertEqual(first[1], second[1])  # same session_id


class TestReadAtCallTime(PortCase):
    """A copy taken at import would not see the patch. 17 tests flip
    ANALYSIS_ENABLED and 5 replace stream_gemini_chat."""

    def test_enabled_follows_a_patched_flag(self):
        self.runtime.ANALYSIS_ENABLED = False
        self.assertFalse(port.enabled())
        self.runtime.ANALYSIS_ENABLED = True
        self.assertTrue(port.enabled())

    def test_enabled_is_a_bool_even_when_the_flag_is_a_key(self):
        self.runtime.ANALYSIS_ENABLED = "some-api-key"
        self.assertIs(port.enabled(), True)

    def test_stream_chat_resolves_the_replaced_function(self):
        sentinel = object()
        self.cuts.stream_gemini_chat.return_value = sentinel
        got = port.stream_chat(["row"], "label", "given", [{"role": "user"}])
        self.assertIs(got, sentinel)
        self.cuts.stream_gemini_chat.assert_called_once_with(
            ["row"], "label", "given", [{"role": "user"}], None
        )


class TestPrivateAccessIsContained(PortCase):
    def test_model_gate_takes_the_second_gate(self):
        self.runtime._gates.return_value = (3, 7)
        self.assertEqual(port.model_gate(), 7)

    def test_gates_is_the_only_private_the_port_reaches_for(self):
        """Parsed rather than grepped, so the docstring that explains _gates does
        not count as a second use. A new name here means the boundary leaked."""
        tree = ast.parse(Path(port.__file__).read_text())
        privates = sorted(
            {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in {*_PORT_MODULES, "runtime"}
                and node.attr.startswith("_")
            }
        )
        self.assertEqual(privates, ["_gates"])


class TestVocabularyIsExposed(unittest.TestCase):
    """Query() defaults read these at import, so they must be plain values."""

    def test_the_cohort_limits_are_concrete(self):
        for name in (
            "MAX_SESSIONS",
            "MIN_DURATION_MS",
        ):
            self.assertIsInstance(getattr(port, name), int, name)

    def test_the_order_vocabulary_is_a_sequence_containing_its_default(self):
        self.assertIn(port.ORDER_LATEST, port.ORDERS)


class TestNoOneElseImportsAnalyze(unittest.TestCase):
    """The rule the port exists to make true. capture/analyze/ may import itself;
    nothing else in capture/ may, except this module."""

    def test_only_the_port_reaches_the_analysis_package(self):
        """Recursive on purpose. This globbed capture/*.py when the package was
        flat, so once the routers moved under capture/api/ any one of them could
        have imported analyze directly and walked straight past the guard.
        """
        root = Path(__file__).resolve().parent.parent.parent / "capture"
        offenders = []
        for source in root.rglob("*.py"):
            if source.name == "analysis_port.py":
                continue
            if "analyze" in source.relative_to(root).parts:
                continue  # the analysis package may import itself
            text = source.read_text()
            if re.search(
                r"^\s*(?:from \.*analyze|from \.\. import .*\banalyze\b|"
                r"from \. import .*\banalyze\b|import analyze)",
                text,
                re.M,
            ):
                offenders.append(str(source.relative_to(root)))
        self.assertEqual(sorted(offenders), [], f"these import analyze directly: {offenders}")

    def test_the_guard_covers_the_routers(self):
        """The above is only meaningful if it actually reaches capture/api/."""
        root = Path(__file__).resolve().parent.parent.parent / "capture"
        scanned = {
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if "analyze" not in p.relative_to(root).parts
        }
        self.assertIn("api/cohorts.py", scanned)
        self.assertIn("api/ingest.py", scanned)
        self.assertIn("server.py", scanned)


if __name__ == "__main__":
    unittest.main()
