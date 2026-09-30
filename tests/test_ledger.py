"""领域规则测试：覆盖制作履历、身份确认、处方变更责任、异常与交接。"""

import unittest
from datetime import timedelta

from ledger import (
    DEVICE_DELIVERED,
    DEVICE_LOST,
    DEVICE_TERMINATED,
    FIT_FAIL,
    FIT_OK,
    RUN_IN_PRODUCTION,
    RUN_QC_FAIL,
    RUN_QC_PASS,
    RUN_SCHEDULED,
    RUN_SUPERSEDED_BY_REWORK,
    RUN_TERMINATED,
    Ledger,
    AuthError,
    ConflictError,
    NotFoundError,
    StateError,
    ValidationError,
)


def _shift(ledger: Ledger, days: int):
    """返回一个把当前时间整体平移 days 天的新 Ledger（模拟逾期）。"""
    base = ledger.now()

    def shifted():
        return base + timedelta(days=days)

    ledger._now_fn = shifted


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.lg = Ledger()
        # 机构
        self.clinic_a = self.lg.register_org("CL-A", "城东口腔", "CLINIC")
        self.clinic_b = self.lg.register_org("CL-B", "城西口腔", "CLINIC")
        self.lab = self.lg.register_org("LAB-1", "精工加工中心", "LAB")
        self.other_lab = self.lg.register_org("LAB-2", "别家加工中心", "LAB")
        # 人员
        self.doctor = self.lg.create_user("李医生", "DOCTOR", "CL-A", "doctor")
        self.staff = self.lg.create_user("王前台", "CLINIC_STAFF", "CL-A", "staff")
        self.doctor_b = self.lg.create_user("陈医生", "DOCTOR", "CL-B", "doctor_b")
        self.lab_admin = self.lg.create_user("赵主任", "LAB_ADMIN", "LAB-1", "lab_admin")
        self.lab_admin_2 = self.lg.create_user("钱主任", "LAB_ADMIN", "LAB-2", "lab_admin_2")
        self.tech1 = self.lg.create_user("孙技师", "TECHNICIAN", "LAB-1", "tech1")
        self.tech2 = self.lg.create_user("周技师", "TECHNICIAN", "LAB-1", "tech2")
        self.guardian = self.lg.create_user("患儿家长", "GUARDIAN", None, "guardian")

        self.patient_payload = {
            "legal_name": "张小明",
            "id_doc_type": "HOUSEHOLD_REGISTER",
            "id_doc_hash": "a1b2c3",
            "birth_date": "2018-05-01",
            "guardian_name": "张父",
        }
        self.rx_content = {
            "appliance_type": "EXPANSION_PLATE",
            "expansion": "每周0.5mm",
            "wear_time": "夜间佩戴",
        }

    # ---- 小工具：标准建档到成品 ----

    def _make_ready_device(self, rx_content=None):
        """走通 建档→确认→扫描→处方→器械→指派→排产→制作→复核→试戴→交付。"""
        patient = self.lg.register_patient(self.patient_payload, self.staff)
        self.lg.confirm_identity(
            patient["id"], {"confirmed_legal_name": "张小明"}, self.doctor
        )
        scan = self.lg.register_scan(
            {
                "patient_id": patient["id"],
                "checksum": "sha256:deadbeef",
                "file_ref": "oss://scan/zhangxm.stl",
            },
            self.staff,
        )
        self.lg.verify_scan(scan["id"], self.doctor, "重算校验一致")
        rx = self.lg.create_prescription(
            {
                "patient_id": patient["id"],
                "content": rx_content or dict(self.rx_content),
            },
            self.doctor,
        )
        device = self.lg.create_device(
            {
                "patient_id": patient["id"],
                "scan_id": scan["id"],
                "prescription_id": rx["id"],
            },
            self.doctor,
        )
        self.lg.assign_lab(device["id"], {"lab_org_id": "LAB-1"}, self.staff)
        run = self.lg.schedule_production(
            device["id"], {"manufacturing_params": {"temp_c": 120, "pressure": 3.2}}, self.lab_admin
        )
        batch = self.lg.register_material_batch(
            {
                "material_code": "RESIN-X",
                "material_name": "正畸树脂X",
                "lot_no": "LOT-2026-01",
                "supplier": "材料厂",
            },
            self.lab_admin,
        )
        self.lg.start_manufacturing(run["id"], {"material_batch_id": batch["id"]}, self.tech1)
        self.lg.technician_check(
            run["id"], {"result": "PASS", "check_items": ["边缘", "卡环"]}, self.tech2
        )
        self.lg.record_fitting(
            device["id"], {"conclusion": FIT_OK, "clinical_note": "就位好"}, self.doctor
        )
        return patient, scan, rx, device, run, batch

    # ---- 身份与去重 ----

    def test_duplicate_scan_does_not_create_second_scan_or_device(self):
        patient = self.lg.register_patient(self.patient_payload, self.staff)
        self.lg.confirm_identity(patient["id"], {"confirmed_legal_name": "张小明"}, self.doctor)
        scan1 = self.lg.register_scan(
            {"patient_id": patient["id"], "checksum": "sha256:aaaa1111", "file_ref": "f1"},
            self.staff,
        )
        scan2 = self.lg.register_scan(
            {"patient_id": patient["id"], "checksum": "sha256:aaaa1111", "file_ref": "f1-copy"},
            self.staff,
        )
        self.assertEqual(scan1["id"], scan2["id"])
        self.assertTrue(scan2["deduplicated"])
        self.assertEqual(len(self.lg.scans), 1)

    def test_name_transcription_blocks_production_until_manual_confirm(self):
        patient = self.lg.register_patient(self.patient_payload, self.staff)
        # 同证件不同转写：档案回到待确认，绝不自动当新人
        again = self.lg.register_patient(
            {**self.patient_payload, "legal_name": "张晓明"}, self.staff
        )
        self.assertTrue(again["matched_existing"])
        self.assertTrue(again["name_variant"])
        self.assertEqual(again["status"], "IDENTITY_PENDING")
        self.assertEqual(len(self.lg.patients), 1)

        scan = self.lg.register_scan(
            {"patient_id": patient["id"], "checksum": "sha256:bbbb", "file_ref": "f"}, self.staff
        )
        self.lg.verify_scan(scan["id"], self.doctor)
        rx = self.lg.create_prescription(
            {"patient_id": patient["id"], "content": dict(self.rx_content)}, self.doctor
        )
        with self.assertRaises(StateError):
            self.lg.create_device(
                {"patient_id": patient["id"], "scan_id": scan["id"], "prescription_id": rx["id"]},
                self.doctor,
            )
        # 人工确认法定姓名后才放行
        self.lg.confirm_identity(
            patient["id"],
            {"confirmed_legal_name": "张小明", "note": "户口本为准，晓明为同音误写"},
            self.doctor,
        )
        device = self.lg.create_device(
            {"patient_id": patient["id"], "scan_id": scan["id"], "prescription_id": rx["id"]},
            self.doctor,
        )
        self.assertEqual(device["status"], "PLANNED")

    def test_same_checksum_different_patient_goes_to_manual_tribunal(self):
        p1 = self.lg.register_patient(self.patient_payload, self.staff)
        self.lg.confirm_identity(p1["id"], {"confirmed_legal_name": "张小明"}, self.doctor)
        self.lg.register_scan(
            {"patient_id": p1["id"], "checksum": "sha256:cccc", "file_ref": "f"}, self.staff
        )
        p2_payload = {
            "legal_name": "李小红",
            "id_doc_type": "BIRTH_CERT",
            "id_doc_hash": "d4e5f6",
            "birth_date": "2019-01-01",
            "guardian_name": "李父",
        }
        p2 = self.lg.register_patient(p2_payload, self.staff)
        self.lg.confirm_identity(p2["id"], {"confirmed_legal_name": "李小红"}, self.doctor)
        with self.assertRaises(ConflictError):
            self.lg.register_scan(
                {"patient_id": p2["id"], "checksum": "sha256:cccc", "file_ref": "f2"}, self.staff
            )
        conflicts = list(self.lg.scan_conflicts.values())
        self.assertEqual(len(conflicts), 1)
        cid = conflicts[0]["id"]
        self.lg.review_scan_conflict(
            cid, {"decision": "REJECT_UPLOAD", "note": "文件误挂"}, self.doctor
        )
        self.assertEqual(self.lg.scan_conflicts[cid]["status"], "RESOLVED")
        self.assertEqual(len(self.lg.scans), 1)

    def test_scan_must_be_verified_before_device(self):
        patient = self.lg.register_patient(self.patient_payload, self.staff)
        self.lg.confirm_identity(patient["id"], {"confirmed_legal_name": "张小明"}, self.doctor)
        scan = self.lg.register_scan(
            {"patient_id": patient["id"], "checksum": "sha256:dddd", "file_ref": "f"}, self.staff
        )
        rx = self.lg.create_prescription(
            {"patient_id": patient["id"], "content": dict(self.rx_content)}, self.doctor
        )
        with self.assertRaises(StateError):
            self.lg.create_device(
                {"patient_id": patient["id"], "scan_id": scan["id"], "prescription_id": rx["id"]},
                self.doctor,
            )

    # ---- 处方变更：批准责任与不可覆盖 ----

    def test_revision_before_scheduling_activates_directly(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        # 直接在一个独立处方上验证排产前变更
        rx2 = self.lg.create_prescription(
            {"patient_id": patient["id"], "content": dict(self.rx_content)}, self.doctor
        )
        self.lg.revise_prescription(
            rx2["id"], {"content": {**self.rx_content, "expansion": "每周0.25mm"},
                        "change_reason": "降低加力"}, self.doctor
        )
        self.lg.approve_revision(
            rx2["id"], {"version": 2, "decision": "ACTIVATE"}, self.doctor
        )
        self.assertEqual(rx2["versions"][0]["status"], "SUPERSEDED")
        self.assertEqual(rx2["versions"][1]["status"], "ACTIVE")

    def test_revision_during_production_terminate_requires_doctor_and_preserves_facts(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        # 另造一件在制件
        patient2 = self.lg.register_patient(
            {
                "legal_name": "王小虎",
                "id_doc_type": "BIRTH_CERT",
                "id_doc_hash": "zz1",
                "birth_date": "2017-03-03",
                "guardian_name": "王父",
            },
            self.staff,
        )
        self.lg.confirm_identity(patient2["id"], {"confirmed_legal_name": "王小虎"}, self.doctor)
        scan2 = self.lg.register_scan(
            {"patient_id": patient2["id"], "checksum": "sha256:eeee", "file_ref": "f"}, self.staff
        )
        self.lg.verify_scan(scan2["id"], self.doctor)
        rx2 = self.lg.create_prescription(
            {"patient_id": patient2["id"], "content": dict(self.rx_content)}, self.doctor
        )
        dv2 = self.lg.create_device(
            {"patient_id": patient2["id"], "scan_id": scan2["id"], "prescription_id": rx2["id"]},
            self.doctor,
        )
        self.lg.assign_lab(dv2["id"], {"lab_org_id": "LAB-1"}, self.staff)
        run2 = self.lg.schedule_production(
            dv2["id"], {"manufacturing_params": {"temp_c": 110}}, self.lab_admin
        )
        self.lg.start_manufacturing(run2["id"], {"material_batch_id": batch["id"]}, self.tech1)
        self.assertEqual(run2["status"], RUN_IN_PRODUCTION)

        self.lg.revise_prescription(
            rx2["id"], {"content": {**self.rx_content, "wear_time": "全天"}, "change_reason": "方案调整"},
            self.doctor,
        )
        # 制作中不能 ACTIVATE/CONTINUE
        with self.assertRaises(ValidationError):
            self.lg.approve_revision(
                rx2["id"], {"version": 2, "device_id": dv2["id"], "decision": "ACTIVATE"}, self.doctor
            )
        # 加工方不能替医生作终止决定
        with self.assertRaises(AuthError):
            self.lg.approve_revision(
                rx2["id"], {"version": 2, "device_id": dv2["id"], "decision": "TERMINATE"},
                self.lab_admin,
            )
        # 责任医生批准终止
        self.lg.approve_revision(
            rx2["id"], {"version": 2, "device_id": dv2["id"], "decision": "TERMINATE"}, self.doctor
        )
        self.assertEqual(run2["status"], RUN_TERMINATED)
        self.assertEqual(dv2["status"], DEVICE_TERMINATED)
        self.assertIsNotNone(run2["terminated"]["by"])
        # 制作事实仍在履历中，可反查
        dossier = self.lg.device_dossier(dv2["id"], self.doctor)
        self.assertEqual(len(dossier["production_runs"]), 1)
        self.assertEqual(dossier["production_runs"][0]["material_snapshot"]["lot_no"], "LOT-2026-01")

    def test_revision_rework_after_schedule_needs_lab_cosign_and_new_run_keeps_history(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.deliver(device["id"], {"delivery_instructions": "夜间佩戴，勿煮沸"}, self.staff)
        # 对刚交付的件提变更并返工
        self.assertEqual(self.lg.devices[device["id"]]["status"], DEVICE_DELIVERED)
        self.lg.revise_prescription(
            rx["id"], {"content": {**self.rx_content, "expansion": "每周1mm"}, "change_reason": "加力调整"},
            self.doctor,
        )
        # 无加工方会签 -> 拒绝
        with self.assertRaises(AuthError):
            self.lg.approve_revision(
                rx["id"], {"version": 2, "device_id": device["id"], "decision": "REWORK"}, self.doctor
            )
        # 别家加工中心会签 -> 拒绝
        with self.assertRaises(AuthError):
            self.lg.approve_revision(
                rx["id"],
                {"version": 2, "device_id": device["id"], "decision": "REWORK",
                 "lab_approver_id": "lab_admin_2"},
                self.doctor,
            )
        # 医生 + 承担加工中心主任 会签返工
        self.lg.approve_revision(
            rx["id"],
            {"version": 2, "device_id": device["id"], "decision": "REWORK",
             "lab_approver_id": "lab_admin"},
            self.doctor,
        )
        runs = self.lg.device_dossier(device["id"], self.doctor)["production_runs"]
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["status"], RUN_SUPERSEDED_BY_REWORK)
        self.assertEqual(runs[1]["status"], RUN_SCHEDULED)
        self.assertEqual(runs[1]["rework_of_run"], runs[0]["run_id"] if "run_id" in runs[0] else runs[0]["id"])
        self.assertEqual(runs[1]["prescription_version"], 2)
        # 新批次必须重新制作与试戴，旧试戴不得沿用
        with self.assertRaises(StateError):
            self.lg.deliver(
                device["id"], {"delivery_instructions": "继续夜间佩戴"}, self.staff
            )

    def test_finished_device_can_continue_only_with_doctor_approval(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.revise_prescription(
            rx["id"], {"content": {**self.rx_content, "wear_time": "夜间+晨起"}, "change_reason": "微调"},
            self.doctor,
        )
        # 成品不能被“终止”抹掉
        with self.assertRaises(StateError):
            self.lg.approve_revision(
                rx["id"], {"version": 2, "device_id": device["id"], "decision": "TERMINATE"}, self.doctor
            )
        self.lg.approve_revision(
            rx["id"], {"version": 2, "device_id": device["id"], "decision": "CONTINUE"}, self.doctor
        )
        dossier = self.lg.device_dossier(device["id"], self.doctor)
        self.assertEqual(len(dossier["lineage"]["continuance_approvals"]), 1)
        self.assertEqual(dossier["production_runs"][0]["status"], RUN_QC_PASS)

    def test_revision_cannot_be_approved_twice(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.revise_prescription(
            rx["id"], {"content": dict(self.rx_content), "change_reason": "无实质变更"}, self.doctor
        )
        self.lg.approve_revision(
            rx["id"], {"version": 2, "device_id": device["id"], "decision": "CONTINUE"}, self.doctor
        )
        with self.assertRaises(StateError):
            self.lg.approve_revision(
                rx["id"], {"version": 2, "device_id": device["id"], "decision": "CONTINUE"}, self.doctor
            )

    # ---- 制作内控 ----

    def test_maker_cannot_check_own_work(self):
        patient = self.lg.register_patient(self.patient_payload, self.staff)
        self.lg.confirm_identity(patient["id"], {"confirmed_legal_name": "张小明"}, self.doctor)
        scan = self.lg.register_scan(
            {"patient_id": patient["id"], "checksum": "sha256:f00d", "file_ref": "f"}, self.staff
        )
        self.lg.verify_scan(scan["id"], self.doctor)
        rx = self.lg.create_prescription(
            {"patient_id": patient["id"], "content": dict(self.rx_content)}, self.doctor
        )
        device = self.lg.create_device(
            {"patient_id": patient["id"], "scan_id": scan["id"], "prescription_id": rx["id"]},
            self.doctor,
        )
        self.lg.assign_lab(device["id"], {"lab_org_id": "LAB-1"}, self.staff)
        run = self.lg.schedule_production(
            device["id"], {"manufacturing_params": {"x": 1}}, self.lab_admin
        )
        batch = self.lg.register_material_batch(
            {"material_code": "M", "material_name": "m", "lot_no": "L1", "supplier": "s"},
            self.lab_admin,
        )
        self.lg.start_manufacturing(run["id"], {"material_batch_id": batch["id"]}, self.tech1)
        with self.assertRaises(StateError):
            self.lg.technician_check(run["id"], {"result": "PASS"}, self.tech1)

    def test_qc_fail_remake_keeps_failed_run_and_blocks_fitting(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        # 用一条新流程制造 QC 失败
        p2 = self.lg.register_patient(
            {**self.patient_payload, "id_doc_hash": "qc1", "legal_name": "钱小多",
             "birth_date": "2016-02-02", "guardian_name": "钱父"},
            self.staff,
        )
        self.lg.confirm_identity(p2["id"], {"confirmed_legal_name": "钱小多"}, self.doctor)
        sc2 = self.lg.register_scan(
            {"patient_id": p2["id"], "checksum": "sha256:0c", "file_ref": "f"}, self.staff
        )
        self.lg.verify_scan(sc2["id"], self.doctor)
        rx2 = self.lg.create_prescription(
            {"patient_id": p2["id"], "content": dict(self.rx_content)}, self.doctor
        )
        dv2 = self.lg.create_device(
            {"patient_id": p2["id"], "scan_id": sc2["id"], "prescription_id": rx2["id"]}, self.doctor
        )
        self.lg.assign_lab(dv2["id"], {"lab_org_id": "LAB-1"}, self.staff)
        run2 = self.lg.schedule_production(
            dv2["id"], {"manufacturing_params": {"x": 1}}, self.lab_admin
        )
        self.lg.start_manufacturing(run2["id"], {"material_batch_id": batch["id"]}, self.tech1)
        self.lg.technician_check(run2["id"], {"result": "FAIL", "note": "卡环过紧"}, self.tech2)
        self.assertEqual(run2["status"], RUN_QC_FAIL)
        with self.assertRaises(StateError):
            self.lg.record_fitting(dv2["id"], {"conclusion": FIT_OK}, self.doctor)
        # 加工方内部返工
        run3 = self.lg.remake_after_qc_fail(
            run2["id"], {"manufacturing_params": {"x": 1, "adjust": "relax"}}, self.lab_admin
        )
        self.assertEqual(run2["status"], RUN_SUPERSEDED_BY_REWORK)
        self.lg.start_manufacturing(run3["id"], {"material_batch_id": batch["id"]}, self.tech1)
        self.lg.technician_check(run3["id"], {"result": "PASS"}, self.tech2)
        self.lg.record_fitting(dv2["id"], {"conclusion": FIT_OK}, self.doctor)
        dossier = self.lg.device_dossier(dv2["id"], self.doctor)
        statuses = [r["status"] for r in dossier["production_runs"]]
        self.assertEqual(statuses, [RUN_SUPERSEDED_BY_REWORK, RUN_QC_PASS])

    # ---- 丢失/破损/过敏/召回 ----

    def test_loss_then_replacement_requires_explicit_attestation(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.deliver(device["id"], {"delivery_instructions": "夜间佩戴，勿煮沸"}, self.staff)
        self.lg.add_guardian(patient["id"], "guardian", self.doctor)
        exc = self.lg.report_exception(
            device["id"],
            {"type": "LOSS", "description": "外地旅行遗失", "responsible_person_id": "doctor"},
            self.guardian,
        )
        self.assertEqual(device["status"] if False else self.lg.devices[device["id"]]["status"], DEVICE_LOST)
        self.assertTrue(exc["deadline"])

        # 有照片也不能照旧重做：无显式复用签署 -> 拒绝
        with self.assertRaises(ValidationError):
            self.lg.create_device(
                {"patient_id": patient["id"], "scan_id": scan["id"], "prescription_id": rx["id"],
                 "replacement_of": device["id"]},
                self.doctor,
            )
        # 未结丢失异常未处理 -> 即使签署也阻断静默复制
        with self.assertRaises(StateError):
            self.lg.create_device(
                {"patient_id": patient["id"], "scan_id": scan["id"], "prescription_id": rx["id"],
                 "replacement_of": device["id"], "reuse_scan_attested": True},
                self.doctor,
            )
        # 结案（例如确认需要补制）后，医生显式签署印模仍有效，方可补制
        self.lg.resolve_exception(
            exc["id"], {"outcome": "REMAKE_ORDERED", "note": "医生确认口型无变化"}, self.doctor
        )
        new_device = self.lg.create_device(
            {"patient_id": patient["id"], "scan_id": scan["id"], "prescription_id": rx["id"],
             "replacement_of": device["id"], "reuse_scan_attested": True},
            self.doctor,
        )
        self.assertEqual(self.lg.devices[device["id"]]["replaced_by"], new_device["id"])
        self.assertEqual(new_device["replacement_of"], device["id"])
        # 两件器械都能独立反查
        self.assertEqual(
            self.lg.device_dossier(new_device["id"], self.doctor)["lineage"]["replacement_of"],
            device["id"],
        )

    def test_guardian_report_must_name_clinic_responsible(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.deliver(device["id"], {"delivery_instructions": "x"}, self.staff)
        self.lg.add_guardian(patient["id"], "guardian", self.doctor)
        with self.assertRaises(ValidationError):
            self.lg.report_exception(
                device["id"], {"type": "DAMAGE", "description": "裂了"}, self.guardian
            )
        exc = self.lg.report_exception(
            device["id"],
            {"type": "DAMAGE", "description": "裂了", "responsible_person_id": "doctor"},
            self.guardian,
        )
        self.assertEqual(exc["responsible_person"]["id"], "doctor")
        self.assertTrue(self.lg.devices[device["id"]]["damaged"])

    def test_allergy_only_advises_lots_no_clinical_verdict(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.deliver(device["id"], {"delivery_instructions": "x"}, self.staff)
        exc = self.lg.report_exception(
            device["id"],
            {"type": "ALLERGY_SUSPECTED", "description": "牙龈红肿", "responsible_person_id": "doctor"},
            self.doctor,
        )
        self.assertIn("LOT-2026-01", exc["risk_advisory"])
        self.assertIn("由医生决定", exc["risk_advisory"])
        risk = self.lg.risk_notices(device["id"], self.doctor)
        self.assertEqual(len(risk["notices"]), 1)
        self.assertIn("临床判断由医生作出", risk["disclaimer"])

    def test_batch_recall_flags_affected_devices_with_deadlines(self):
        # 第一件使用 LOT-2026-01
        p1, s1, rx1, dv1, run1, batch1 = self._make_ready_device()
        # 第二件使用同批次
        p2, s2, rx2, dv2, run2, _ = None, None, None, None, None, None
        patient2 = self.lg.register_patient(
            {**self.patient_payload, "id_doc_hash": "rc1", "legal_name": "林小雷",
             "birth_date": "2015-09-09", "guardian_name": "林父"},
            self.staff,
        )
        self.lg.confirm_identity(patient2["id"], {"confirmed_legal_name": "林小雷"}, self.doctor)
        sc2 = self.lg.register_scan(
            {"patient_id": patient2["id"], "checksum": "sha256:000000000000000c", "file_ref": "f"}, self.staff
        )
        self.lg.verify_scan(sc2["id"], self.doctor)
        rx2 = self.lg.create_prescription(
            {"patient_id": patient2["id"], "content": dict(self.rx_content)}, self.doctor
        )
        dv2 = self.lg.create_device(
            {"patient_id": patient2["id"], "scan_id": sc2["id"], "prescription_id": rx2["id"]},
            self.doctor,
        )
        self.lg.assign_lab(dv2["id"], {"lab_org_id": "LAB-1"}, self.staff)
        run2 = self.lg.schedule_production(
            dv2["id"], {"manufacturing_params": {"x": 2}}, self.lab_admin
        )
        self.lg.start_manufacturing(run2["id"], {"material_batch_id": batch1["id"]}, self.tech2)
        self.lg.technician_check(run2["id"], {"result": "PASS"}, self.tech1)

        result = self.lg.initiate_recall(
            batch1["id"], {"reason": "单体残留超标", "responsible_person_id": "doctor"}, self.lab_admin
        )
        self.assertEqual(result["affected_count"], 2)
        # 已召回批次不得再用于制作
        p3 = self.lg.register_patient(
            {**self.patient_payload, "id_doc_hash": "rc2", "legal_name": "赵小云",
             "birth_date": "2016-08-08", "guardian_name": "赵父"},
            self.staff,
        )
        self.lg.confirm_identity(p3["id"], {"confirmed_legal_name": "赵小云"}, self.doctor)
        sc3 = self.lg.register_scan(
            {"patient_id": p3["id"], "checksum": "sha256:000000000000000d", "file_ref": "f"}, self.staff
        )
        self.lg.verify_scan(sc3["id"], self.doctor)
        rx3 = self.lg.create_prescription(
            {"patient_id": p3["id"], "content": dict(self.rx_content)}, self.doctor
        )
        dv3 = self.lg.create_device(
            {"patient_id": p3["id"], "scan_id": sc3["id"], "prescription_id": rx3["id"]}, self.doctor
        )
        self.lg.assign_lab(dv3["id"], {"lab_org_id": "LAB-1"}, self.staff)
        run3 = self.lg.schedule_production(
            dv3["id"], {"manufacturing_params": {"x": 3}}, self.lab_admin
        )
        with self.assertRaises(StateError):
            self.lg.start_manufacturing(run3["id"], {"material_batch_id": batch1["id"]}, self.tech1)

        # 风险范围可见且含截止时间
        risk = self.lg.risk_notices(dv1["id"], self.doctor)
        self.assertEqual(risk["recall_scope"][0]["affected_run_count"], 2)
        open_excs = self.lg.list_exceptions(self.doctor, status="OPEN")
        self.assertEqual(len(open_excs), 2)
        for e in open_excs:
            self.assertEqual(e["type"], "BATCH_RECALL")
            self.assertTrue(e["deadline"])

    def test_overdue_flag_after_deadline(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.deliver(device["id"], {"delivery_instructions": "x"}, self.staff)
        exc = self.lg.report_exception(
            device["id"],
            {"type": "DAMAGE", "description": "裂", "responsible_person_id": "doctor",
             "deadline_days": 7},
            self.doctor,
        )
        self.assertFalse(self.lg.list_exceptions(self.doctor)[0]["overdue"])
        _shift(self.lg, 8)
        self.assertTrue(self.lg.list_exceptions(self.doctor)[0]["overdue"])
        overdue = self.lg.list_exceptions(self.doctor, overdue_only=True)
        self.assertEqual([e["id"] for e in overdue], [exc["id"]])

    # ---- 授权与最小化 ----

    def test_lab_sees_minimum_data_and_unassigned_lab_sees_nothing(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        view = self.lg.device_dossier(device["id"], self.lab_admin)
        self.assertEqual(view["view"], "LAB_MINIMIZED")
        flat = repr(view)
        self.assertNotIn("张小明", flat)
        self.assertNotIn("a1b2c3", flat)  # 证件 hash 不外泄
        self.assertIn("sha256:deadbeef", flat)  # 制作所需印模校验值可见
        self.assertEqual(view["prescription"]["fabrication"]["appliance_type"], "EXPANSION_PLATE")
        # 未被指派的加工中心无权
        with self.assertRaises(AuthError):
            self.lg.device_dossier(device["id"], self.lab_admin_2)

    def test_other_clinic_and_guardian_authorization(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        with self.assertRaises(AuthError):
            self.lg.device_dossier(device["id"], self.doctor_b)
        # 监护人未授权
        with self.assertRaises(AuthError):
            self.lg.device_dossier(device["id"], self.guardian)
        self.lg.add_guardian(patient["id"], "guardian", self.doctor)
        gview = self.lg.device_dossier(device["id"], self.guardian)
        self.assertEqual(gview["view"], "GUARDIAN")
        self.assertNotIn("a1b2c3", repr(gview))

    def test_shipment_rejects_identity_and_image_attachments_and_masks_tracking(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.deliver(device["id"], {"delivery_instructions": "x"}, self.staff)
        with self.assertRaises(ValidationError):
            self.lg.create_shipment(
                device["id"],
                {"carrier": "顺丰", "tracking_no": "SF1234567890", "photos": ["face.jpg"]},
                self.staff,
            )
        with self.assertRaises(ValidationError):
            self.lg.create_shipment(
                device["id"],
                {"carrier": "顺丰", "tracking_no": "SF1234567890",
                 "attachments": [{"kind": "ID_DOCUMENT", "ref": "id.jpg"}]},
                self.staff,
            )
        label = self.lg.create_shipment(
            device["id"], {"carrier": "顺丰", "tracking_no": "SF1234567890"}, self.staff
        )
        self.assertNotIn("张小明", repr(label))
        self.assertEqual(label["tracking_no_hint"], "SF****90")

    # ---- 跨院交接 ----

    def test_opening_scenario_out_of_town_loss_cannot_be_remade_from_photo(self):
        """开篇场景：异地丢失，仅持照片；监护人授权外地门诊，履历可调取但不能照旧复制。"""
        # 原门诊已完成一件并交付
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.deliver(device["id"], {"delivery_instructions": "夜间佩戴"}, self.staff)
        self.lg.add_guardian(patient["id"], "guardian", self.doctor)

        # 监护人在外地门诊当面授权查看与随访
        self.lg.authorize_clinic(
            patient["id"], "CL-B",
            {"scope": "VIEW_AND_FOLLOWUP", "purpose": "异地丢件复诊"},
            self.guardian,
        )
        # 外地门诊仅凭照片不能"照旧重做"：他们能读到履历与校验值，
        # 但复用印模补制必须满足 丢失状态 + 异常结案 + 医生显式签署
        dossier = self.lg.device_dossier(device["id"], self.doctor_b)
        self.assertEqual(dossier["scan"]["checksum"], "sha256:deadbeef")
        exc = self.lg.report_exception(
            device["id"],
            {"type": "LOSS", "description": "外地酒店遗失，仅余手机照片",
             "responsible_person_id": "doctor_b"},
            self.doctor_b,
        )
        # 异常带着责任人与期限
        self.assertEqual(exc["responsible_person"]["id"], "doctor_b")
        self.assertTrue(exc["deadline"])

        # 异常未结案：任何机构都不能静默补制
        with self.assertRaises(StateError):
            self.lg.create_device(
                {"patient_id": patient["id"], "scan_id": scan["id"], "prescription_id": rx["id"],
                 "replacement_of": device["id"], "reuse_scan_attested": True},
                self.doctor_b,
            )
        # 只读授权的门诊不能开制（构造一个只读门诊）
        self.lg.register_org("CL-C", "城南口腔", "CLINIC")
        doctor_c = self.lg.create_user("吴医生", "DOCTOR", "CL-C", "doctor_c")
        self.lg.authorize_clinic(
            patient["id"], "CL-C", {"scope": "VIEW_ONLY"}, self.guardian
        )
        with self.assertRaises(AuthError):
            self.lg.report_exception(
                device["id"],
                {"type": "DAMAGE", "description": "x", "responsible_person_id": "doctor_c"},
                doctor_c,
            )

        # 结案并由接诊医生显式确认印模仍有效，补制产生新器械、旧件事实保留
        self.lg.resolve_exception(
            exc["id"], {"outcome": "REMAKE_ORDERED", "note": "复查口型无变化，照片仅辅助"},
            self.doctor_b,
        )
        new_device = self.lg.create_device(
            {"patient_id": patient["id"], "scan_id": scan["id"], "prescription_id": rx["id"],
             "replacement_of": device["id"], "reuse_scan_attested": True},
            self.doctor_b,
        )
        self.assertEqual(new_device["replacement_of"], device["id"])
        # 两件器械履历都可反查，旧件不被覆盖
        self.assertEqual(len(self.lg.devices), 2)
        old = self.lg.device_dossier(device["id"], self.doctor_b)
        self.assertEqual(old["status"], DEVICE_LOST)
        self.assertEqual(old["lineage"]["replaced_by"], new_device["id"])

    def test_transfer_blocked_until_open_exceptions_have_owner_and_deadline(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.deliver(device["id"], {"delivery_instructions": "x"}, self.staff)
        exc = self.lg.report_exception(
            device["id"],
            {"type": "ALLERGY_SUSPECTED", "description": "红肿", "responsible_person_id": "doctor"},
            self.doctor,
        )
        transfer = self.lg.initiate_transfer(
            device["id"], {"to_org_id": "CL-B"}, self.staff
        )
        self.assertEqual(transfer["status"], "PENDING_ACCEPTANCE")
        self.assertEqual(len(transfer["carried_exceptions"]), 1)
        carried = transfer["carried_exceptions"][0]
        self.assertEqual(carried["responsible_person"]["id"], "doctor")
        self.assertTrue(carried["deadline"])
        # 接收方必须逐项确认，不得遗漏
        with self.assertRaises(ValidationError):
            self.lg.accept_transfer(transfer["id"], {"acknowledged_exception_ids": []}, self.doctor_b)
        self.lg.accept_transfer(
            transfer["id"], {"acknowledged_exception_ids": [exc["id"]]}, self.doctor_b
        )
        # 归属已转移：原门诊失去 owner 权限，新门诊可看完整履历
        self.assertEqual(self.lg.patients[patient["id"]]["owner_org"]["id"], "CL-B")
        with self.assertRaises(AuthError):
            self.lg.device_dossier(device["id"], self.doctor)
        dossier = self.lg.device_dossier(device["id"], self.doctor_b)
        self.assertEqual(dossier["patient"]["legal_name"], "张小明")
        # 未结异常仍带着责任人与期限
        still_open = self.lg.list_exceptions(self.doctor_b, status="OPEN")
        self.assertEqual(len(still_open), 1)
        self.assertEqual(still_open[0]["responsible_person"]["id"], "doctor")

    def test_transfer_history_allows_full_traceability(self):
        patient, scan, rx, device, run, batch = self._make_ready_device()
        self.lg.deliver(device["id"], {"delivery_instructions": "x"}, self.staff)
        tr = self.lg.initiate_transfer(device["id"], {"to_org_id": "CL-B"}, self.staff)
        self.lg.accept_transfer(tr["id"], {"acknowledged_exception_ids": []}, self.doctor_b)
        dossier = self.lg.device_dossier(device["id"], self.doctor_b)
        self.assertEqual(len(dossier["transfers"]), 1)
        self.assertEqual(dossier["transfers"][0]["from_org"]["id"], "CL-A")
        # 反查链完整：扫描、处方版本、材料、制作人、复核、试戴
        self.assertEqual(dossier["scan"]["checksum"], "sha256:deadbeef")
        self.assertEqual(dossier["prescription"]["current_version"], 1)
        run_view = dossier["production_runs"][0]
        self.assertEqual(run_view["maker"]["id"], "tech1")
        self.assertEqual(run_view["checker"]["id"], "tech2")
        self.assertEqual(run_view["material_snapshot"]["lot_no"], "LOT-2026-01")
        self.assertEqual(dossier["fittings"][0]["by"], "doctor")


if __name__ == "__main__":
    unittest.main()
