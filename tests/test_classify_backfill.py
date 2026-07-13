# CLASSIFY-2b — offline tests for scripts/classify_backfill.py.
#
# No DB, no network. A tiny fake psycopg connection/cursor emulates an
# analysis_results table holding (id, title, claim_text, domain) and enforces
# the SAME guard the real UPDATE has: it only mutates `domain`, and only when the
# existing domain IS NULL. The tests prove:
#   (a) the UPDATE statement string targets the domain column only (no verdict);
#   (b) the IS NULL guard makes an already-labeled row a no-op;
#   (c) a classifier that raises is impossible by contract, but even a fake that
#       returns the fallback writes 기타-미분류 and never crashes the run.

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import classify_backfill as bf


class _FakeCursor:
    def __init__(self, store):
        self._store = store          # list of row dicts (the fake table)
        self._fetch = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT id, title, claim_text"):
            if "AND id >" in s:                      # dry-run paginated SELECT
                last_id, limit = params
            else:                                    # real-mode SELECT
                last_id, limit = 0, params[0]
            null_rows = [r for r in self._store
                         if r["domain"] is None and r["id"] > last_id]
            null_rows.sort(key=lambda r: r["id"])    # ORDER BY id
            self._fetch = [(r["id"], r["title"], r["claim_text"]) for r in null_rows[:limit]]
        elif s.startswith("UPDATE analysis_results SET domain"):
            # Enforce the real guard: domain-only, and only when currently NULL.
            label, rid = params
            for r in self._store:
                if r["id"] == rid and r["domain"] is None:   # AND domain IS NULL
                    r["domain"] = label
            self._fetch = None
        elif s.startswith("SELECT count(*)"):
            self._fetch = [(sum(1 for r in self._store if r["domain"] is None),)]
        else:
            raise AssertionError("unexpected SQL: %s" % s)

    def fetchall(self):
        return list(self._fetch or [])

    def fetchone(self):
        return self._fetch[0]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store):
        self._store = store
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class BackfillSafetyTests(unittest.TestCase):

    def test_update_sql_is_domain_only_with_is_null_guard(self):
        # (a) The statement names the domain column and the IS NULL guard, and
        # mentions NO verdict/scoring field.
        sql = bf.UPDATE_SQL
        self.assertEqual(
            sql,
            "UPDATE analysis_results SET domain = %s WHERE id = %s AND domain IS NULL",
        )
        self.assertIn("SET domain =", sql)
        self.assertIn("AND domain IS NULL", sql)
        for forbidden in (
            "verdict_label", "policy_alert_level", "verdict_confidence",
            "review_status", "truth_claim", "operator_review_required",
            "policy_confidence_score", "risk_level",
        ):
            self.assertNotIn(forbidden, sql)

    def test_backfill_labels_null_rows_only(self):
        store = [
            {"id": 1, "title": "전세 대출 규제", "claim_text": "c1", "domain": None},
            {"id": 2, "title": "기초생활보장 확대", "claim_text": "c2", "domain": None},
        ]
        conn = _FakeConn(store)

        def fake_classify(title, claim_text=None):
            return "realestate" if "전세" in (title or "") else "welfare"

        total = bf.run_backfill(conn, fake_classify, batch=50, max_rows=None, dry_run=False)
        self.assertEqual(total, 2)
        self.assertEqual(store[0]["domain"], "realestate")
        self.assertEqual(store[1]["domain"], "welfare")
        self.assertGreaterEqual(conn.commits, 1)

    def test_is_null_guard_no_ops_already_labeled_row(self):
        # (b) An already-labeled row must NOT be re-touched or overwritten.
        store = [
            {"id": 1, "title": "이미 분류됨", "claim_text": "c", "domain": "finance"},
            {"id": 2, "title": "복지 지원", "claim_text": "c", "domain": None},
        ]
        conn = _FakeConn(store)

        def fake_classify(title, claim_text=None):
            return "labor"   # would mislabel id=1 if the guard failed

        total = bf.run_backfill(conn, fake_classify, batch=50, max_rows=None, dry_run=False)
        self.assertEqual(total, 1)                 # only the one NULL row processed
        self.assertEqual(store[0]["domain"], "finance")  # untouched
        self.assertEqual(store[1]["domain"], "labor")

    def test_dry_run_writes_nothing(self):
        store = [{"id": 1, "title": "t", "claim_text": "c", "domain": None}]
        conn = _FakeConn(store)
        total = bf.run_backfill(conn, lambda t, c=None: "finance",
                                batch=50, max_rows=None, dry_run=True)
        self.assertEqual(total, 1)
        self.assertIsNone(store[0]["domain"])      # dry-run: no write
        self.assertEqual(conn.commits, 0)

    def test_fallback_label_is_written_not_crashed(self):
        # (c) The real classify_domain never raises; it returns 기타-미분류 on any
        # failure. A fake returning the fallback must be written cleanly.
        store = [{"id": 1, "title": "", "claim_text": None, "domain": None}]
        conn = _FakeConn(store)
        total = bf.run_backfill(conn, lambda t, c=None: "기타-미분류",
                                batch=50, max_rows=None, dry_run=False)
        self.assertEqual(total, 1)
        self.assertEqual(store[0]["domain"], "기타-미분류")

    def test_max_rows_caps_total(self):
        store = [{"id": i, "title": "t%d" % i, "claim_text": "c", "domain": None}
                 for i in range(1, 11)]
        conn = _FakeConn(store)
        total = bf.run_backfill(conn, lambda t, c=None: "finance",
                                batch=3, max_rows=4, dry_run=False)
        self.assertEqual(total, 4)
        self.assertEqual(sum(1 for r in store if r["domain"] is None), 6)


