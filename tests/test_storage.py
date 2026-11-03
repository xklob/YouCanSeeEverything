import io
import errno
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from biography.storage import ImportPaused, IntegrityError, ObjectStore, SpaceGuard


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ObjectStore(self.root / "vault", reserve_bytes=0,
                                 space_guard=lambda _: None)

    def tearDown(self):
        self.temp.cleanup()

    def test_object_is_read_back_and_duplicate_does_not_overwrite_corruption(self):
        first = self.store.put_stream(io.BytesIO(b"original"), expected_size=8)
        self.assertEqual(first.path.read_bytes(), b"original")
        second = self.store.put_stream(io.BytesIO(b"original"), expected_size=8)
        self.assertTrue(second.deduplicated)
        first.path.write_bytes(b"corrupt!")
        with self.assertRaises(IntegrityError):
            self.store.put_stream(io.BytesIO(b"original"), expected_size=8)
        self.assertEqual(first.path.read_bytes(), b"corrupt!")

    def test_length_failure_never_publishes_object(self):
        with self.assertRaises(IntegrityError):
            self.store.put_stream(io.BytesIO(b"short"), expected_size=10)
        self.assertEqual(list(self.store.objects.glob("*/*")), [])
        self.assertEqual(list((self.store.root / "tmp").glob("*.part")), [])

    def test_guard_checks_declared_member_then_each_chunk(self):
        checks = []
        store = ObjectStore(self.root / "vault", space_guard=checks.append)
        store.put_stream(io.BytesIO(b"data"), expected_size=4)
        self.assertEqual(checks, [4, 4])

    def test_windows_backing_disk_is_an_independent_guard(self):
        windows = self.root / "windows"
        windows.mkdir()
        vault = self.root / "vault"
        usage = shutil._ntuple_diskusage
        def disk_usage(path):
            return usage(1000, 950, 50) if Path(path) == windows else usage(1000, 0, 1000)
        with patch("biography.storage.shutil.disk_usage", side_effect=disk_usage):
            with self.assertRaises(ImportPaused) as caught:
                SpaceGuard(vault, reserve_bytes=40, windows_root=windows)(11)
        self.assertEqual(caught.exception.to_dict()["disk"], "windows_c_backing_disk")

    def test_unexpected_disk_full_is_a_structured_pause(self):
        class FullDiskStream:
            def read(self, size):
                raise OSError(errno.ENOSPC, "Synthetic full filesystem")
        with self.assertRaises(ImportPaused) as caught:
            self.store.put_stream(FullDiskStream(), expected_size=4)
        self.assertEqual(caught.exception.reason, "filesystem_full")
        self.assertEqual(list((self.store.root / "tmp").glob("*.part")), [])

    def test_host_and_ext4_can_have_distinct_reserves(self):
        windows = self.root / "windows"
        windows.mkdir()
        usage = shutil._ntuple_diskusage
        with patch("biography.storage.shutil.disk_usage", return_value=usage(1000, 925, 75)):
            SpaceGuard(self.root, reserve_bytes=20, windows_root=windows,
                       windows_reserve_bytes=50)(25)
            with self.assertRaises(ImportPaused) as caught:
                SpaceGuard(self.root, reserve_bytes=20, windows_root=windows,
                           windows_reserve_bytes=50)(26)
        self.assertEqual(caught.exception.details["reserve_bytes"], 50)


if __name__ == "__main__":
    unittest.main()
