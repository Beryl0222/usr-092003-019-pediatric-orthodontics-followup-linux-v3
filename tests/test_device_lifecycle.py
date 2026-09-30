"""器械履历完整性与处方变更裁决责任。"""

import unittest

from ledger import AuthorizationError, Conflict, ValidationError
from tests.helpers import build_world, deliver_device, make_finished_device

DOCTOR_APPROVAL = {"approver_id": "d1", "role": "prescribing_doctor",
                   "org_id": "clinic1"}
FAB_APPROVAL = {"approver_id": "f1", "role": "fabrication_admin",
                "org_id": "fab1"}
ADMIN_APPROVAL = {"approver_id": "a1", "role": "clinic_admin",
                  "org_id": "clinic1"}


class DeviceHistoryTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.proj, self.actors, self.clock = build_world()
        self.scan_id, self.device_id = make_finished_device(self.svc, self.actors)
        deliver_device(self.svc, self.actors, self.device_id)

    def test_full_history_references_scan_prescription_material_people_fitting_guardian(self):
        h = self.proj.full_history(self.actors["doctor"], self.device_id)
        self.assertEqual(h["scan"]["checksum"], "scan-hash-1")
        self.assertEqual(h["prescription"]["current_version"], 1)
        self.assertEqual(len(h["fabrication"]), 1)
        self.assertEqual(h["fabrication"][0]["material"]["batch_id"], "B001")
        self.assertEqual(h["fabrication"][0]["reviews"][0]["reviewer_id"], "t1")
        self.assertEqual(h["fitting"]["conclusion"], "accepted")
        self.assertEqual(h["fitting"]["doctor_id"], "d1")
        self.assertTrue(h["guardian_confirmed"])
        personnel = h["fabrication"][0]["personnel"]
        self.assertEqual(personnel["scheduled_by"], "d1")
        self.assertEqual(personnel["material_by"], "f1")
        self.assertEqual(personnel["started_by"], "t1")
        self.assertEqual(personnel["finished_by"], "f1")
        nodes = [n["node"] for n in h["timeline"]]
        self.assertIn("material", nodes)
        self.assertIn("tech_review", nodes)
        self.assertIn("fitting", nodes)
        self.assertIn("guardian_confirmation", nodes)

    def test_events_are_append_only(self):
        before = self.svc.store.seq
        self.svc.open_issue(
            self.actors["guardian"], self.device_id, "loss", "丢失"
        )
        self.assertEqual(self.svc.store.seq, before + 1)
        # 历史事件载荷不可变：首件材料批次仍可反查为 B001
        h = self.proj.full_history(self.actors["doctor"], self.device_id)
        self.assertIn("B001", str(h["fabrication"]))

    def test_cannot_skip_tech_review_before_finish(self):
        up = self.svc.upload_scan(
            self.actors["doctor"], "h-x", "sha256", patient_id="p1"
        )
        dev = self.svc.create_device(
            self.actors["doctor"], "p1", up["scan"]["scan_id"]
        )
        self.svc.record_prescription(self.actors["doctor"], dev["device_id"], {"v": 1})
        self.svc.schedule_production(
            self.actors["doctor"], dev["device_id"], "fab1", {}
        )
        self.svc.assign_material(
            self.actors["fab_admin"], dev["device_id"], "B9", "m", "s", "l"
        )
        self.svc.start_production(self.actors["technician"], dev["device_id"])
        with self.assertRaises(ValidationError):
            self.svc.finish_production(self.actors["fab_admin"], dev["device_id"])

    def test_delivery_requires_passed_fitting_and_guardian_is_separate(self):
        up = self.svc.upload_scan(
            self.actors["doctor"], "h-y", "sha256", patient_id="p1"
        )
        dev = self.svc.create_device(
            self.actors["doctor"], "p1", up["scan"]["scan_id"]
        )
        with self.assertRaises(Conflict):  # 未试戴不能交付
            self.svc.deliver(self.actors["doctor"], dev["device_id"], "说明")
        with self.assertRaises(Conflict):  # 未交付监护人不能确认
            self.svc.guardian_confirm(self.actors["guardian"], dev["device_id"])


class PrescriptionChangeTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.proj, self.actors, self.clock = build_world()

    def test_change_before_schedule_creates_new_version_without_approval_workflow(self):
        up = self.svc.upload_scan(
            self.actors["doctor"], "h0", "sha256", patient_id="p1"
        )
        dev = self.svc.create_device(
            self.actors["doctor"], "p1", up["scan"]["scan_id"]
        )
        self.svc.record_prescription(self.actors["doctor"], dev["device_id"], {"v": 1})
        chg = self.svc.propose_prescription_change(
            self.actors["doctor"], dev["device_id"], {"v": 2}, "排产前调整"
        )
        decided = self.svc.decide_prescription_change(
            self.actors["doctor"], chg["change_id"], "rework", [DOCTOR_APPROVAL]
        )
        self.assertEqual(decided["change"]["status"], "decided")
        self.assertEqual(
            self.svc.devices[dev["device_id"]]["current_prescription_version"], 2
        )

    def test_in_production_change_requires_doctor_and_fabrication_approvals(self):
        from tests.helpers import make_in_production_device

        _sid, device_id = make_in_production_device(
            self.svc, self.actors, checksum="h-prod"
        )
        chg = self.svc.propose_prescription_change(
            self.actors["doctor"], device_id, {"v": 2}, "患者口型变化"
        )
        with self.assertRaises(AuthorizationError):
            self.svc.decide_prescription_change(
                self.actors["doctor"], chg["change_id"], "rework",
                [DOCTOR_APPROVAL],
            )
        self.svc.decide_prescription_change(
            self.actors["doctor"], chg["change_id"], "rework",
            [DOCTOR_APPROVAL, FAB_APPROVAL], "双方确认返工",
        )
        # 返工：新处方版本生效，可重新排产；旧生产事实仍保留
        self.assertEqual(
            self.svc.devices[device_id]["current_prescription_version"], 2
        )
        self.svc.schedule_production(
            self.actors["doctor"], device_id, "fab1", {"layer_um": 60}
        )
        self.svc.assign_material(
            self.actors["fab_admin"], device_id, "B002", "膜片B", "供应商Y", "L2"
        )
        self.svc.start_production(self.actors["technician"], device_id)
        self.svc.tech_review(self.actors["technician"], device_id, "pass")
        self.svc.finish_production(self.actors["fab_admin"], device_id)
        h = self.proj.full_history(self.actors["doctor"], device_id)
        batches = [f["material"]["batch_id"] for f in h["fabrication"]]
        self.assertEqual(batches, ["B001", "B002"])
        decisions = [n for n in h["timeline"] if n["node"] == "change_decision"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(len(decisions[0]["approvals"]), 2)

    def test_after_finish_only_terminate_or_continue_allowed(self):
        _sid, device_id = make_finished_device(self.svc, self.actors,
                                               checksum="h-fin")
        chg = self.svc.propose_prescription_change(
            self.actors["doctor"], device_id, {"v": 9}, "成品后想改"
        )
        with self.assertRaises(Conflict):
            self.svc.decide_prescription_change(
                self.actors["doctor"], chg["change_id"], "rework",
                [DOCTOR_APPROVAL, FAB_APPROVAL],
            )

    def test_continue_use_after_finish_needs_written_reason_and_two_signoffs(self):
        _sid, device_id = make_finished_device(self.svc, self.actors,
                                               checksum="h-cont")
        chg = self.svc.propose_prescription_change(
            self.actors["doctor"], device_id, {"v": 2}, "微调"
        )
        with self.assertRaises(ValidationError):  # 缺书面理由
            self.svc.decide_prescription_change(
                self.actors["doctor"], chg["change_id"], "continue_use",
                [DOCTOR_APPROVAL, ADMIN_APPROVAL], "",
            )
        result = self.svc.decide_prescription_change(
            self.actors["doctor"], chg["change_id"], "continue_use",
            [DOCTOR_APPROVAL, ADMIN_APPROVAL], "临床确认旧件仍适用",
        )
        self.assertEqual(result["change"]["disposition"], "continue_use")
        # 继续使用：现行处方版本不变
        self.assertEqual(
            self.svc.devices[device_id]["current_prescription_version"], 1
        )

    def test_terminate_after_finish_requires_two_signoffs_and_blocks_further_work(self):
        _sid, device_id = make_finished_device(self.svc, self.actors,
                                               checksum="h-term")
        chg = self.svc.propose_prescription_change(
            self.actors["doctor"], device_id, {"v": 2}, "器械丢失"
        )
        with self.assertRaises(AuthorizationError):
            self.svc.decide_prescription_change(
                self.actors["doctor"], chg["change_id"], "terminate",
                [DOCTOR_APPROVAL], "丢失",
            )
        self.svc.decide_prescription_change(
            self.actors["doctor"], chg["change_id"], "terminate",
            [DOCTOR_APPROVAL, ADMIN_APPROVAL], "器械在外地丢失，终止旧件",
        )
        self.assertEqual(self.svc.devices[device_id]["stage"], "terminated")
        with self.assertRaises(Conflict):
            self.svc.record_fitting(
                self.actors["doctor"], device_id, "accepted"
            )
        # 裁决不可重复
        with self.assertRaises(Conflict):
            self.svc.decide_prescription_change(
                self.actors["doctor"], chg["change_id"], "continue_use",
                [DOCTOR_APPROVAL, ADMIN_APPROVAL], "x",
            )

    def test_change_cannot_be_decided_twice(self):
        up = self.svc.upload_scan(
            self.actors["doctor"], "h-z", "sha256", patient_id="p1"
        )
        dev = self.svc.create_device(
            self.actors["doctor"], "p1", up["scan"]["scan_id"]
        )
        self.svc.record_prescription(self.actors["doctor"], dev["device_id"], {"v": 1})
        chg = self.svc.propose_prescription_change(
            self.actors["doctor"], dev["device_id"], {"v": 2}
        )
        self.svc.decide_prescription_change(
            self.actors["doctor"], chg["change_id"], "rework", [DOCTOR_APPROVAL]
        )
        with self.assertRaises(Conflict):
            self.svc.decide_prescription_change(
                self.actors["doctor"], chg["change_id"], "terminate",
                [DOCTOR_APPROVAL],
            )


if __name__ == "__main__":
    unittest.main()
