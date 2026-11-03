"""Software attachments retain original bytes without importing app resources."""
import hashlib
import io
import json
import tarfile
import zipfile

import pytest

from biography.importer import ingest_path
from biography.parsers import parse_file
from biography.source_policy import application_package, package_member


def package_bytes():
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('AndroidManifest.xml', b'\x03\x00\x08\x00binary application manifest')
        archive.writestr('res/drawable/icon.png', b'\x89PNG\r\n\x1a\napplication artwork')
    return data.getvalue()


@pytest.mark.parametrize('outer',['zip','tar',None])
def test_application_attachment_is_retained_as_one_verified_file(tmp_path,outer):
    root = tmp_path / 'exports'
    root.mkdir()
    payload = package_bytes()
    source = root / ('history.' + outer if outer else 'example.APK')
    if outer == 'zip':
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('messages/attachments/example.APK', payload)
            archive.writestr('messages/thread.txt', 'A fictional message with an application attachment.')
    elif outer == 'tar':
        with tarfile.open(source, 'w') as archive:
            item = tarfile.TarInfo('messages/attachments/example.APK')
            item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))
    else:
        source.write_bytes(payload)
    report = ingest_path(source, tmp_path / 'vault', delete_source=True,
        allowed_root=root, reserve_bytes=0, discard_container=True)
    assert report.status == 'complete'
    assert not source.exists()
    retained = next(item for item in report.files if item.original_path.endswith('example.APK'))
    assert retained.path.read_bytes() == payload
    assert retained.sha256 == hashlib.sha256(payload).hexdigest()
    assert retained.disposition == 'retained_leaf'
    assert not any('AndroidManifest.xml' in item.original_path or 'icon.png' in item.original_path for item in report.files)
    parsed = parse_file(retained.path, retained.original_path, 'Synthetic archive')
    assert parsed.status == 'metadata_only'
    assert len(parsed.events) == 1
    assert parsed.events[0]['occurred_at'] is None
    ledger = [json.loads(line) for line in report.manifest_path.read_text().splitlines()]
    assert any(row['kind'] == 'source_deleted' for row in ledger)


def test_ordinary_nested_archives_still_extract_messages(tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as nested:
        nested.writestr('thread.txt', 'A fictional conversation')
    outer = tmp_path / 'history.zip'
    with zipfile.ZipFile(outer, 'w') as archive:
        archive.writestr('messages.zip', data.getvalue())
    report = ingest_path(outer, tmp_path / 'vault', reserve_bytes=0)
    assert any(item.original_path.endswith('messages.zip!thread.txt') for item in report.files)


def test_old_expanded_package_resources_remain_retained_without_model_fallback(tmp_path):
    raw = tmp_path / 'canonical'
    payload = b'\x03\x00\x08\x00binary application manifest'
    raw.write_bytes(payload)
    parsed = parse_file(raw, 'history.tar!messages.zip!files/example.apk!AndroidManifest.xml', 'Synthetic archive')
    assert parsed.status == 'retained'
    assert parsed.parser == 'policy.package_contents'
    assert not parsed.events
    assert raw.read_bytes() == payload
    assert not package_member('history.zip!photos/apk/holiday.xml')
    assert not package_member('history.zip!example.apk')
    assert not application_package('example.apk-backup.zip')
    assert application_package('attachments\\example.APK')
