"""HTTP 契约测试：鉴权、错误码映射与跨角色端到端链路。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ledger import Ledger
from service import Handler, SERVICE_ID, health_payload


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = Ledger()
        cls.Handler = type("BoundHandler", (Handler,), {"ledger": cls.ledger, "bootstrap_token": "bt"})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, token=None, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_01_health_unchanged(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())
        self.assertEqual(body["service"], SERVICE_ID)

    def test_02_bootstrap_requires_token(self):
        status, body = self.call("POST", "/orgs", body={"org_id": "X", "name": "x", "kind": "CLINIC"})
        self.assertEqual(status, 401)
        status, _ = self.call(
            "POST", "/orgs",
            headers={"X-Bootstrap-Token": "bt"},
            body={"org_id": "CL-A", "name": "城东口腔", "kind": "CLINIC"},
        )
        self.assertEqual(status, 200)
        self.call(
            "POST", "/orgs",
            headers={"X-Bootstrap-Token": "bt"},
            body={"org_id": "LAB-1", "name": "加工中心", "kind": "LAB"},
        )

    def test_03_business_endpoint_requires_bearer(self):
        status, body = self.call("POST", "/patients", body={})
        self.assertEqual(status, 401)
        status, body = self.call("POST", "/patients", token="ghost", body={})
        self.assertEqual(status, 401)

    def test_04_full_flow_over_http(self):
        # 人员
        for uid, name, role, org in [
            ("doctor", "李医生", "DOCTOR", "CL-A"),
            ("staff", "王前台", "CLINIC_STAFF", "CL-A"),
            ("lab_admin", "赵主任", "LAB_ADMIN", "LAB-1"),
            ("tech1", "孙技师", "TECHNICIAN", "LAB-1"),
            ("tech2", "周技师", "TECHNICIAN", "LAB-1"),
            ("guardian", "家长", "GUARDIAN", None),
        ]:
            status, _ = self.call(
                "POST", "/users", headers={"X-Bootstrap-Token": "bt"},
                body={"user_id": uid, "name": name, "role": role, "org_id": org},
            )
            self.assertEqual(status, 200)

        # 登记 + 确认身份
        status, patient = self.call("POST", "/patients", token="staff", body={
            "legal_name": "张小明", "id_doc_type": "HK", "id_doc_hash": "hash1",
            "birth_date": "2018-05-01", "guardian_name": "张父",
        })
        self.assertEqual(status, 200)
        pid = patient["id"]
        status, _ = self.call("POST", f"/patients/{pid}/confirm-identity", token="doctor",
                              body={"confirmed_legal_name": "张小明"})
        self.assertEqual(status, 200)

        # 扫描去重
        scan_body = {"patient_id": pid, "checksum": "sha256:abcd1234", "file_ref": "oss://s.stl"}
        status, scan1 = self.call("POST", "/scans", token="staff", body=scan_body)
        status, scan2 = self.call("POST", "/scans", token="staff", body=scan_body)
        self.assertEqual(scan1["id"], scan2["id"])
        self.assertTrue(scan2["deduplicated"])
        sid = scan1["id"]
        status, _ = self.call("POST", f"/scans/{sid}/verify", token="doctor", body={"note": "一致"})
        self.assertEqual(status, 200)

        # 处方与器械
        status, rx = self.call("POST", "/prescriptions", token="doctor", body={
            "patient_id": pid, "content": {"appliance_type": "PLATE", "expansion": "0.5/wk"},
        })
        self.assertEqual(status, 200)
        rxid = rx["id"]
        status, device = self.call("POST", "/devices", token="doctor", body={
            "patient_id": pid, "scan_id": sid, "prescription_id": rxid,
        })
        self.assertEqual(status, 200)
        did = device["id"]

        # 未指派的加工方无权
        status, body = self.call("GET", f"/devices/{did}/dossier", token="lab_admin")
        self.assertEqual(status, 403)

        # 指派 → 排产 → 制作 → 复核（制作者不能自审）
        self.call("POST", f"/devices/{did}/assign-lab", token="staff",
                  body={"lab_org_id": "LAB-1"})
        status, run = self.call("POST", f"/devices/{did}/schedule", token="lab_admin",
                                body={"manufacturing_params": {"temp": 120}})
        self.assertEqual(status, 200)
        run_id = run["id"]
        status, batch = self.call("POST", "/materials/batches", token="lab_admin", body={
            "material_code": "R", "material_name": "树脂", "lot_no": "L1", "supplier": "厂",
        })
        self.assertEqual(status, 200)
        status, _ = self.call("POST", f"/runs/{run_id}/start", token="tech1",
                              body={"material_batch_id": batch["id"]})
        self.assertEqual(status, 200)
        status, body = self.call("POST", f"/runs/{run_id}/check", token="tech1",
                                 body={"result": "PASS"})
        self.assertEqual(status, 422)
        status, _ = self.call("POST", f"/runs/{run_id}/check", token="tech2",
                              body={"result": "PASS"})
        self.assertEqual(status, 200)

        # 试戴 → 交付 → 监护确认
        self.call("POST", f"/devices/{did}/fittings", token="doctor",
                  body={"conclusion": "OK"})
        self.call("POST", f"/patients/{pid}/guardians", token="doctor",
                  body={"guardian_user_id": "guardian"})
        status, _ = self.call("POST", f"/devices/{did}/deliveries", token="staff",
                              body={"delivery_instructions": "夜间佩戴"})
        self.assertEqual(status, 200)
        status, _ = self.call(
            "POST", f"/devices/{did}/guardian-confirmations", token="guardian",
            body={"acknowledged_items": ["佩戴", "清洁"], "signature_text": "张父"},
        )
        self.assertEqual(status, 200)

        # 物流单禁止影像，运单号脱敏
        status, body = self.call("POST", f"/devices/{did}/shipments", token="staff", body={
            "carrier": "顺丰", "tracking_no": "SF1234567890", "photos": ["x.jpg"],
        })
        self.assertEqual(status, 400)
        status, label = self.call("POST", f"/devices/{did}/shipments", token="staff", body={
            "carrier": "顺丰", "tracking_no": "SF1234567890",
        })
        self.assertEqual(status, 200)
        self.assertEqual(label["tracking_no_hint"], "SF****90")
        self.assertNotIn("张小明", json.dumps(label, ensure_ascii=False))

        # 加工方最小视图不含身份信息
        status, lab_view = self.call("GET", f"/devices/{did}/dossier", token="lab_admin")
        self.assertEqual(status, 200)
        self.assertEqual(lab_view["view"], "LAB_MINIMIZED")
        self.assertNotIn("张小明", json.dumps(lab_view, ensure_ascii=False))
        self.assertNotIn("hash1", json.dumps(lab_view, ensure_ascii=False))

        # 监护人视图
        status, gv = self.call("GET", f"/devices/{did}/dossier", token="guardian")
        self.assertEqual(status, 200)
        self.assertEqual(gv["view"], "GUARDIAN")

        # 异常：过敏疑点只提示批次
        status, exc = self.call("POST", f"/devices/{did}/exceptions", token="doctor", body={
            "type": "ALLERGY_SUSPECTED", "description": "牙龈红",
        })
        self.assertEqual(status, 200)
        self.assertIn("L1", exc["risk_advisory"])

        # 逾期清单可查
        status, items = self.call("GET", "/exceptions?status=OPEN", token="doctor")
        self.assertEqual(status, 200)
        self.assertEqual(len(items), 1)

    def test_05_unknown_route_404(self):
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
