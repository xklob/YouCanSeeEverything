import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from biography.db import connect, init_db
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