class _MiscFakeCursor:
    """DOMAIN-LABEL 2b fake: emulates BOTH targets. Enforces the misc guard —
    an UPDATE only mutates a row whose current domain == 기타-미분류."""

    def __init__(self, store):
        self._store = store
        self._fetch = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT id, title, claim_text"):
            if "domain = '기타-미분류'" in s:
                last_id, limit = params
                rows = [r for r in self._store
                        if r["domain"] == "기타-미분류" and r["id"] > last_id]
            elif "AND id >" in s:
                last_id, limit = params
                rows = [r for r in self._store
                        if r["domain"] is None and r["id"] > last_id]
            else:
                limit = params[0]
                rows = [r for r in self._store if r["domain"] is None]
            rows.sort(key=lambda r: r["id"])
            self._fetch = [(r["id"], r["title"], r["claim_text"])
                           for r in rows[:limit]]
        elif s.startswith("UPDATE analysis_results SET domain"):
            label, rid = params
            for r in self._store:
                if r["id"] != rid:
                    continue
                if "domain = '기타-미분류'" in s:
                    if r["domain"] == "기타-미분류":   # the misc guard
                        r["domain"] = label
                elif r["domain"] is None:              # the NULL guard
                    r["domain"] = label
            self._fetch = None
        elif s.startswith("SELECT count(*)"):
            if "기타-미분류" in s:
                self._fetch = [(sum(1 for r in self._store
                                    if r["domain"] == "기타-미분류"),)]
            else:
                self._fetch = [(sum(1 for r in self._store
                                    if r["domain"] is None),)]
        else:
            raise AssertionError("unexpected SQL: %s" % s)

    def fetchall(self):
        return list(self._fetch or [])

    def fetchone(self):
        return self._fetch[0]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _MiscFakeConn(_FakeConn):
    def cursor(self):
        return _MiscFakeCursor(self._store)


class ReclassifyMiscTests(unittest.TestCase):
    """DOMAIN-LABEL 2b — the additive 기타-미분류 re-classify target."""

    def test_misc_update_sql_is_domain_only_with_misc_guard(self):
        sql = bf.UPDATE_MISC_SQL
        self.assertIn("SET domain =", sql)
        self.assertIn("AND domain = '기타-미분류'", sql)
        for forbidden in (
            "verdict_label", "policy_alert_level", "verdict_confidence",
            "review_status", "truth_claim", "operator_review_required",
            "policy_confidence_score", "risk_level",
        ):
            self.assertNotIn(forbidden, sql)

    @staticmethod
    def _classify(title, claim_text=None):
        if "입시" in (title or "") or "교육" in (title or ""):
            return "education"
        if "복지" in (title or ""):
            return "welfare"
        return "기타-미분류"

    def _store(self):
        return [
            {"id": 1, "title": "전세 대책", "claim_text": "c", "domain": "realestate"},
            {"id": 2, "title": "대학입시 개편", "claim_text": "c", "domain": "기타-미분류"},
            {"id": 3, "title": "복지 지원 확대", "claim_text": "c", "domain": "기타-미분류"},
            {"id": 4, "title": "동네 축제 소식", "claim_text": "c", "domain": "기타-미분류"},
            {"id": 5, "title": "라벨 없음", "claim_text": "c", "domain": None},
        ]

    def test_misc_run_moves_labels_never_touches_real_or_null(self):
        store = self._store()
        conn = _MiscFakeConn(store)
        total = bf.run_backfill(conn, self._classify, batch=2, max_rows=None,
                                dry_run=False, reclassify_misc=True)
        self.assertEqual(total, 3)                       # only the misc pool
        self.assertEqual(store[0]["domain"], "realestate")  # real label untouched
        self.assertEqual(store[1]["domain"], "education")
        self.assertEqual(store[2]["domain"], "welfare")
        self.assertEqual(store[3]["domain"], "기타-미분류")  # stays, no crash
        self.assertIsNone(store[4]["domain"])            # NULL pool untouched

    def test_misc_rerun_is_idempotent_and_terminates(self):
        store = self._store()
        conn = _MiscFakeConn(store)
        bf.run_backfill(conn, self._classify, batch=2, max_rows=None,
                        dry_run=False, reclassify_misc=True)
        # Second run: only the one still-misc row is re-processed; the keyset
        # cursor passes it and the run terminates (no infinite refetch).
        total2 = bf.run_backfill(conn, self._classify, batch=2, max_rows=None,
                                 dry_run=False, reclassify_misc=True)
        self.assertEqual(total2, 1)
        self.assertEqual(store[3]["domain"], "기타-미분류")

    def test_misc_dry_run_writes_nothing(self):
        store = self._store()
        conn = _MiscFakeConn(store)
        total = bf.run_backfill(conn, self._classify, batch=50, max_rows=None,
                                dry_run=True, reclassify_misc=True)
        self.assertEqual(total, 3)
        self.assertEqual([r["domain"] for r in store],
                         ["realestate", "기타-미분류", "기타-미분류",
                          "기타-미분류", None])
        self.assertEqual(conn.commits, 0)

    def test_null_path_unchanged_by_flag_default(self):
        # The original NULL-target behavior with the default flag value.
        store = [{"id": 1, "title": "복지 지원", "claim_text": "c", "domain": None}]
        conn = _MiscFakeConn(store)
        total = bf.run_backfill(conn, self._classify, batch=50, max_rows=None,
                                dry_run=False)
        self.assertEqual(total, 1)
        self.assertEqual(store[0]["domain"], "welfare")


if __name__ == "__main__":
    unittest.main()
