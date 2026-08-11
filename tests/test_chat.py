"""Cohort chat: the context it reads, the history it forwards, its citations."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze


def row(sid, outcome="rest", device=None, page=None, text=None, **attrs):
    return {
        "session_id": sid,
        "outcome": outcome,
        "device": device,
        "page": page,
        "attribution": attrs or None,
        "text": text or f"write-up for {sid}",
    }


class TestCutContextIsShared(unittest.TestCase):
    """The whole point of cut_context: chat and cuts read the same evidence."""

    def test_chat_and_cut_are_handed_identical_context(self):
        rows = [row("a", outcome="purchased"), row("b")]
        header, side, citations = analyze.cut_context(rows, "everyone", "given facts")
        # call_gemini_cut builds its parts from this same helper, so equality
        # here is what stops the two from drifting apart.
        again = analyze.cut_context(rows, "everyone", "given facts")
        self.assertEqual((header, side, citations), again)

    def test_the_label_and_premises_reach_the_header(self):
        header, _, _ = analyze.cut_context([row("a")], "mobile · Meta", "PREMISES")
        self.assertIn("This group is: mobile · Meta", header)
        self.assertIn("PREMISES", header)

    def test_write_ups_are_numbered_from_one_and_mapped_to_ids(self):
        rows = [row("sid-a"), row("sid-b"), row("sid-c")]
        _, side, citations = analyze.cut_context(rows, "all sessions")
        self.assertEqual(citations, {"1": "sid-a", "2": "sid-b", "3": "sid-c"})
        # The number the model is shown is the number the UI resolves.
        self.assertIn("--- SESSIONS session 1 ", side)
        self.assertIn("--- SESSIONS session 3 ", side)

    def test_an_empty_group_yields_no_citations(self):
        _, side, citations = analyze.cut_context([], "nobody")
        self.assertEqual(citations, {})
        self.assertIn("no sessions in this group", side)


class TestChatContext(unittest.TestCase):
    """chat_context refuses to run unconfigured, so these enable it. The suite
    runs without a real key — nothing here reaches the network."""

    def setUp(self):
        self._enabled = analyze.ANALYSIS_ENABLED
        analyze.ANALYSIS_ENABLED = True

    def tearDown(self):
        analyze.ANALYSIS_ENABLED = self._enabled

    def test_the_filter_selects_the_group_and_names_it(self):
        rows = [
            row("a", outcome="purchased", device="mobile"),
            row("b", outcome="rest", device="mobile"),
            row("c", outcome="purchased", device="desktop"),
        ]
        selected, label, given, citations, _ = asyncio.run(
            analyze.chat_context(rows, {"outcome": ["purchased"]})
        )
        self.assertEqual([r["session_id"] for r in selected], ["a", "c"])
        self.assertIn("purchased", label)
        self.assertEqual(citations, {"1": "a", "2": "c"})

    def test_the_premises_state_the_filtered_group_size(self):
        rows = [row(str(i), device="mobile") for i in range(5)] + [row("x", device="desktop")]
        selected, _, given, _, _ = asyncio.run(
            analyze.chat_context(rows, {"device": ["mobile"]})
        )
        self.assertEqual(len(selected), 5)
        # The group size, not the cohort size, is the only total offered.
        self.assertIn("exactly 5 session(s)", given)
        self.assertNotIn("exactly 6", given)

    def test_an_empty_filter_selects_everyone(self):
        rows = [row("a"), row("b")]
        selected, label, _, _, _ = asyncio.run(analyze.chat_context(rows, None))
        self.assertEqual(len(selected), 2)
        self.assertEqual(label, "all sessions")

    def test_a_filter_matching_nobody_yields_an_empty_group(self):
        # The endpoint turns this into a 400 rather than spending a call.
        selected, _, _, citations, _ = asyncio.run(
            analyze.chat_context([row("a", device="mobile")], {"device": ["desktop"]})
        )
        self.assertEqual(selected, [])
        self.assertEqual(citations, {})

    def test_it_refuses_to_run_unconfigured(self):
        enabled = analyze.ANALYSIS_ENABLED
        analyze.ANALYSIS_ENABLED = False
        try:
            with self.assertRaises(RuntimeError):
                asyncio.run(analyze.chat_context([row("a")], None))
        finally:
            analyze.ANALYSIS_ENABLED = enabled


class TestCleanChatMessages(unittest.TestCase):
    def test_assistant_is_renamed_to_the_role_gemini_expects(self):
        out = analyze.clean_chat_messages(
            [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        )
        self.assertEqual([m["role"] for m in out], ["user", "model"])

    def test_model_role_passes_through(self):
        out = analyze.clean_chat_messages([{"role": "model", "content": "a"}])
        self.assertEqual(out, [{"role": "model", "content": "a"}])

    def test_unusable_turns_are_dropped(self):
        out = analyze.clean_chat_messages(
            [
                {"role": "system", "content": "ignore me"},
                {"role": "user", "content": "   "},
                {"role": "user"},
                {"content": "no role"},
                "not a dict",
                None,
                {"role": "user", "content": "kept"},
            ]
        )
        self.assertEqual(out, [{"role": "user", "content": "kept"}])

    def test_a_non_list_is_no_history_at_all(self):
        for value in (None, "messages", {"role": "user"}, 7):
            self.assertEqual(analyze.clean_chat_messages(value), [])

    def test_history_is_capped_to_the_recent_turns(self):
        many = [{"role": "user", "content": f"q{i}"} for i in range(40)]
        out = analyze.clean_chat_messages(many)
        self.assertEqual(len(out), analyze.CHAT_MAX_TURNS)
        # The newest turns survive — the question is always last.
        self.assertEqual(out[-1]["content"], "q39")

    def test_an_overlong_message_is_truncated_not_dropped(self):
        out = analyze.clean_chat_messages([{"role": "user", "content": "x" * 9000}])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["content"]), analyze.CHAT_MAX_MESSAGE_CHARS)

    def test_whitespace_is_trimmed(self):
        out = analyze.clean_chat_messages([{"role": "user", "content": "  hi  "}])
        self.assertEqual(out[0]["content"], "hi")


class TestChatEndpoint(unittest.TestCase):
    """The SSE contract, with the model stubbed — no network, no key needed."""

    @classmethod
    def setUpClass(cls):
        import os
        import tempfile

        cls.data_dir = Path(tempfile.mkdtemp(prefix="capture-chat-"))
        os.environ["CAPTURE_DATA_DIR"] = str(cls.data_dir)
        os.environ["REPLAY_AUTH_DISABLED"] = "1"

        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import pgfixture

        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        os.environ["CAPTURE_DATABASE_URL"] = cls.app_url
        os.environ["CAPTURE_PROJECT_CACHE_TTL_S"] = "0"
        pgfixture.seed_default_project(cls.admin_url)
        import server

        cls.server = server
        from fastapi.testclient import TestClient

        cls.client = TestClient(server.app)

        # A cohort of two analyzed sessions, written straight to disk.
        cls.pool = server.resolve_pool(None)
        for sid, text in (("a" * 32, "S-a write-up"), ("b" * 32, "S-b write-up")):
            d = cls.pool / sid
            d.mkdir(parents=True, exist_ok=True)
            (d / analyze.ANALYSIS_FILE).write_text(
                '{"status":"done","text":' + f'"{text}"' + "}"
            )
        cls.cohort = analyze.write_cohort(
            cls.pool,
            {
                "id": "cohort-chat-test",
                "status": "done",
                "sessions": [
                    {"session_id": "a" * 32, "filter": "purchased", "status": "done"},
                    {"session_id": "b" * 32, "filter": "all", "status": "done"},
                ],
            },
        )

    @classmethod
    def tearDownClass(cls):
        import os
        import shutil

        import db

        db.close_pool()
        os.environ.pop("CAPTURE_DATABASE_URL", None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def setUp(self):
        self._enabled = analyze.ANALYSIS_ENABLED
        self._stream = analyze.stream_gemini_chat
        analyze.ANALYSIS_ENABLED = True

    def tearDown(self):
        analyze.ANALYSIS_ENABLED = self._enabled
        analyze.stream_gemini_chat = self._stream

    def ask(self, messages, chunks=("hello ", "world")):
        async def fake(rows, label, given, msgs, spec=None):
            for c in chunks:
                yield c

        analyze.stream_gemini_chat = fake
        return self.client.post(
            "/api/cohorts/cohort-chat-test/chat", json={"messages": messages}
        )

    def test_meta_comes_first_so_citations_resolve_while_text_streams(self):
        res = self.ask([{"role": "user", "content": "why?"}])
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.text.startswith("event: meta"))
        frames = res.text.split("\n\n")
        self.assertIn('"citations"', frames[0])
        self.assertIn('"n":2', frames[0])

    def test_the_answer_arrives_as_deltas_then_done(self):
        res = self.ask([{"role": "user", "content": "why?"}], chunks=("a", "b"))
        self.assertIn('event: delta\ndata: {"text":"a"}', res.text)
        self.assertIn('event: delta\ndata: {"text":"b"}', res.text)
        self.assertTrue(res.text.rstrip().endswith("event: done\ndata: {}"))

    def test_a_newline_in_the_answer_cannot_split_a_frame(self):
        res = self.ask([{"role": "user", "content": "q"}], chunks=("one\ntwo",))
        # JSON-escaped, so the data line stays one line.
        self.assertIn('data: {"text":"one\\ntwo"}', res.text)

    def test_a_mid_stream_failure_arrives_as_an_error_event(self):
        async def boom(rows, label, given, msgs, spec=None):
            raise RuntimeError("gemini exploded")
            yield  # pragma: no cover

        analyze.stream_gemini_chat = boom
        res = self.client.post(
            "/api/cohorts/cohort-chat-test/chat",
            json={"messages": [{"role": "user", "content": "q"}]},
        )
        # The status is already 200 by the time it fails, so the failure has
        # to travel in the stream.
        self.assertEqual(res.status_code, 200)
        self.assertIn('event: error', res.text)
        self.assertIn("gemini exploded", res.text)
        self.assertNotIn("event: done", res.text)

    def test_a_question_is_required(self):
        for body in ({"messages": []}, {}, {"messages": [{"role": "system", "c": 1}]}):
            res = self.client.post("/api/cohorts/cohort-chat-test/chat", json=body)
            self.assertEqual(res.status_code, 400, body)

    def test_the_last_turn_must_be_the_users(self):
        res = self.client.post(
            "/api/cohorts/cohort-chat-test/chat",
            json={"messages": [{"role": "assistant", "content": "hi"}]},
        )
        self.assertEqual(res.status_code, 400)

    def test_a_non_json_body_is_rejected(self):
        res = self.client.post(
            "/api/cohorts/cohort-chat-test/chat", content=b"not json"
        )
        self.assertEqual(res.status_code, 400)

    def test_an_unknown_cohort_is_a_404(self):
        res = self.client.post(
            "/api/cohorts/no-such-cohort/chat",
            json={"messages": [{"role": "user", "content": "q"}]},
        )
        self.assertEqual(res.status_code, 404)

    def test_an_unconfigured_server_says_so(self):
        analyze.ANALYSIS_ENABLED = False
        res = self.client.post(
            "/api/cohorts/cohort-chat-test/chat",
            json={"messages": [{"role": "user", "content": "q"}]},
        )
        self.assertEqual(res.status_code, 503)

    def test_a_filter_matching_nobody_does_not_spend_a_call(self):
        called = False

        async def fake(rows, label, given, msgs, spec=None):
            nonlocal called
            called = True
            yield "x"

        analyze.stream_gemini_chat = fake
        res = self.client.post(
            "/api/cohorts/cohort-chat-test/chat?device=desktop",
            json={"messages": [{"role": "user", "content": "q"}]},
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(called)

    def test_the_filter_narrows_the_group_the_chat_reads(self):
        seen = {}

        async def fake(rows, label, given, msgs, spec=None):
            seen["ids"] = [r["session_id"] for r in rows]
            yield "ok"

        analyze.stream_gemini_chat = fake
        res = self.client.post(
            "/api/cohorts/cohort-chat-test/chat?outcome=purchased",
            json={"messages": [{"role": "user", "content": "q"}]},
        )
        self.assertEqual(res.status_code, 200)
        # Only the purchased session reaches the model.
        self.assertEqual(seen["ids"], ["a" * 32])
        self.assertIn('"n":1', res.text)


class TestChatPrompt(unittest.TestCase):
    def test_it_separates_missing_evidence_from_absent_behaviour(self):
        """The failure this prompt exists to prevent: a write-up that does not
        mention X read as proof that X did not happen."""
        self.assertIn("absent from the EVIDENCE", analyze.CHAT_PROMPT)
        self.assertIn("absent from the SESSIONS", analyze.CHAT_PROMPT)

    def test_it_keeps_the_citation_contract_the_ui_parses(self):
        # citations.js only linkifies this exact shape.
        self.assertIn("S4 [01:09]", analyze.CHAT_PROMPT)
        self.assertIn("--- SESSIONS session N ---", analyze.CHAT_PROMPT)

    def test_it_carries_the_filter_is_not_a_finding_rule(self):
        self.assertIn("The filter is not a finding", analyze.CHAT_PROMPT)

    def test_it_declines_the_questions_write_ups_cannot_answer(self):
        for topic in ("Precise metrics", "Trends over time", "outside this group"):
            self.assertIn(topic, analyze.CHAT_PROMPT)


if __name__ == "__main__":
    unittest.main()
