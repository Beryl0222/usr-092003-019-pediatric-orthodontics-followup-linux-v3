"""HTTP JSON API 集成测试：身份头、错误码映射与完整制作链路。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request

from service import build_app


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _store, _svc, _proj, handler = build_app()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    roles = {
        "admin": "clinic_admin",
        "admin2": "clinic_admin",
        "fab": "fabrication_admin",
        "doc": "prescribing_doctor",
        "doc2": "receiving_doctor",
        "tech": "technician",
        "guard": "guardian",
    }

    def _ids(self, actor, org=None):
        """返回 (actor_id, org_id)。"""
        mapping = {
            "admin": ("a1", "clinic1"), "admin2": ("a2", "clinic2"),
            "fab": ("f1", "fab1"), "doc": ("d1", "clinic1"),
            "doc2": ("d2", "clinic2"), "tech": ("t1", "fab1"),
            "guard": ("g1", None),
        }
        actor_id, default_org = mapping[actor]
        return actor_id, org or default_org

    def post(self, path, body, actor, org=None):
        actor_id, org_id = self._ids(actor)
        headers = {"Content-Type": "application/json",
                   "X-Actor-Id": actor_id, "X-Actor-Role": self.roles[actor]}
        if org_id:
            headers["X-Org-Id"] = org_id
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(self.base + path, data=data, headers=headers, method="POST")
        try:
            from urllib.request import urlopen

            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def get(self, path, actor, org=None):
        actor_id, org_id = self._ids(actor)
        headers = {"X-Actor-Id": actor_id, "X-Actor-Role": self.roles[actor]}
        if org_id:
            headers["X-Org-Id"] = org_id
        req = Request(self.base + path, headers=headers, method="GET")
        try:
            from urllib.request import urlopen

            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_full_pipeline_and_dedup_over_http(self):
        status, _ = self.post("/orgs", {"org_id": "clinic1", "name": "原门诊",
                                        "kind": "clinic"}, "admin")
        self.assertEqual(status, 200)
        self.assertEqual(self.post("/orgs", {"org_id": "clinic2", "name": "新门诊",
                                            "kind": "clinic"}, "admin2")[0], 200)
        self.assertEqual(self.post("/orgs", {"org_id": "fab1", "name": "加工中心",
                                            "kind": "fabrication"}, "fab")[0], 200)
        self.assertEqual(self.post("/patients", {
            "patient_id": "p1", "name": "张小明", "guardian_id": "g1",
            "guardian_name": "张父", "home_clinic_id": "clinic1"}, "admin")[0], 200)

        status, up = self.post("/scans", {"checksum": "http-hash", "algorithm": "sha256",
                                          "patient_id": "p1",
                                          "content_ref": "s3://secret"}, "doc")
        self.assertEqual(status, 200)
        scan_id = up["scan"]["scan_id"]
        self.assertNotIn("content_ref", up["scan"])

        # 重复上传：同一 scan_id，不产生第二件
        _, dup = self.post("/scans", {"checksum": "http-hash", "algorithm": "sha256",
                                      "patient_id": "p1"}, "doc")
        self.assertEqual(dup["status"], "duplicate")
        self.assertEqual(dup["scan"]["scan_id"], scan_id)

        _, dev = self.post("/devices", {"patient_id": "p1", "scan_id": scan_id}, "doc")
        device_id = dev["device_id"]
        self.assertEqual(self.post(f"/devices/{device_id}/prescriptions",
                                  {"details": {"v": 1}}, "doc")[0], 200)
        self.assertEqual(self.post(f"/devices/{device_id}/schedule",
                                  {"fabrication_org_id": "fab1",
                                   "params": {"layer_um": 50}}, "doc")[0], 200)
        self.assertEqual(self.post(f"/devices/{device_id}/material",
                                  {"batch_id": "B01", "name": "膜片",
                                   "supplier": "S", "lot": "L"}, "fab")[0], 200)
        self.assertEqual(self.post(f"/devices/{device_id}/production/start",
                                  {}, "tech")[0], 200)
        self.assertEqual(self.post(f"/devices/{device_id}/production/tech-review",
                                  {"result": "pass"}, "tech")[0], 200)
        self.assertEqual(self.post(f"/devices/{device_id}/production/finish",
                                  {}, "fab")[0], 200)
        self.assertEqual(self.post(f"/devices/{device_id}/fitting",
                                  {"conclusion": "accepted"}, "doc")[0], 200)
        self.assertEqual(self.post(f"/devices/{device_id}/delivery",
                                  {"instructions": "每日佩戴"}, "doc")[0], 200)
        self.assertEqual(self.post(f"/devices/{device_id}/guardian-confirmation",
                                  {"note": "收到"}, "guard")[0], 200)

        status, history = self.get(f"/devices/{device_id}/history", "doc")
        self.assertEqual(status, 200)
        self.assertTrue(history["guardian_confirmed"])

        # 加工单脱敏
        _, wo = self.get(f"/devices/{device_id}/work-order", "fab")
        self.assertNotIn("张小明", json.dumps(wo, ensure_ascii=False))

    def test_error_mapping_conflict_and_forbidden(self):
        # 未登记机构的医生建患者 -> 422
        status, payload = self.post("/patients", {
            "patient_id": "px", "name": "X", "guardian_id": "gx",
            "guardian_name": "X父", "home_clinic_id": "clinic-unknown"}, "doc")
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "ValidationError")

        # 无身份头 -> 403
        req = Request(self.base + "/scans", method="POST")
        with self.assertRaises(HTTPError) as ctx:
            from urllib.request import urlopen

            urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()


if __name__ == "__main__":
    unittest.main()
