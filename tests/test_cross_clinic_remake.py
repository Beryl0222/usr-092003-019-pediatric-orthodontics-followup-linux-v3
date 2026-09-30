"""跨院丢失场景端到端：照片不可照旧、终止旧件、按原扫描重制、履历可反查。"""

import unittest

from ledger import Conflict
from tests.helpers import build_world, make_finished_device


class CrossClinicRemakeTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.proj, self.actors, self.clock = build_world()
        self.scan_id, self.old_id = make_finished_device(self.svc, self.actors)

    def _move_to_new_clinic(self):
        issue = self.svc.open_issue(
            self.actors["guardian"], self.old_id, "loss", "外地游玩时丢失"
        )
        hov = self.svc.initiate_handover(
            self.actors["doctor"], "p1", "clinic2"
        )
        self.svc.accept_handover(self.actors["doctor2"], hov["handover_id"])
        return issue

    def test_photo_alone_cannot_reproduce_appliance(self):
        self._move_to_new_clinic()
        photo = self.svc.upload_scan(
            self.actors["doctor2"], "phone-photo", "sha256",
            patient_id="p1", kind="photo",
        )
        # 家长手机照片：系统能比对，但不允许照此重做
        verify = self.svc.verify_scan_for_device(
            self.actors["doctor2"], self.old_id, "phone-photo", "sha256"
        )
        self.assertFalse(verify["matched"])
        with self.assertRaises(Exception):
            self.svc.create_device(
                self.actors["doctor2"], "p1", photo["scan"]["scan_id"]
            )

    def test_terminate_then_remake_from_original_scan_keeps_lineage(self):
        issue = self._move_to_new_clinic()
        # 原门诊对旧件作成品后终止（医生+门诊负责人双签，责任留痕）
        chg = self.svc.propose_prescription_change(
            self.actors["doctor"], self.old_id, {"v": 1}, "外地丢失，终止旧件"
        )
        self.svc.decide_prescription_change(
            self.actors["doctor"], chg["change_id"], "terminate",
            [
                {"approver_id": "d1", "role": "prescribing_doctor",
                 "org_id": "clinic1"},
                {"approver_id": "a1", "role": "clinic_admin",
                 "org_id": "clinic1"},
            ],
            "器械确认丢失，防止旧件被误交付",
        )
        # 未终止时不能重制
        # 新门诊按原扫描显式重制，谱系挂 remade_from；同校验值不产生重复扫描
        new_dev = self.svc.create_device(
            self.actors["doctor2"], "p1", self.scan_id,
            remade_from=self.old_id,
        )
        self.assertEqual(new_dev["remade_from"], self.old_id)
        self.assertEqual(new_dev["clinic_id"], "clinic2")

        # 重复扫描仍然只有一件在役器械
        with self.assertRaises(Conflict):
            self.svc.create_device(
                self.actors["doctor2"], "p1", self.scan_id
            )

        # 旧件异常带着原责任人与截止时间完成结案
        self.svc.resolve_issue(
            self.actors["doctor2"], issue["issue_id"],
            "旧件已终止失效，新件已重制排产",
        )

        # 任一器械最终都能反查全部关键要素
        old_history = self.proj.full_history(
            self.actors["doctor2"], self.old_id
        )
        new_history = self.proj.full_history(
            self.actors["doctor2"], new_dev["device_id"]
        )
        self.assertEqual(old_history["scan"]["scan_id"],
                         new_history["scan"]["scan_id"])
        self.assertEqual(old_history["stage"], "terminated")
        self.assertTrue(
            any(n["node"] == "device_terminated" for n in old_history["timeline"])
        )

    def test_remake_requires_terminated_predecessor(self):
        self._move_to_new_clinic()
        with self.assertRaises(Conflict):
            self.svc.create_device(
                self.actors["doctor2"], "p1", self.scan_id,
                remade_from=self.old_id,
            )


if __name__ == "__main__":
    unittest.main()
