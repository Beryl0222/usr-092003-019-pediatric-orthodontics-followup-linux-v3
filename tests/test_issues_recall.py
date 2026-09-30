"""丢失、破损、过敏疑点、批次召回的限期处置规则。"""

import unittest

from ledger import ValidationError
from tests.helpers import build_world, deliver_device, make_finished_device


class IssueSlaTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.proj, self.actors, self.clock = build_world()
        _sid, self.device_id = make_finished_device(self.svc, self.actors)
        deliver_device(self.svc, self.actors, self.device_id)

    def test_each_issue_kind_has_its_own_deadline(self):
        from datetime import timedelta

        cases = [
            ("loss", 7), ("damage", 5),
            ("allergy_suspected", 3), ("batch_recall", 10),
        ]
        for kind, days in cases:
            issue = self.svc.open_issue(
                self.actors["guardian"], self.device_id, kind, kind
            )
            expected = (self.clock() + timedelta(days=days)).date().isoformat()
            self.assertTrue(issue["due_at"].startswith(expected))
            self.assertTrue(issue["risk_notice"])
            self.assertEqual(self.svc.issue_status(issue["issue_id"]), "open")

    def test_issue_becomes_overdue_after_deadline_without_reset(self):
        issue = self.svc.open_issue(
            self.actors["guardian"], self.device_id, "allergy_suspected", "红疹"
        )
        self.clock.advance(days=4)
        self.assertEqual(self.svc.issue_status(issue["issue_id"]), "overdue")
        # 采取处置不重置截止时间，状态仍是逾期（提示风险）
        self.svc.record_issue_action(
            self.actors["doctor"], issue["issue_id"], "已建议停用并预约复诊"
        )
        self.assertEqual(self.svc.issue_status(issue["issue_id"]), "overdue")

    def test_allergy_cannot_close_without_doctor_clinical_conclusion(self):
        issue = self.svc.open_issue(
            self.actors["guardian"], self.device_id, "allergy_suspected", "疑似过敏"
        )
        with self.assertRaises(ValidationError):
            self.svc.resolve_issue(
                self.actors["doctor"], issue["issue_id"], "结案"
            )
        # 监护人不能代为下临床结论
        from ledger import AuthorizationError

        with self.assertRaises(AuthorizationError):
            self.svc.resolve_issue(
                self.actors["guardian"], issue["issue_id"], "好了",
                clinical_conclusion="确认过敏",
            )
        self.svc.resolve_issue(
            self.actors["doctor"], issue["issue_id"], "已复诊处理",
            clinical_conclusion="临床表现不支持材料过敏，恢复使用",
        )
        self.assertEqual(self.svc.issue_status(issue["issue_id"]), "resolved")

    def test_system_only_warns_no_clinical_verdict(self):
        issue = self.svc.open_issue(
            self.actors["guardian"], self.device_id, "allergy_suspected", "痒"
        )
        self.assertIn("系统不判定过敏", issue["risk_notice"])


class BatchRecallTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.proj, self.actors, self.clock = build_world()

    def test_recall_locks_affected_scope_and_opens_timely_issue_per_device(self):
        s1, d1 = make_finished_device(
            self.svc, self.actors, patient_id="p1", checksum="h1", batch_id="B-REC"
        )
        # 第二名患者、第二件同批次器械
        self.svc.register_patient(
            self.actors["clinic_admin"], "p2", "李华", "g2", "李母", "clinic1"
        )
        up = self.svc.upload_scan(
            self.actors["doctor"], "h2", "sha256", patient_id="p2"
        )
        d2dev = self.svc.create_device(
            self.actors["doctor"], "p2", up["scan"]["scan_id"]
        )
        self.svc.record_prescription(
            self.actors["doctor"], d2dev["device_id"], {"v": 1}
        )
        self.svc.schedule_production(
            self.actors["doctor"], d2dev["device_id"], "fab1", {}
        )
        self.svc.assign_material(
            self.actors["fab_admin"], d2dev["device_id"],
            "B-REC", "膜片A", "供应商X", "LOT1",
        )
        self.svc.start_production(self.actors["technician"], d2dev["device_id"])
        self.svc.tech_review(self.actors["technician"], d2dev["device_id"], "pass")
        self.svc.finish_production(self.actors["fab_admin"], d2dev["device_id"])

        recall = self.svc.open_recall(
            self.actors["fab_admin"], "B-REC", "膜片批次强度不达标"
        )
        self.assertEqual(set(recall["affected_device_ids"]),
                         {d1, d2dev["device_id"]})
        self.assertEqual(len(recall["issue_ids"]), 2)
        for issue_id in recall["issue_ids"]:
            issue = self.svc.issues[issue_id]
            self.assertEqual(issue["kind"], "batch_recall")
            self.assertIn("2026-09-11", issue["due_at"])

        # 两件器械的处置相互独立：只结一件不影响另一件
        self.svc.resolve_issue(
            self.actors["doctor"], recall["issue_ids"][0],
            "已停用召回件并安排重制",
        )
        second = self.svc.issues[recall["issue_ids"][1]]
        self.assertNotEqual(second["status"], "resolved")

    def test_terminated_and_superseded_devices_excluded_from_recall_scope(self):
        _s, d1 = make_finished_device(
            self.svc, self.actors, checksum="h1", batch_id="B-OLD"
        )
        recall = self.svc.open_recall(
            self.actors["fab_admin"], "B-NOPE", "无此批次"
        )
        self.assertEqual(recall["affected_device_ids"], [])
        self.assertEqual(recall["issue_ids"], [])


if __name__ == "__main__":
    unittest.main()
