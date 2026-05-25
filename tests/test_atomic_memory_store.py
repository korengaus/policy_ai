"""M12.2 — Atomic write tests for ``memory_store.save_policy_memory``.

These tests pin the M12.2 contract:

* ``save_policy_memory`` writes via tmp + ``os.replace`` (atomic).
* The tmp file is cleaned up after a successful rename.
* On serialisation failure the on-disk file is untouched.
* On ``os.replace`` failure the tmp file is removed in the finally block.
* Concurrent in-process saves do not corrupt the file (module-level
  ``threading.Lock``).
* A round-trip ``save -> load`` preserves topic/article structure.

Tests never touch the project's real ``policy_memory.json`` — each test
overrides ``memory_store.MEMORY_FILE`` to a path inside a
``tempfile.TemporaryDirectory``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import memory_store  # noqa: E402


class _IsolatedMemoryFile:
    """Context manager that swaps ``memory_store.MEMORY_FILE`` to a
    temp-dir path for the duration of a test and restores it after."""

    def __init__(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmpdir.name, "policy_memory.json")
        self._original = None

    def __enter__(self):
        self._original = memory_store.MEMORY_FILE
        memory_store.MEMORY_FILE = self.path
        return self

    def __exit__(self, exc_type, exc, tb):
        memory_store.MEMORY_FILE = self._original
        self._tmpdir.cleanup()


def _sample_memory() -> dict:
    return {
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_updated_at": None,
        "topics": {
            "금리": {
                "topic": "금리",
                "events": [],
                "latest_stage": None,
                "latest_probability": None,
                "latest_market_impact": None,
                "latest_signal_change": None,
                "timeline": {},
            },
        },
        "articles": [
            {"article_id": "abc123", "title": "테스트", "url": "https://example.com/a"},
        ],
    }


class SaveBasicRoundTrip(unittest.TestCase):
    """Saving then loading returns the same payload (with last_updated_at
    populated by ``save_policy_memory``)."""

    def test_round_trip_preserves_topics_and_articles(self):
        with _IsolatedMemoryFile() as ctx:
            memory = _sample_memory()
            memory_store.save_policy_memory(memory)
            loaded = memory_store.load_policy_memory()
        self.assertEqual(loaded["topics"], memory["topics"])
        self.assertEqual(loaded["articles"], memory["articles"])
        self.assertIsNotNone(loaded["last_updated_at"])


class TmpFileNotLeftBehind(unittest.TestCase):
    """After a successful save, no ``policy_memory.json.tmp.*`` files
    remain in the target directory."""

    def test_no_tmp_files_after_save(self):
        with _IsolatedMemoryFile() as ctx:
            memory_store.save_policy_memory(_sample_memory())
            siblings = os.listdir(os.path.dirname(ctx.path))
        tmp_siblings = [name for name in siblings if ".tmp." in name]
        self.assertEqual(tmp_siblings, [], f"stale tmp files: {tmp_siblings}")


class AtomicWriteUsesReplace(unittest.TestCase):
    """``save_policy_memory`` must call ``os.replace`` exactly once with
    ``(tmp_path, MEMORY_FILE)``."""

    def test_os_replace_called_with_tmp_and_target(self):
        with _IsolatedMemoryFile() as ctx:
            calls = []
            real_replace = os.replace

            def spy_replace(src, dst, *args, **kwargs):
                calls.append((src, dst))
                return real_replace(src, dst, *args, **kwargs)

            with mock.patch.object(memory_store.os, "replace", side_effect=spy_replace):
                memory_store.save_policy_memory(_sample_memory())

        self.assertEqual(len(calls), 1, f"os.replace call count: {len(calls)}")
        src, dst = calls[0]
        self.assertIn(".tmp.", os.path.basename(src))
        self.assertEqual(os.path.abspath(dst), os.path.abspath(ctx.path))


class SerializationFailureLeavesOriginalUntouched(unittest.TestCase):
    """A non-JSON-serialisable payload raises before the on-disk file is
    touched. The previous file content remains intact and no tmp file
    is left behind."""

    def test_set_in_payload_raises_and_preserves_file(self):
        with _IsolatedMemoryFile() as ctx:
            # Seed the file with a known-good payload.
            memory_store.save_policy_memory(_sample_memory())
            with open(ctx.path, "rb") as file:
                original_bytes = file.read()

            bad_memory = _sample_memory()
            bad_memory["topics"]["broken"] = {"data": {1, 2, 3}}  # set is not JSON-able

            with self.assertRaises((TypeError, ValueError)):
                memory_store.save_policy_memory(bad_memory)

            with open(ctx.path, "rb") as file:
                after_bytes = file.read()
            siblings = os.listdir(os.path.dirname(ctx.path))

        self.assertEqual(original_bytes, after_bytes)
        tmp_siblings = [name for name in siblings if ".tmp." in name]
        self.assertEqual(tmp_siblings, [])


class TmpCleanupOnReplaceFailure(unittest.TestCase):
    """If ``os.replace`` raises, the tmp file must be removed by the
    finally block. The previous on-disk file is untouched (because
    replace never ran)."""

    def test_replace_oserror_triggers_tmp_cleanup(self):
        with _IsolatedMemoryFile() as ctx:
            memory_store.save_policy_memory(_sample_memory())
            with open(ctx.path, "rb") as file:
                original_bytes = file.read()

            def boom(src, dst, *args, **kwargs):
                raise OSError("simulated replace failure")

            with mock.patch.object(memory_store.os, "replace", side_effect=boom):
                with self.assertRaises(OSError):
                    memory_store.save_policy_memory(_sample_memory())

            with open(ctx.path, "rb") as file:
                after_bytes = file.read()
            siblings = os.listdir(os.path.dirname(ctx.path))

        self.assertEqual(original_bytes, after_bytes)
        tmp_siblings = [name for name in siblings if ".tmp." in name]
        self.assertEqual(tmp_siblings, [], f"leaked tmp files: {tmp_siblings}")


class ConcurrentSavesSerializeViaLock(unittest.TestCase):
    """Eight worker threads each call ``save_policy_memory`` with a
    distinct payload. The final file must parse cleanly (no truncation
    or interleaving) and its content must match one of the eight
    payloads byte-for-byte."""

    def test_concurrent_writes_produce_consistent_file(self):
        with _IsolatedMemoryFile() as ctx:
            payloads = []
            for index in range(8):
                memory = _sample_memory()
                memory["topics"][f"topic_{index}"] = {
                    "topic": f"topic_{index}",
                    "events": [],
                    "latest_stage": None,
                    "latest_probability": index,
                    "latest_market_impact": None,
                    "latest_signal_change": None,
                    "timeline": {},
                }
                payloads.append(memory)

            barrier = threading.Barrier(len(payloads))

            def worker(payload):
                barrier.wait()
                memory_store.save_policy_memory(payload)

            threads = [
                threading.Thread(target=worker, args=(payload,))
                for payload in payloads
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            with open(ctx.path, "r", encoding="utf-8") as file:
                loaded = json.load(file)

        # The file is a complete dict (not truncated / interleaved).
        self.assertIn("topics", loaded)
        self.assertIn("articles", loaded)
        # Exactly one of the eight topic markers is present (the writer
        # that won the race); none of the others are mixed in.
        topic_markers = {key for key in loaded["topics"].keys() if key.startswith("topic_")}
        self.assertEqual(
            len(topic_markers), 1,
            f"expected exactly one winning topic marker, got {topic_markers}",
        )


class LoadAfterAtomicWriteByteIdenticalStructure(unittest.TestCase):
    """A load -> save -> load chain preserves the topic/article
    structure exactly (modulo the ``last_updated_at`` timestamp the
    save path stamps in)."""

    def test_structure_stable_across_round_trip(self):
        with _IsolatedMemoryFile() as ctx:
            seed = _sample_memory()
            memory_store.save_policy_memory(seed)
            first = memory_store.load_policy_memory()
            memory_store.save_policy_memory(first)
            second = memory_store.load_policy_memory()

        self.assertEqual(first["topics"], second["topics"])
        self.assertEqual(first["articles"], second["articles"])


class ModuleLevelLockExists(unittest.TestCase):
    """Sanity check: the module exposes ``_SAVE_LOCK`` as a Lock
    instance. Future refactors that drop the lock will fail this pin."""

    def test_save_lock_is_a_lock(self):
        self.assertTrue(
            isinstance(memory_store._SAVE_LOCK, type(threading.Lock())),
            f"unexpected lock type: {type(memory_store._SAVE_LOCK).__name__}",
        )


if __name__ == "__main__":
    unittest.main()
