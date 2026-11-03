import io
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from biography.importer import ingest_path, read_manifest, ProgressCallbackError
from biography.storage import ImportPaused, IntegrityError, SourceChangedError


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "exports"
        self.source.mkdir()
        self.vault = self.root / "vault"

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, source=None, **kwargs):
        kwargs.setdefault("discard_container", True)
        return ingest_path(source or self.source, self.vault, reserve_bytes=0,
                           space_guard=lambda _: None, **kwargs)

    def test_duplicate_archive_names_hidden_files_and_empty_files_survive(self):
        archive = self.source / "export.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("same.txt", b"first")
                zipped.writestr("same.txt", b"second")
                zipped.writestr(".hidden", b"hidden")
                zipped.writestr("empty", b"")
                zipped.writestr("directory/", b"")
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertFalse(archive.exists())
        self.assertEqual({item.path.read_bytes() for item in report.files},
                         {b"first", b"second", b"hidden", b""})
        entries = [r for r in read_manifest(report.manifest_path) if r["kind"] == "archive_entry"]
        self.assertEqual([r["ordinal"] for r in entries], list(range(5)))
        self.assertEqual([r["path"] for r in entries[:2]], ["same.txt", "same.txt"])

    def test_corrupt_destination_blocks_source_deletion(self):
        path = self.source / "original.txt"
        path.write_bytes(b"keep me")
        first = self.ingest(path)
        first.files[0].path.write_bytes(b"corrupt")
        with self.assertRaises(IntegrityError):
            self.ingest(path, delete_source=True, allowed_root=self.source)
        self.assertEqual(path.read_bytes(), b"keep me")

    def test_source_mutation_during_read_blocks_deletion(self):
        path = self.source / "original.txt"
        path.write_bytes(b"original")
        changed = False
        def progress(event):
            nonlocal changed
            if event["event"] == "bytes_written" and not changed:
                changed = True
                path.write_bytes(b"modified")
        with self.assertRaises(SourceChangedError):
            self.ingest(path, delete_source=True, allowed_root=self.source,
                        progress=progress)
        self.assertEqual(path.read_bytes(), b"modified")

    def test_zip_traversal_never_writes_outside_objects_and_retains_original(self):
        archive = self.source / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("../../escaped.txt", b"evidence")
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertTrue(archive.exists())
        self.assertFalse((self.root.parent / "escaped.txt").exists())
        self.assertEqual(report.status, "needs_review")
        self.assertTrue(any(item.disposition == "opaque_container" for item in report.files))
        self.assertTrue(any(item.path.read_bytes() == b"evidence" for item in report.files))

    def test_interrupted_import_restarts_without_losing_originals_or_unique_content(self):
        for name, body in [("one", b"one"), ("duplicate", b"one"), ("two", b"two")]:
            (self.source / name).write_bytes(body)
        finished = 0
        def progress(event):
            nonlocal finished
            if event["event"] == "source_finished":
                finished += 1
                if finished == 1:
                    raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.ingest(progress=progress)
        self.assertEqual(len(list(self.source.iterdir())), 3)
        report = self.ingest(delete_source=True, allowed_root=self.source)
        self.assertEqual({item.path.read_bytes() for item in report.files}, {b"one", b"two"})
        self.assertEqual(report.counts["unique_objects"], 2)
        self.assertEqual(list(self.source.iterdir()), [])

    def test_low_space_pause_is_structured_and_source_survives(self):
        path = self.source / "important"
        path.write_bytes(b"never lose")
        calls = 0
        def guard(size):
            nonlocal calls
            calls += 1
            if calls > 2:
                raise ImportPaused("test_low_space", required_bytes=size)
        with self.assertRaises(ImportPaused) as caught:
            ingest_path(path, self.vault, space_guard=guard, delete_source=True,
                        allowed_root=self.source)
        self.assertEqual(caught.exception.reason, "test_low_space")
        self.assertTrue(path.exists())
        self.assertEqual(read_manifest(caught.exception.manifest_path)[-1]["status"], "paused")
        report = self.ingest(path)
        self.assertEqual(report.status, "complete")

    def test_tar_forward_and_chained_hardlinks_preserve_aliases_without_container_copy(self):
        archive = self.source / "links.tar"
        with tarfile.open(archive, "w") as tar:
            for name, target in [("earlier", "later"), ("later", "data")]:
                link = tarfile.TarInfo(name)
                link.type = tarfile.LNKTYPE
                link.linkname = target
                tar.addfile(link)
            info = tarfile.TarInfo("data")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"leaf"))
            link = tarfile.TarInfo("shortcut")
            link.type = tarfile.SYMTYPE
            link.linkname = "/never/follow/this"
            tar.addfile(link)
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertFalse(archive.exists())
        self.assertEqual(report.status, "complete")
        self.assertEqual(len(report.files), 3)
        self.assertEqual(len({item.sha256 for item in report.files}), 1)
        self.assertTrue(all(item.disposition == "retained_leaf" for item in report.files))
        entries = read_manifest(report.manifest_path)
        self.assertEqual(len([r for r in entries if r["kind"] == "hardlink_resolved"]), 2)
        self.assertTrue(any(r.get("disposition") == "retained_symlink_metadata" for r in entries))

    def test_unresolved_tar_hardlink_blocks_cleanup(self):
        archive = self.source / "links.tar"
        with tarfile.open(archive, "w") as tar:
            link = tarfile.TarInfo("broken")
            link.type = tarfile.LNKTYPE
            link.linkname = "missing"
            tar.addfile(link)
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertTrue(archive.exists())
        self.assertEqual(report.status, "needs_review")

    def test_duplicate_tar_target_is_ambiguous_and_cannot_authorize_cleanup(self):
        archive = self.source / "ambiguous.tar"
        with tarfile.open(archive, "w") as tar:
            for content in (b"first", b"second"):
                info = tarfile.TarInfo("duplicate")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            link = tarfile.TarInfo("alias")
            link.type = tarfile.LNKTYPE
            link.linkname = "duplicate"
            tar.addfile(link)
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertTrue(archive.exists())
        self.assertEqual(report.status, "needs_review")
        self.assertTrue({b"first", b"second"}.issubset(
            {item.path.read_bytes() for item in report.files}))

    def test_nested_attachment_zip_retains_original_bytes_and_all_children(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as zipped:
            zipped.writestr("payload.txt", b"attachment payload")
        archive = self.source / "export.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("messages/attachment.zip", nested.getvalue())
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertFalse(archive.exists())
        self.assertEqual({item.path.read_bytes() for item in report.files},
                         {nested.getvalue(), b"attachment payload"})
        leaf = next(item for item in report.files if item.disposition == "retained_leaf")
        self.assertEqual(len(leaf.archive_chain), 2)

    def test_unparsed_native_attachment_is_retained_without_forcing_outer_copy(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w", compression=zipfile.ZIP_STORED) as zipped:
            zipped.writestr("broken.txt", b"CORRUPT_ME")
        original_attachment = nested.getvalue().replace(b"CORRUPT_ME", b"corrupt_me", 1)
        archive = self.source / "outer.tar"
        with tarfile.open(archive, "w") as tar:
            sibling = tarfile.TarInfo("sibling.txt")
            sibling.size = 7
            tar.addfile(sibling, io.BytesIO(b"sibling"))
            info = tarfile.TarInfo("messages/attachment.zip")
            info.size = len(original_attachment)
            tar.addfile(info, io.BytesIO(original_attachment))
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertFalse(archive.exists())
        self.assertEqual({item.path.read_bytes() for item in report.files},
                         {b"sibling", original_attachment})
        self.assertEqual(report.status, "needs_review")
        self.assertTrue(any(r.get("disposition") == "nested_archive_retained_unparsed"
                            for r in read_manifest(report.manifest_path)))

    def test_corrupt_zip_member_still_inventories_every_central_directory_entry(self):
        archive = self.source / "corrupt.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zipped:
            zipped.writestr("first", b"CORRUPT_ME")
            zipped.writestr("second", b"keep second")
        raw = archive.read_bytes().replace(b"CORRUPT_ME", b"corrupt_me", 1)
        archive.write_bytes(raw)
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertTrue(archive.exists())
        self.assertEqual(report.status, "needs_review")
        entries = read_manifest(report.manifest_path)
        inventory = [r for r in entries if r["kind"] == "archive_entry_inventory"]
        self.assertEqual([r["path"] for r in inventory], ["first", "second"])
        self.assertTrue(any(item.path.read_bytes() == raw for item in report.files))

    def test_empty_zip_placeholder_is_a_retained_empty_leaf(self):
        placeholder = self.source / "pending.zip"
        placeholder.touch()
        report = self.ingest(placeholder)
        self.assertEqual(report.status, "complete")
        self.assertEqual(report.files[0].path.read_bytes(), b"")

    def test_native_archive_bytes_are_retained_by_default_before_source_deletion(self):
        archive = self.source / "attachment.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("payload.txt", b"original attachment content")
        original = archive.read_bytes()
        report = ingest_path(archive, self.vault, reserve_bytes=0,
                             space_guard=lambda _: None, delete_source=True,
                             allowed_root=self.source, logical_path="Drive/attachment.zip")
        self.assertFalse(archive.exists())
        self.assertTrue(any(item.path.read_bytes() == original for item in report.files))
        self.assertTrue(any(item.original_path == "Drive/attachment.zip"
                            and item.disposition == "verified_container"
                            for item in report.files))

    def test_exported_git_bytes_are_retained_and_relative_path_is_preserved(self):
        git = self.source / ".git"
        git.mkdir()
        source = git / "HEAD"
        source.write_bytes(b"ref: refs/heads/main\n")
        report = self.ingest(source, logical_path=".git/HEAD", delete_source=True,
                             allowed_root=self.source)
        self.assertFalse(source.exists())
        self.assertEqual(report.files[0].original_path, ".git/HEAD")
        self.assertEqual(report.files[0].path.read_bytes(), b"ref: refs/heads/main\n")

    def test_catalog_callback_failure_aborts_archive_before_cleanup_or_opaque_copy(self):
        archive = self.source / "export.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("payload.txt", b"original")
        def callback(event):
            if event["event"] == "object_verified":
                raise RuntimeError("catalog unavailable")
        with self.assertRaises(ProgressCallbackError):
            self.ingest(archive, progress=callback, delete_source=True,
                        allowed_root=self.source)
        self.assertTrue(archive.exists())
        manifest = next((self.vault / "manifests").iterdir())
        self.assertFalse(any(r.get("disposition") == "opaque_container"
                             for r in read_manifest(manifest)))

    def test_corrupt_archive_leaf_aborts_without_copying_archive_as_opaque(self):
        archive = self.source / "export.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("payload.txt", b"original")
        first = self.ingest(archive)
        first.files[0].path.write_bytes(b"corrupt")
        with self.assertRaises(IntegrityError):
            self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertTrue(archive.exists())
        manifests = list((self.vault / "manifests").iterdir())
        self.assertFalse(any(r.get("disposition") == "opaque_container"
                             for p in manifests for r in read_manifest(p)))

    def test_excluded_tar_hardlink_alias_cannot_bypass_exclusion(self):
        archive = self.source / "export.tar"
        with tarfile.open(archive, "w") as tar:
            info = tarfile.TarInfo("payload")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"leaf"))
            link = tarfile.TarInfo("excluded-family/alias")
            link.type = tarfile.LNKTYPE
            link.linkname = "payload"
            tar.addfile(link)
        report = self.ingest(archive, delete_source=True, allowed_root=self.source, excluded_directory_names=("excluded-family",))
        self.assertTrue(archive.exists())
        self.assertFalse(any("excluded-family" in item.original_path for item in report.files))
        self.assertEqual(report.status, "needs_review")

    def test_concatenated_tar_tail_blocks_container_deletion(self):
        parts = []
        for name in ("first", "unparsed_second"):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as tar:
                info = tarfile.TarInfo(name)
                info.size = len(name)
                tar.addfile(info, io.BytesIO(name.encode()))
            parts.append(buffer.getvalue())
        archive = self.source / "concatenated.tar"
        archive.write_bytes(b"".join(parts))
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertTrue(archive.exists())
        self.assertEqual(report.status, "needs_review")
        self.assertTrue(any(item.path.read_bytes() == b"".join(parts)
                            for item in report.files))
        self.assertTrue(any(r["kind"] == "archive_unparsed_tail"
                            for r in read_manifest(report.manifest_path)))

    def test_valid_compressed_tar_zero_padding_is_reconciled(self):
        archive = self.source / "export.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("data")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"leaf"))
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertFalse(archive.exists())
        self.assertEqual(report.status, "complete")
        self.assertEqual(report.files[0].path.read_bytes(), b"leaf")

    def test_tar_hardlink_cycle_and_traversal_target_block_deletion(self):
        archive = self.source / "links.tar"
        with tarfile.open(archive, "w") as tar:
            for name, target in [("a", "b"), ("b", "a"), ("escape", "../a")]:
                link = tarfile.TarInfo(name)
                link.type = tarfile.LNKTYPE
                link.linkname = target
                tar.addfile(link)
        report = self.ingest(archive, delete_source=True, allowed_root=self.source)
        self.assertTrue(archive.exists())
        self.assertEqual(report.status, "needs_review")
        self.assertEqual(sum(r["kind"] == "hardlink_unresolved"
                             for r in read_manifest(report.manifest_path)), 3)

    def test_cleanup_requires_root_and_rejects_paths_outside_it(self):
        path = self.root / "outside"
        path.write_bytes(b"do not delete")
        with self.assertRaises(IntegrityError):
            self.ingest(path, delete_source=True)
        with self.assertRaises(IntegrityError):
            self.ingest(path, delete_source=True, allowed_root=self.source)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
