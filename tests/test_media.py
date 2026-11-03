import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from biography.db import connect, init_db
from biography import media as media_module
from biography.media import (explicit_references,index_media_files,media_file_info,
                             media_for_event,media_for_events,reconcile_media,sniff_media)

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic fixture"


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root/"catalog.sqlite3"
        self.objects = self.root/"objects"
        init_db(self.database)
        with connect(self.database) as conn:
            for source in ("source-a","source-b"):
                conn.execute("INSERT INTO sources(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                             (source,source,"2024-01-01","2024-01-01"))

    def tearDown(self):
        self.temp.cleanup()

    def image_bytes(self,format='TIFF',size=(48,32)):
        from PIL import Image
        if format=='HEIF':
            from pillow_heif import register_heif_opener
            register_heif_opener(thumbnails=False)
        data=io.BytesIO();Image.new('RGB',size,(30,90,150)).save(data,format=format)
        return data.getvalue()

    def test_tiff_data_preview_is_cached_jpeg_and_original_bytes_stay_exact(self):
        from PIL import Image
        original=self.image_bytes();physical=self.file('tiff','maps/image.data',original)
        self.event('photo',{},['tiff'],category='document');reconcile_media(self.database)
        self.assertEqual(sniff_media(physical)[0],'image/tiff')
        self.assertEqual(media_for_event('photo',self.database)[0]['mime'],'image/jpeg')
        with connect(self.database) as conn:
            before=conn.execute('SELECT name FROM sqlite_schema ORDER BY name').fetchall();conn.execute('PRAGMA query_only=ON')
            first=media_file_info('tiff',conn,objects_root=self.objects)
            self.assertEqual(conn.execute('SELECT name FROM sqlite_schema ORDER BY name').fetchall(),before)
        self.assertIsNotNone(first);self.assertEqual(first['mime'],'image/jpeg');self.assertTrue(first['derived'])
        self.assertNotEqual(first['path'],physical)
        with Image.open(first['path']) as image:self.assertEqual(image.size,(48,32))
        self.assertEqual(physical.read_bytes(),original)
        with patch.object(media_module,'_run_preview_worker',side_effect=AssertionError('Cache must not reconvert')):
            second=media_file_info('tiff',self.database,objects_root=self.objects)
        self.assertEqual(second['path'],first['path'])

    def test_heic_magic_in_data_file_decodes_to_safe_jpeg(self):
        from PIL import Image
        original=self.image_bytes('HEIF');physical=self.file('heic','maps/image.data',original)
        index_media_files(self.database);self.assertEqual(sniff_media(physical)[0],'image/heic')
        preview=media_file_info('heic',self.database,objects_root=self.objects)
        self.assertIsNotNone(preview);self.assertEqual(preview['mime'],'image/jpeg')
        with Image.open(preview['path']) as image:self.assertEqual(image.size,(48,32))
        self.assertEqual(physical.read_bytes(),original)

    def test_corrupt_tiff_is_retained_and_rejection_is_cached(self):
        original=b'II*\x00corrupt source';physical=self.file('broken','image.data',original);index_media_files(self.database)
        self.assertIsNone(media_file_info('broken',self.database,objects_root=self.objects))
        with patch.object(media_module,'_run_preview_worker',side_effect=AssertionError('Rejected bytes must not repeatedly decode')):
            self.assertIsNone(media_file_info('broken',self.database,objects_root=self.objects))
        self.assertEqual(physical.read_bytes(),original)

    def test_valid_image_over_pixel_or_input_budget_is_not_decoded_unbounded(self):
        original=self.image_bytes();physical=self.file('large','large.data',original);index_media_files(self.database)
        with patch.object(media_module,'PREVIEW_MAX_SOURCE_PIXELS',100):
            self.assertIsNone(media_file_info('large',self.database,objects_root=self.objects))
        with patch.object(media_module,'PREVIEW_MAX_SOURCE_BYTES',10),patch.object(media_module,'_run_preview_worker',side_effect=AssertionError('Input budget must reject first')):
            self.assertIsNone(media_file_info('large',self.database,objects_root=self.objects))
        self.assertEqual(physical.read_bytes(),original)

    def test_corrupt_cached_jpeg_is_regenerated_from_immutable_original(self):
        original=self.image_bytes();physical=self.file('image','image.data',original);index_media_files(self.database)
        preview=media_file_info('image',self.database,objects_root=self.objects)
        self.assertIsNotNone(preview);preview['path'].write_bytes(b'\xff\xd8\xffcorrupt cache')
        with patch.object(media_module,'_run_preview_worker',wraps=media_module._run_preview_worker) as called:
            repaired=media_file_info('image',self.database,objects_root=self.objects)
        self.assertEqual(called.call_count,1);self.assertIsNotNone(repaired)
        self.assertNotEqual(repaired['path'].read_bytes(),b'\xff\xd8\xffcorrupt cache')
        self.assertEqual(physical.read_bytes(),original)

    def test_converter_timeout_does_not_publish_partial_preview_or_change_original(self):
        import subprocess
        original=self.image_bytes();physical=self.file('image','image.data',original);index_media_files(self.database)
        def timed_out(source,destination,*args):
            destination.write_bytes(b'partial output');raise subprocess.TimeoutExpired('synthetic decoder',1)
        with patch.object(media_module,'_run_preview_worker',side_effect=timed_out):
            self.assertIsNone(media_file_info('image',self.database,objects_root=self.objects))
        self.assertEqual(list((self.root/'media-previews').rglob('*.part')),[])
        self.assertEqual(physical.read_bytes(),original)

    def test_corrupt_ready_metadata_is_regenerated_instead_of_crashing(self):
        self.file('image','image.data',self.image_bytes());index_media_files(self.database)
        preview=media_file_info('image',self.database,objects_root=self.objects)
        metadata_path=preview['path'].with_suffix('.json')
        for corrupt in ([],{'width':None},{'height':0},{'frame':'invalid'}):
            metadata=json.loads(metadata_path.read_text())
            if isinstance(corrupt,dict):metadata.update(corrupt)
            else:metadata=corrupt
            metadata_path.write_text(json.dumps(metadata))
            with patch.object(media_module,'_run_preview_worker',wraps=media_module._run_preview_worker) as called:
                self.assertIsNotNone(media_file_info('image',self.database,objects_root=self.objects))
            self.assertEqual(called.call_count,1)

    def test_signal_and_runtime_decoder_failure_are_retryable(self):
        import subprocess
        self.file('image','image.data',self.image_bytes());index_media_files(self.database)
        for returncode in (-9,3):
            with patch.object(media_module.subprocess,'run',return_value=subprocess.CompletedProcess([],returncode)):
                self.assertIsNone(media_file_info('image',self.database,objects_root=self.objects))
            self.assertEqual(list((self.root/'media-previews').rglob('*.json')),[])
        self.assertIsNotNone(media_file_info('image',self.database,objects_root=self.objects))

    def test_new_index_version_reprobes_old_unsupported_tiff_cache(self):
        original=self.image_bytes();self.file('image','image.data',original)
        with connect(self.database) as conn:
            media_module.ensure_schema(conn)
            conn.execute('INSERT INTO media_object_index VALUES(?,?,?,?)',(hashlib.sha256(original).hexdigest(),None,None,'not_previewable'))
            conn.execute('INSERT INTO media_file_state VALUES(?,?)',('image',1))
        self.assertEqual(index_media_files(self.database)['media_files'],1)
        self.assertIsNotNone(media_file_info('image',self.database,objects_root=self.objects))

    def test_read_helpers_work_on_query_only_catalog_without_creating_schema(self):
        with connect(self.database) as conn:
            before=conn.execute("SELECT name FROM sqlite_schema ORDER BY name").fetchall()
            conn.execute('PRAGMA query_only=ON')
            self.assertEqual(media_for_events(['missing'],conn),{'missing':[]})
            self.assertIsNone(media_file_info('missing',conn,objects_root=self.objects))
            self.assertEqual(conn.execute("SELECT name FROM sqlite_schema ORDER BY name").fetchall(),before)

    def file(self,identity,original,content=PNG,*,source="source-a"):
        digest = hashlib.sha256(content).hexdigest()
        physical = self.objects/digest[:2]/digest
        physical.parent.mkdir(parents=True,exist_ok=True)
        physical.write_bytes(content)
        with connect(self.database) as conn:
            conn.execute("INSERT OR IGNORE INTO objects(sha256,bytes,path,verified_at) VALUES(?,?,?,?)",
                         (digest,len(content),str(physical),"2024-01-01"))
            conn.execute("INSERT INTO files(id,source_id,original_path,object_hash,bytes,created_at) VALUES(?,?,?,?,?,?)",
                         (identity,source,original,digest,len(content),"2024-01-01"))
        return physical

    def event(self,identity,metadata,files,*,category="message",source="source-a",native=None):
        with connect(self.database) as conn:
            conn.execute("INSERT INTO events(id,source_id,native_id,title,category,metadata,created_at) VALUES(?,?,?,?,?,?,?)",
                         (identity,source,native,"Synthetic event",category,json.dumps(metadata),"2024-01-01"))
            for file_id in files:
                conn.execute("INSERT INTO evidence(event_id,file_id,locator,parser) VALUES(?,?,?,?)",
                             (identity,file_id,"fixture:1","fixture"))
            media_module.ensure_schema(conn)
            media_module.record_media_candidate(conn,identity,source,native,None,category,metadata)

    def test_meta_explicit_relative_media_resolves_and_unrelated_source_does_not(self):
        self.file("document","export.zip!messages/thread/message_1.json",b"{}")
        self.file("photo","export.zip!messages/thread/photos/picture.png")
        self.file("other","export.zip!messages/thread/photos/picture.png",PNG+b"other",source="source-b")
        self.event("message",{"photos":[{"uri":"photos/picture.png"}]},["document"])
        result = reconcile_media(self.database)
        self.assertEqual(result["resolved"],1)
        media = media_for_event("message",self.database)
        self.assertEqual(media[0]["file_id"],"photo")
        self.assertEqual(media[0]["mime"],"image/png")
        self.assertEqual(media[0]["url"],"/api/files/photo/media")

    def test_ambiguous_suffix_and_wrong_export_stay_unresolved(self):
        self.file("document","export.zip!messages/thread/message_1.json",b"{}")
        self.file("one","export.zip!messages/one/photos/shared.png")
        self.file("two","export.zip!messages/two/photos/shared.png",PNG+b"different")
        self.file("other-export","another.zip!messages/thread/photos/only.png")
        self.event("message",{"photos":[{"uri":"photos/shared.png"},{"uri":"photos/only.png"}]},["document"])
        result = reconcile_media(self.database)
        self.assertEqual(result["resolved"],0)
        refs = media_for_event("message",self.database,include_unresolved=True)
        self.assertEqual([r["status"] for r in refs],["ambiguous","unresolved"])

    def test_native_media_events_preview_their_own_retained_evidence(self):
        physical = self.file("photo","photos/synthetic.png")
        self.event("media-event",{},["photo"],category="media")
        reconcile_media(self.database)
        self.assertEqual(media_for_events(["media-event"],self.database)["media-event"][0]["file_id"],"photo")
        info = media_file_info("photo",self.database,objects_root=self.objects)
        self.assertEqual(info["path"],physical.resolve())
        self.assertIsNone(media_file_info("photo",self.database,objects_root=self.root/"other-root"))

    def test_active_content_disguised_as_image_is_never_previewable(self):
        self.file("active","photos/not-really.png",b"<svg onload='alert(1)'></svg>")
        self.event("event",{},["active"],category="media")
        reconcile_media(self.database)
        self.assertEqual(media_for_event("event",self.database),[])
        self.assertIsNone(media_file_info("active",self.database,objects_root=self.objects))

    def test_chatgpt_asset_pointer_and_twitter_export_filename(self):
        self.file("chat-doc","chat.zip!conversations.json",b"{}")
        self.file("chat-image","chat.zip!file-asset123456-image.png")
        self.event("chat",{"content_parts":[{"asset_pointer":"file-service://file-asset123456"}]},["chat-doc"])
        self.file("tweet-doc","social.zip!data/tweets.json",b"[]")
        self.file("tweet-image","social.zip!data/tweets_media/123-picture.png")
        self.event("tweet",{"extended_entities":{"media":[{"media_url_https":"https://pbs.twimg.com/media/picture.png"}]}},["tweet-doc"],category="post",native="123")
        reconcile_media(self.database)
        self.assertEqual(media_for_event("chat",self.database)[0]["file_id"],"chat-image")
        self.assertEqual(media_for_event("tweet",self.database)[0]["file_id"],"tweet-image")

    def test_chatgpt_sediment_asset_id_matches_exact_retained_stem(self):
        asset = "file_0123456789abcdef0123456789abcdef"
        self.file("document", "chat.zip!conversations.json", b"[]")
        self.file("image", f"chat.zip!{asset}.png")
        self.event("message", {"content_parts": [{"asset_pointer": f"sediment://{asset}"}]}, ["document"])
        result = reconcile_media(self.database)
        self.assertEqual(result["resolved"], 1)
        attachment = media_for_event("message", self.database)[0]
        self.assertEqual(attachment["file_id"], "image")
        self.assertEqual(attachment["match_basis"], "explicit_sediment_asset_id")

    def test_sediment_id_cannot_match_longer_similar_id_or_other_export(self):
        asset = "file_0123456789abcdef0123456789abcdef"
        self.file("document", "chat.zip!conversations.json", b"[]")
        self.file("longer-id", f"chat.zip!{asset}1.png")
        self.file("other-export", f"another.zip!{asset}.png")
        self.event("message", {"content_parts": [{"asset_pointer": f"sediment://{asset}"}]}, ["document"])
        reconcile_media(self.database)
        refs = media_for_event("message", self.database, include_unresolved=True)
        self.assertEqual(refs[0]["status"], "unresolved")
        self.assertEqual(refs[0]["match_basis"], "not_found")

    def test_sediment_conflicting_bytes_remain_ambiguous(self):
        asset = "file_0123456789abcdef0123456789abcdef"
        self.file("document", "chat.zip!conversations.json", b"[]")
        self.file("png", f"chat.zip!{asset}.png")
        self.file("jpeg", f"chat.zip!{asset}.jpg", PNG + b"different bytes")
        self.event("message", {"content_parts": [{"asset_pointer": f"sediment://{asset}"}]}, ["document"])
        reconcile_media(self.database)
        self.assertEqual(media_for_event("message", self.database, include_unresolved=True)[0]["status"], "ambiguous")

    def test_unknown_file_library_namespace_stays_explicitly_unsupported(self):
        self.file("document", "chat.zip!conversations.json", b"[]")
        self.file("image", "chat.zip!opaque-pointer.png")
        self.event("message", {"content_parts": [{"asset_pointer": "file-library:///opaque-pointer"}]}, ["document"])
        reconcile_media(self.database)
        refs = media_for_event("message", self.database, include_unresolved=True)
        self.assertEqual(refs[0]["status"], "unresolved")
        self.assertEqual(refs[0]["match_basis"], "unsupported_reference")

    def test_twitter_csv_filename_lists_attach_retained_media_to_posts(self):
        self.file("tweet-doc", "social.zip!tweets.csv", b"Date,Link,Text Content,Media File Names")
        self.file("first", "social.zip!data/tweets_media/first image.png")
        self.file("second", "social.zip!data/tweets_media/second.png", PNG + b"different")
        self.event("tweet", {"Media File Names": "first image.png; second.png"}, ["tweet-doc"], category="post")
        result = reconcile_media(self.database)
        self.assertEqual(result["resolved"], 2)
        attachments = media_for_event("tweet", self.database)
        self.assertEqual({item["file_id"] for item in attachments}, {"first", "second"})
        self.assertEqual({item["match_basis"] for item in attachments}, {"explicit_export_basename"})

    def test_filename_lists_preserve_quoted_delimiters_and_spaces(self):
        examples = [
            ("one.png; two.mp4", ["one.png", "two.mp4"]),
            ("one.png, two.mp4", ["one.png", "two.mp4"]),
            ("one.png|two.mp4", ["one.png", "two.mp4"]),
            ("one.png\ntwo.mp4", ["one.png", "two.mp4"]),
            ('"photo, edited.png"; "photo; two.png"', ["photo, edited.png", "photo; two.png"]),
            ("photo with spaces.png", ["photo with spaces.png"]),
            ("photo, edited.png", ["photo, edited.png"]),
            ('["one.png", "two.mp4"]', ["one.png", "two.mp4"]),
            (["one.png", "two.mp4", ""], ["one.png", "two.mp4"]),
        ]
        for value, expected in examples:
            with self.subTest(value=value):
                refs = explicit_references({"Media File Names": value})
                self.assertEqual([ref["reference"] for ref in refs], expected)

    def test_bare_filename_cannot_cross_export_or_choose_conflicting_bytes(self):
        self.file("document", "social.zip!tweets.csv", b"CSV")
        self.file("other-export", "other.zip!data/tweets_media/other.png")
        self.file("one", "social.zip!data/tweets_media/shared.png")
        self.file("two", "social.zip!data/other/shared.png", PNG + b"different")
        self.event("tweet", {"Media File Names": "other.png; shared.png"}, ["document"], category="post")
        reconcile_media(self.database)
        refs = media_for_event("tweet", self.database, include_unresolved=True)
        self.assertEqual([ref["status"] for ref in refs], ["unresolved", "ambiguous"])

    def test_reparsed_event_metadata_refreshes_attachments_without_new_evidence(self):
        self.file("document", "social.zip!tweets.csv", b"CSV")
        self.file("image", "social.zip!data/tweets_media/image.png")
        self.event("tweet", {}, ["document"], category="post")
        reconcile_media(self.database)
        self.assertEqual(media_for_event("tweet", self.database), [])
        with connect(self.database) as conn:
            conn.execute("UPDATE events SET metadata=? WHERE id='tweet'", (json.dumps({"Media File Names": "image.png"}),))
        self.assertEqual(reconcile_media(self.database,event_ids=['tweet'])["resolved"], 1)
        self.assertEqual(media_for_event("tweet", self.database)[0]["file_id"], "image")
        self.assertEqual(reconcile_media(self.database)["events"], 0)

    def manifest(self, identity, original, items, *, source="source-a"):
        return self.file(identity, original, json.dumps({"items": items}).encode(), source=source)

    def test_explicit_manifest_native_id_and_hash_attaches_media_to_actual_post(self):
        self.file("document", "archive.zip!posts.json", b"[]")
        self.file("image", "archive.zip!media/images/renamed.png")
        digest = hashlib.sha256(PNG).hexdigest()
        self.manifest("manifest", "archive.zip!media/manifest.json", [{"status": "downloaded", "file": "images/declared-original-name.png", "sha256": digest, "references": [{"fullname": "t3_post", "role": "post"}]}])
        self.event("post", {}, ["document"], category="post", native="t3_post")
        result = reconcile_media(self.database)
        self.assertEqual(result["manifest_references"], 1)
        attachment = media_for_event("post", self.database)[0]
        self.assertEqual(attachment["file_id"], "image")
        self.assertEqual(attachment["match_basis"], "explicit_manifest_native_id_and_hash")
        self.assertEqual(reconcile_media(self.database)["events"], 0)

    def test_manifest_unavailable_item_is_not_inferred_from_similar_filename(self):
        self.file("document", "archive.zip!posts.json", b"[]")
        self.file("image", "archive.zip!media/image.png")
        self.manifest("manifest", "archive.zip!media/manifest.json", [{"status": "unavailable", "url": "https://example.invalid/image.png", "file": "media/image.png", "references": [{"fullname": "t3_post"}]}])
        self.event("post", {}, ["document"], category="post", native="t3_post")
        reconcile_media(self.database)
        attachments = media_for_event("post", self.database, include_unresolved=True)
        self.assertEqual(attachments[0]["status"], "unresolved")
        self.assertEqual(attachments[0]["match_basis"], "manifest_unavailable")

    def test_manifest_variants_preserved_and_identical_byte_attachments_deduplicated(self):
        self.file("document", "archive.zip!posts.json", b"[]")
        self.file("one", "archive.zip!media/one.png")
        self.file("two", "archive.zip!media/two.png", PNG + b"variant")
        hashes = [hashlib.sha256(data).hexdigest() for data in (PNG, PNG, PNG + b"variant")]
        self.manifest("manifest", "archive.zip!media/manifest.json", [{"status": "downloaded", "file": f"media/{i}.png", "sha256": digest, "references": [{"native_id": "t3_post"}]} for i, digest in enumerate(hashes)])
        self.event("post", {}, ["document"], category="post", native="t3_post")
        result = reconcile_media(self.database)
        self.assertEqual(result["manifest_references"], 3)
        self.assertEqual(result["resolved"], 2)
        self.assertEqual({ref["file_id"] for ref in media_for_event("post", self.database)}, {"one", "two"})

    def test_manifest_exact_native_ids_and_export_containers_do_not_cross(self):
        self.file("document", "archive.zip!posts.json", b"[]")
        self.file("image", "archive.zip!media/image.png")
        digest = hashlib.sha256(PNG).hexdigest()
        items = [{"status": "downloaded", "file": "media/image.png", "sha256": digest, "references": [{"fullname": "t3_post"}]}]
        self.manifest("other-export", "another.zip!media/manifest.json", items)
        self.manifest("other-source", "archive.zip!media/manifest.json", items, source="source-b")
        self.manifest("wrong-id", "archive.zip!manifest.json", [{**items[0], "references": [{"fullname": "post"}]}])
        self.event("post", {}, ["document"], category="post", native="t3_post")
        reconcile_media(self.database)
        self.assertEqual(media_for_event("post", self.database, include_unresolved=True), [])

    def test_manifest_added_later_refreshes_an_existing_native_post(self):
        self.file("document", "archive.zip!posts.json", b"[]")
        self.file("image", "archive.zip!media/image.png")
        self.event("post", {}, ["document"], category="post", native="t3_post")
        reconcile_media(self.database)
        self.assertEqual(media_for_event("post", self.database), [])
        self.manifest("manifest", "archive.zip!media/manifest.json", [{"status": "downloaded", "file": "media/image.png", "sha256": hashlib.sha256(PNG).hexdigest(), "references": [{"source_native_id": "t3_post"}]}])
        self.assertEqual(reconcile_media(self.database)["resolved"], 1)
        self.assertEqual(media_for_event("post", self.database)[0]["file_id"], "image")

    def test_bare_filename_match_is_rechecked_when_a_competing_file_arrives(self):
        self.file("document", "social.zip!tweets.csv", b"CSV")
        self.file("image", "social.zip!data/tweets_media/shared.png")
        self.event("post", {"Media File Names": "shared.png"}, ["document"], category="post")
        reconcile_media(self.database)
        self.assertEqual(media_for_event("post", self.database)[0]["file_id"], "image")
        self.file("conflict", "social.zip!data/other/shared.png", PNG + b"different")
        reconcile_media(self.database)
        self.assertEqual(media_for_event("post", self.database, include_unresolved=True)[0]["status"], "ambiguous")

    def test_new_media_reconciles_prior_unresolved_reference_without_reprocessing_every_event(self):
        self.file("document","export.zip!messages/thread/message.json",b"{}")
        self.event("event",{"photos":[{"uri":"photos/new.png"}]},["document"])
        self.assertEqual(reconcile_media(self.database)["unresolved"],1)
        self.assertEqual(reconcile_media(self.database)["events"],0)
        self.file("new","export.zip!messages/thread/photos/new.png")
        self.assertEqual(reconcile_media(self.database)["resolved"],1)
        self.assertEqual(reconcile_media(self.database)["events"],0)

    def test_cross_export_evidence_with_different_attachment_bytes_is_ambiguous(self):
        self.file("doc-one","one.zip!messages/thread/message.json",b"{}")
        self.file("doc-two","two.zip!messages/thread/message.json",b"{}")
        self.file("image-one","one.zip!messages/thread/photos/picture.png")
        self.file("image-two","two.zip!messages/thread/photos/picture.png",PNG+b"variant")
        self.event("message",{"photos":[{"uri":"photos/picture.png"}]},["doc-one","doc-two"])
        reconcile_media(self.database)
        self.assertEqual(media_for_event("message",self.database,include_unresolved=True)[0]["status"],"ambiguous")

    def test_remote_arbitrary_urls_are_not_downloaded_or_filename_matched(self):
        self.file("document","export.zip!messages/thread/message.json",b"{}")
        self.file("image","export.zip!messages/thread/photos/picture.png")
        self.event("message",{"photos":[{"uri":"https://unrelated.example/picture.png"}]},["document"])
        reconcile_media(self.database)
        self.assertEqual(media_for_event("message",self.database),[])
        self.assertEqual(media_for_event("message",self.database,include_unresolved=True)[0]["match_basis"],"remote_reference")

    def test_same_export_can_resolve_across_resumed_import_attempts(self):
        self.file("document","export.zip!messages/thread/message.json",b"{}")
        self.file("image","export.zip!messages/thread/photos/picture.png")
        with connect(self.database) as conn:
            for identity in ("attempt-one","attempt-two"):
                conn.execute("INSERT INTO imports(id,source_id,status,started_at) VALUES(?,?,?,?)",
                             (identity,"source-a","complete","2024-01-01"))
            conn.execute("UPDATE files SET import_id='attempt-one' WHERE id='document'")
            conn.execute("UPDATE files SET import_id='attempt-two' WHERE id='image'")
        self.event("message",{"photos":[{"uri":"photos/picture.png"}]},["document"])
        reconcile_media(self.database)
        self.assertEqual(media_for_event("message",self.database)[0]["file_id"],"image")

    def test_competing_root_and_document_relative_paths_are_ambiguous(self):
        self.file("document","export.zip!messages/thread/message.json",b"{}")
        self.file("relative","export.zip!messages/thread/photos/picture.png")
        self.file("root-relative","export.zip!photos/picture.png",PNG+b"different")
        self.event("message",{"photos":[{"uri":"photos/picture.png"}]},["document"])
        reconcile_media(self.database)
        self.assertEqual(media_for_event("message",self.database,include_unresolved=True)[0]["status"],"ambiguous")

    def test_missing_objects_are_retryable_when_retained_bytes_return(self):
        physical = self.file("image","photos/picture.png")
        self.event("event",{},["image"],category="media")
        physical.unlink()
        self.assertEqual(reconcile_media(self.database)["resolved"],0)
        with connect(self.database) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM media_file_state").fetchone()[0],0)
        physical.write_bytes(PNG)
        reconcile_media(self.database)
        self.assertEqual(media_for_event("event",self.database)[0]["file_id"],"image")

    def test_new_export_evidence_rechecks_a_previously_exact_attachment_match(self):
        self.file("document","one.zip!messages/thread/message.json",b"{}")
        self.file("image","one.zip!messages/thread/photos/picture.png")
        self.event("event",{"photos":[{"uri":"photos/picture.png"}]},["document"])
        reconcile_media(self.database)
        self.assertEqual(media_for_event("event",self.database)[0]["file_id"],"image")
        self.file("new-document","two.zip!messages/thread/message.json",b"{}")
        self.file("new-image","two.zip!messages/thread/photos/picture.png",PNG+b"variant")
        with connect(self.database) as conn:
            conn.execute("INSERT INTO evidence(event_id,file_id,locator,parser) VALUES(?,?,?,?)",
                         ("event","new-document","fixture:1","fixture"))
        reconcile_media(self.database)
        self.assertEqual(media_for_event("event",self.database,include_unresolved=True)[0]["status"],"ambiguous")


if __name__ == "__main__":
    unittest.main()
