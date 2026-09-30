"""扫描去重与姓名转写差异的人工确认规则。"""

import unittest

from ledger import Conflict, IdentityMismatch
from ledger.service import SCAN_KIND_PHOTO
from tests.helpers import build_world


class ScanDedupTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.proj, self.actors, self.clock = build_world()

    def test_same_checksum_returns_same_scan_and_does_not_create_second_device(self):
        first = self.svc.upload_scan(
            self.actors["doctor"], "hash-xyz", "sha256", patient_id="p1"
        )
        second = self.svc.upload_scan(
            self.actors["doctor"], "hash-xyz", "sha256", patient_id="p1"
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["scan"]["scan_id"], second["scan"]["scan_id"])
        dev = self.svc.create_device(
            self.actors["doctor"], "p1", first["scan"]["scan_id"]
        )
        # 在役期间同扫描不能建第二件
        with self.assertRaises(Conflict):
            self.svc.create_device(
                self.actors["doctor"], "p1", first["scan"]["scan_id"]
            )
        self.assertEqual(
            self.svc.scan_devices[first["scan"]["scan_id"]], [dev["device_id"]]
        )

    def test_content_ref_never_echoed_in_responses(self):
        up = self.svc.upload_scan(
            self.actors["doctor"], "h1", "sha256",
            patient_id="p1", content_ref="s3://secret/bucket/key.stl",
        )
        self.assertNotIn("content_ref", up["scan"])
        dup = self.svc.upload_scan(
            self.actors["doctor"], "h1", "sha256", patient_id="p1"
        )
        self.assertNotIn("content_ref", dup["scan"])

    def test_photo_cannot_anchor_device(self):
        up = self.svc.upload_scan(
            self.actors["doctor"], "photo-hash", "sha256",
            patient_id="p1", kind=SCAN_KIND_PHOTO,
        )
        with self.assertRaises(Exception) as ctx:
            self.svc.create_device(
                self.actors["doctor"], "p1", up["scan"]["scan_id"]
            )
        # 照片即便身份确认也不能作为制作依据
        self.assertIn(ctx.exception.__class__.__name__,
                      {"ValidationError", "IdentityMismatch"})

    def test_checksum_verification_detects_non_matching_file(self):
        from tests.helpers import make_finished_device

        _scan_id, device_id = make_finished_device(self.svc, self.actors)
        same = self.svc.verify_scan_for_device(
            self.actors["doctor"], device_id, "scan-hash-1", "sha256"
        )
        self.assertTrue(same["matched"])
        diff = self.svc.verify_scan_for_device(
            self.actors["doctor"], device_id, "other-file", "sha256"
        )
        self.assertFalse(diff["matched"])
        self.assertIn("不得照旧重做", diff["notice"])


class NameTranscriptionTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.proj, self.actors, self.clock = build_world()

    def test_name_discrepancy_is_queued_and_blocks_device_until_confirmed(self):
        up = self.svc.upload_scan(
            self.actors["doctor"], "h2", "sha256",
            patient_id="p1", name_on_scan="ZHANG Xiaoming",
        )
        self.assertEqual(up["status"], "awaiting_identity")
        with self.assertRaises(IdentityMismatch):
            self.svc.create_device(
                self.actors["doctor"], "p1", up["scan"]["scan_id"]
            )
        pending = self.svc.pending_name_confirmations(self.actors["doctor"])
        self.assertEqual([p["alias"] for p in pending], ["ZHANG Xiaoming"])

        approved = self.svc.resolve_name_alias(
            self.actors["doctor"], "p1", "ZHANG Xiaoming", True
        )
        self.assertEqual(approved["status"], "confirmed")
        self.svc.confirm_scan_identity(
            self.actors["doctor"], up["scan"]["scan_id"], "p1"
        )
        # 确认后可以建件
        dev = self.svc.create_device(
            self.actors["doctor"], "p1", up["scan"]["scan_id"]
        )
        self.assertTrue(dev["device_id"])

    def test_rejected_alias_still_blocks_creation(self):
        up = self.svc.upload_scan(
            self.actors["doctor"], "h3", "sha256",
            patient_id="p1", name_on_scan="LI Xiaoming",
        )
        self.svc.resolve_name_alias(
            self.actors["doctor"], "p1", "LI Xiaoming", False
        )
        with self.assertRaises(IdentityMismatch):
            self.svc.create_device(
                self.actors["doctor"], "p1", up["scan"]["scan_id"]
            )

    def test_duplicate_scan_across_different_identities_is_quarantined(self):
        self.svc.upload_scan(
            self.actors["doctor"], "shared", "sha256", patient_id="p1"
        )
        self.svc.register_patient(
            self.actors["clinic_admin"], "p2", "李华", "g2", "李母", "clinic1"
        )
        with self.assertRaises(IdentityMismatch) as ctx:
            self.svc.upload_scan(
                self.actors["doctor"], "shared", "sha256",
                patient_id="p2", name_on_scan="李华",
            )
        self.assertEqual(ctx.exception.details["existing_patient_id"], "p1")
        # 没有任何第二份扫描入库
        self.assertEqual(
            len([s for s in self.svc.scans.values() if s["checksum"] == "shared"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
