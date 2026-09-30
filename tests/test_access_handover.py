"""最小授权视图、物流去标识化与换院交接。"""

import unittest

from ledger import AuthorizationError
from tests.helpers import build_world, make_finished_device


class AuthorizationViewTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.proj, self.actors, self.clock = build_world()
        self.scan_id, self.device_id = make_finished_device(self.svc, self.actors)

    def test_fabrication_work_order_has_no_identity_or_scan_body(self):
        wo = self.proj.fabrication_work_order(
            self.actors["fab_admin"], self.device_id
        )
        rendered = str(wo)
        self.assertNotIn("张小明", rendered)
        self.assertNotIn("g1", rendered)
        self.assertNotIn("content_ref", rendered)
        self.assertIn("prescription_version", wo["work_order"])
        self.assertEqual(
            wo["work_order"]["scan"]["delivery"], "controlled_pull_by_checksum"
        )

    def test_other_fabrication_org_cannot_see_work_order(self):
        from ledger import Actor

        stranger = Actor("f9", "fabrication_admin", "fab9")
        with self.assertRaises(AuthorizationError):
            self.proj.fabrication_work_order(stranger, self.device_id)

    def test_clinic_without_access_cannot_view_patient(self):
        with self.assertRaises(AuthorizationError):
            self.proj.clinic_view(self.actors["doctor2"], "p1")

    def test_guardian_view_hides_production_params(self):
        gv = self.proj.guardian_view(self.actors["guardian"], "p1")
        rendered = str(gv)
        self.assertNotIn("params", rendered)
        self.assertNotIn("layer_um", rendered)
        self.assertEqual(gv["devices"][0]["device_id"], self.device_id)

    def test_wrong_guardian_rejected(self):
        from ledger import Actor

        other = Actor("g9", "guardian")
        with self.assertRaises(AuthorizationError):
            self.proj.guardian_view(other, "p1")

    def test_shipping_label_is_de_identified(self):
        label = self.proj.shipping_label(
            self.actors["fab_admin"], self.device_id
        )
        self.assertFalse(label["contains_scan_images"])
        self.assertFalse(label["contains_identity_documents"])
        self.assertNotIn("张小明", str(label))
        self.assertEqual(label["destination_org"], "clinic1")


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.proj, self.actors, self.clock = build_world()
        self.scan_id, self.device_id = make_finished_device(self.svc, self.actors)
        self.issue = self.svc.open_issue(
            self.actors["guardian"], self.device_id, "loss", "外地丢失"
        )
        self.due_at = self.issue["due_at"]

    def test_handover_carries_open_issues_with_owner_and_original_deadline(self):
        hov = self.svc.initiate_handover(
            self.actors["doctor"], "p1", "clinic2"
        )
        self.assertEqual(len(hov["open_issues"]), 1)
        carried = hov["open_issues"][0]
        self.assertEqual(carried["owner_id"], "g1")
        self.assertEqual(carried["due_at"], self.due_at)  # 期限不重置
        self.assertIn(self.device_id, hov["device_ids"])

    def test_only_home_clinic_can_initiate(self):
        with self.assertRaises(AuthorizationError):
            self.svc.initiate_handover(
                self.actors["doctor2"], "p1", "clinic2"
            )

    def test_after_acceptance_new_clinic_sees_history_and_issue_progress(self):
        hov = self.svc.initiate_handover(
            self.actors["doctor"], "p1", "clinic2"
        )
        with self.assertRaises(AuthorizationError):
            self.proj.clinic_view(self.actors["doctor2"], "p1")
        self.svc.accept_handover(
            self.actors["doctor2"], hov["handover_id"]
        )
        view = self.proj.clinic_view(self.actors["doctor2"], "p1")
        self.assertEqual(len(view["devices"]), 1)
        self.assertEqual(view["issues"][0]["issue_id"], self.issue["issue_id"])
        # 新门诊可反查完整履历
        history = self.proj.full_history(
            self.actors["doctor2"], self.device_id
        )
        self.assertEqual(history["scan"]["checksum"], "scan-hash-1")
        self.assertEqual(
            history["fabrication"][0]["material"]["batch_id"], "B001"
        )

    def test_overdue_status_survives_handover(self):
        hov = self.svc.initiate_handover(
            self.actors["doctor"], "p1", "clinic2"
        )
        self.svc.accept_handover(self.actors["doctor2"], hov["handover_id"])
        self.clock.advance(days=10)
        view = self.proj.clinic_view(self.actors["doctor2"], "p1")
        self.assertEqual(view["issues"][0]["derived_status"], "overdue")

    def test_reject_leaves_access_ungranted(self):
        hov = self.svc.initiate_handover(
            self.actors["doctor"], "p1", "clinic2"
        )
        self.svc.reject_handover(
            self.actors["doctor2"], hov["handover_id"], "材料不全"
        )
        with self.assertRaises(AuthorizationError):
            self.proj.clinic_view(self.actors["doctor2"], "p1")

    def test_handover_pack_visible_only_to_parties(self):
        from ledger import Actor

        svc, proj, actors, _ = build_world()
        _s, d = make_finished_device(svc, actors, checksum="other-hash")
        svc.register_org(actors["clinic_admin"], "clinic3", "第三家", "clinic")
        hov = svc.initiate_handover(actors["doctor"], "p1", "clinic2")
        outsider = Actor("x", "clinic_admin", "clinic3")
        with self.assertRaises(AuthorizationError):
            proj.handover_pack(outsider, hov["handover_id"])


if __name__ == "__main__":
    unittest.main()
