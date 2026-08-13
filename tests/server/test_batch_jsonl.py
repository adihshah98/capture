"""The JSONL a Gemini Batch submission uploads must be a GenerateContentRequest.

Dumping the SDK's InlinedRequest wrapper (`config`, snake_case) is a 400, and
every cohort then falls back to sync Gemini at full price.
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture.analyze import gemini, runtime


class FakeInlinedRequest:
    def __init__(self, dumped):
        self._dumped = dumped

    def model_dump(self, mode="json", exclude_none=True, by_alias=False):
        return json.loads(json.dumps(self._dumped))


class TestBatchFileRequest(unittest.TestCase):
    def test_config_is_lifted_into_generation_config(self):
        req = FakeInlinedRequest(
            {
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "metadata": {"session_id": "s1"},
                "config": {
                    "systemInstruction": "sys",
                    "temperature": 0.3,
                    "maxOutputTokens": 4000,
                    "thinkingConfig": {"thinkingBudget": 1024},
                },
            }
        )
        key, payload = gemini._batch_file_request(req)
        self.assertEqual(key, "s1")
        self.assertNotIn("config", payload)
        self.assertNotIn("metadata", payload)
        self.assertEqual(payload["systemInstruction"], "sys")
        self.assertEqual(
            payload["generationConfig"],
            {
                "temperature": 0.3,
                "maxOutputTokens": 4000,
                "thinkingConfig": {"thinkingBudget": 1024},
            },
        )
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "hi")

    def test_sdk_only_config_fields_are_stripped(self):
        req = FakeInlinedRequest(
            {
                "contents": [],
                "metadata": {"session_id": "s1"},
                "config": {
                    "systemInstruction": "sys",
                    "httpOptions": {"timeout": 1},
                    "shouldReturnHttpResponse": True,
                    "temperature": 0.1,
                },
            }
        )
        _, payload = gemini._batch_file_request(req)
        self.assertEqual(payload["generationConfig"], {"temperature": 0.1})

    def test_missing_session_id_is_a_hard_error(self):
        req = FakeInlinedRequest({"contents": [], "config": {}})
        with self.assertRaises(ValueError):
            gemini._batch_file_request(req)


class TestSubmitPreparedGeminiBatch(unittest.TestCase):
    def setUp(self):
        import google.genai as genai

        self._client = genai.Client
        self.addCleanup(lambda: setattr(genai, "Client", self._client))
        self.genai = genai
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved_key = runtime.GEMINI_API_KEY
        runtime.GEMINI_API_KEY = "test-key"
        self.addCleanup(lambda: setattr(runtime, "GEMINI_API_KEY", self._saved_key))

    def test_uploaded_jsonl_is_a_generate_content_request(self):
        captured = {}

        class Files:
            async def upload(_self, *, file, config=None):
                captured["mime"] = getattr(config, "mime_type", None)
                captured["line"] = Path(file).read_text().splitlines()[0]
                return SimpleNamespace(name="files/in")

            async def delete(_self, *, name):
                captured.setdefault("deleted", []).append(name)

        class Batches:
            async def create(_self, *, model, src, config=None):
                return SimpleNamespace(name="batches/j")

        class Client:
            def __init__(self, **kwargs):
                self.aio = SimpleNamespace(files=Files(), batches=Batches())

        self.genai.Client = Client
        frame = Path(self.tmp.name) / "f.jpg"
        frame.write_bytes(b"jpeg")
        digest = {
            "frames": [{"atMs": 0, "file": str(frame)}],
            "warnings": [],
            "session": {},
        }
        entry = {"session_id": "session-1"}

        with patch.object(gemini, "session_metadata", return_value={"entry_url": "https://x"}):
            name = asyncio.run(
                gemini.submit_prepared_gemini_batch(
                    [(entry, digest)],
                    display_name="capture-c",
                    work_dir=Path(self.tmp.name),
                    project_id="proj",
                )
            )

        self.assertEqual(name, "batches/j")
        self.assertEqual(captured["mime"], "application/json")
        row = json.loads(captured["line"])
        self.assertEqual(row["key"], "session-1")
        request = row["request"]
        self.assertNotIn("config", request)
        self.assertNotIn("metadata", request)
        self.assertIn("systemInstruction", request)
        self.assertIn("generationConfig", request)
        self.assertIn("thinkingConfig", request["generationConfig"])
        parts = request["contents"][0]["parts"]
        inline = [p for p in parts if "inlineData" in p]
        self.assertEqual(len(inline), 1)
        self.assertEqual(inline[0]["inlineData"]["mimeType"], "image/jpeg")
        self.assertEqual(captured.get("deleted"), ["files/in"])

    def test_the_uploaded_file_is_deleted_even_when_create_fails(self):
        deleted = []

        class Files:
            async def upload(_self, *, file, config=None):
                return SimpleNamespace(name="files/in")

            async def delete(_self, *, name):
                deleted.append(name)

        class Batches:
            async def create(_self, *, model, src, config=None):
                raise RuntimeError("batch create 400")

        class Client:
            def __init__(self, **kwargs):
                self.aio = SimpleNamespace(files=Files(), batches=Batches())

        self.genai.Client = Client
        digest = {"frames": [], "warnings": [], "session": {}}
        with patch.object(gemini, "session_metadata", return_value={}):
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    gemini.submit_prepared_gemini_batch(
                        [({"session_id": "s1"}, digest)],
                        display_name="capture-c",
                        work_dir=Path(self.tmp.name),
                        project_id="proj",
                    )
                )
        self.assertEqual(deleted, ["files/in"])

    def test_project_id_cannot_be_omitted(self):
        with self.assertRaises(TypeError):
            asyncio.run(
                gemini.submit_prepared_gemini_batch(
                    [({"session_id": "s1"}, {"frames": []})],
                    display_name="capture-c",
                    work_dir=Path(self.tmp.name),
                )
            )

    def test_a_blank_project_id_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                gemini.submit_prepared_gemini_batch(
                    [({"session_id": "s1"}, {"frames": []})],
                    display_name="capture-c",
                    work_dir=Path(self.tmp.name),
                    project_id="",
                )
            )
        self.assertIn("project_id", str(ctx.exception))


class TestSessionMetadata(unittest.TestCase):
    def _meta(self, entry, order):
        with (
            patch.object(gemini.analyze_store, "connect", return_value=object()),
            patch.object(
                gemini.analyze_store, "session_facts", return_value=(entry, order)
            ),
        ):
            return gemini.session_metadata("proj", "s1")

    def test_purchase_total_is_exposed_as_order_value(self):
        meta = self._meta(
            "https://shop.test/",
            {"purchased_at": 1, "total": 59.5, "currency": "USD"},
        )
        self.assertEqual(meta["order_value"], 59.5)
        self.assertEqual(meta["currency"], "USD")
        self.assertEqual(meta["outcome"], "purchased")
        self.assertNotIn("products", meta)
        self.assertNotIn("total", meta)

    def test_checkout_without_a_total_omits_order_fields(self):
        meta = self._meta("https://shop.test/", {"checkout_started_at": 1})
        self.assertEqual(meta["outcome"], "reached checkout")
        self.assertNotIn("order_value", meta)
        self.assertNotIn("currency", meta)
        self.assertNotIn("products", meta)


if __name__ == "__main__":
    unittest.main()
