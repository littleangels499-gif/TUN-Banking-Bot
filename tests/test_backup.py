import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tunbank import backup as BK  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank.db import Database  # noqa: E402


class TestRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = Database(self.dir / "tunbank.db")
        self.db.migrate()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_backup_stage_and_apply_keeps_safety_copy(self):
        with self.db.tx() as c:
            L.set_state(c, "marker", "old")
            L.audit(c, 1, "TEST", None, {})
        good = self.db.backup_to(self.dir / "good.db")
        with self.db.tx() as c:
            L.set_state(c, "marker", "new")
        info = BK.stage_restore(self.dir, good)
        self.assertGreaterEqual(info["audit_entries"], 1)
        self.db.close()
        msg = BK.apply_pending_restore(self.dir, self.dir / "tunbank.db", self.dir / "backups")
        self.assertIn("Restore applied", msg)
        self.assertEqual(len(list((self.dir / "backups").glob("pre-restore-*.db"))), 1)
        db2 = Database(self.dir / "tunbank.db")
        with db2.read() as c:
            self.assertEqual(L.get_state(c, "marker"), "old")
        db2.close()
        self.assertFalse((self.dir / BK.PENDING_NAME).exists())

    def test_garbage_and_foreign_files_rejected(self):
        junk = self.dir / "junk.db"
        junk.write_bytes(b"hello" * 5000)
        with self.assertRaises(BK.BackupError):
            BK.validate_backup(junk)
        other = self.dir / "other.db"
        c = sqlite3.connect(str(other))
        c.execute("CREATE TABLE x(a)")
        c.executemany("INSERT INTO x VALUES(?)", [("y" * 200,)] * 200)
        c.commit()
        c.close()
        with self.assertRaises(BK.BackupError):
            BK.validate_backup(other)

    def test_tampered_backup_rejected(self):
        with self.db.tx() as c:
            L.audit(c, 1, "TEST", None, {})
        good = self.db.backup_to(self.dir / "g.db")
        raw = sqlite3.connect(str(good))
        raw.execute("DROP TRIGGER trg_audit_no_update")
        raw.execute("UPDATE audit_log SET action='FORGED'")
        raw.commit()
        raw.close()
        with self.assertRaises(BK.BackupError):
            BK.validate_backup(good)

    def test_bad_pending_restore_is_not_applied(self):
        (self.dir / BK.PENDING_NAME).write_bytes(b"x" * 9000)
        msg = BK.apply_pending_restore(self.dir, self.dir / "tunbank.db", self.dir / "backups")
        self.assertIn("REJECTED", msg)
        self.assertTrue((self.dir / "tunbank.db").exists())


if __name__ == "__main__":
    unittest.main()
