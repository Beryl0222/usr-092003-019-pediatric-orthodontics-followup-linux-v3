"""事件日志持久化：重启后履历完整重建，事件不可变。"""

import os
import tempfile
import unittest

from ledger import Actor, EventStore, LedgerService, Projection
from tests.helpers import make_finished_device


class PersistenceTest(unittest.TestCase):
    def test_state_rebuilds_from_jsonl_and_history_is_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            store = EventStore(path=path)
            svc = LedgerService(store)
            admin = Actor("a1", "clinic_admin", "clinic1")
            doc = Actor("d1", "prescribing_doctor", "clinic1")
            fab = Actor("f1", "fabrication_admin", "fab1")
            svc.register_org(admin, "clinic1", "门诊", "clinic")
            svc.register_org(fab, "fab1", "加工", "fabrication")
            svc.register_patient(admin, "p1", "张小明", "g1", "张父", "clinic1")
            up = svc.upload_scan(doc, "h1", "sha256", patient_id="p1")
            dev = svc.create_device(doc, "p1", up["scan"]["scan_id"])
            svc.record_prescription(doc, dev["device_id"], {"v": 1})
            svc.schedule_production(doc, dev["device_id"], "fab1", {"p": 1})
            svc.assign_material(fab, dev["device_id"], "B1", "m", "s", "l")
            seq_after = store.seq

            # 用同一日志重新打开：状态从事件流重建
            store2 = EventStore(path=path)
            svc2 = LedgerService(store2)
            self.assertEqual(store2.seq, seq_after)
            self.assertEqual(svc2.patients["p1"]["name"], "张小明")
            rebuilt = svc2.devices[dev["device_id"]]
            self.assertEqual(rebuilt["scan_id"], up["scan"]["scan_id"])
            self.assertEqual(
                rebuilt["production_versions"][0]["material"]["batch_id"], "B1"
            )
            history = Projection(svc2).full_history(doc, dev["device_id"])
            self.assertEqual(history["prescription"]["current_version"], 1)

    def test_event_log_is_append_only_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            store = EventStore(path=path)
            svc = LedgerService(store)
            admin = Actor("a1", "clinic_admin", "clinic1")
            svc.register_org(admin, "clinic1", "门诊", "clinic")
            svc.register_patient(admin, "p1", "张小明", "g1", "张父", "clinic1")
            with open(path, "r", encoding="utf-8") as handle:
                lines = [ln for ln in handle.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)
            # 行号单调递增、不可回填覆盖
            import json

            seqs = [json.loads(ln)["seq"] for ln in lines]
            self.assertEqual(seqs, [1, 2])


if __name__ == "__main__":
    unittest.main()
