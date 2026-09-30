"""测试公共夹具：构造一组已登记的机构、人员与受控时钟。"""

from datetime import datetime, timedelta, timezone

from ledger import Actor, EventStore, LedgerService, Projection


class Clock:
    def __init__(self, start=None):
        self.now = start or datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def build_world(clock=None):
    """返回 (svc, proj, actors) 的最小可用世界。"""
    clock = clock or Clock()
    svc = LedgerService(EventStore(clock=clock))
    proj = Projection(svc)

    clinic_admin = Actor("a1", "clinic_admin", "clinic1")
    clinic2_admin = Actor("a2", "clinic_admin", "clinic2")
    fab_admin = Actor("f1", "fabrication_admin", "fab1")
    doctor = Actor("d1", "prescribing_doctor", "clinic1")
    doctor2 = Actor("d2", "receiving_doctor", "clinic2")
    technician = Actor("t1", "technician", "fab1")
    guardian = Actor("g1", "guardian")

    svc.register_org(clinic_admin, "clinic1", "原门诊", "clinic")
    svc.register_org(clinic2_admin, "clinic2", "新门诊", "clinic")
    svc.register_org(fab_admin, "fab1", "加工中心", "fabrication")
    svc.register_patient(
        clinic_admin, "p1", "张小明", "g1", "张父", "clinic1"
    )
    actors = {
        "clinic_admin": clinic_admin,
        "clinic2_admin": clinic2_admin,
        "fab_admin": fab_admin,
        "doctor": doctor,
        "doctor2": doctor2,
        "technician": technician,
        "guardian": guardian,
    }
    return svc, proj, actors, clock


def make_finished_device(svc, actors, patient_id="p1", checksum="scan-hash-1",
                         batch_id="B001"):
    """从扫描一路推进到成品，返回 (scan_id, device_id)。"""
    up = svc.upload_scan(
        actors["doctor"], checksum, "sha256", patient_id=patient_id
    )
    scan_id = up["scan"]["scan_id"]
    dev = svc.create_device(actors["doctor"], patient_id, scan_id)
    svc.record_prescription(actors["doctor"], dev["device_id"], {"v": 1})
    svc.schedule_production(
        actors["doctor"], dev["device_id"], "fab1", {"layer_um": 50}
    )
    svc.assign_material(
        actors["fab_admin"], dev["device_id"],
        batch_id, "膜片A", "供应商X", "LOT1",
    )
    svc.start_production(actors["technician"], dev["device_id"])
    svc.tech_review(actors["technician"], dev["device_id"], "pass", "合格")
    svc.finish_production(actors["fab_admin"], dev["device_id"])
    return scan_id, dev["device_id"]


def deliver_device(svc, actors, device_id):
    svc.record_fitting(actors["doctor"], device_id, "accepted", "贴合")
    svc.deliver(actors["doctor"], device_id, "每日佩戴12小时")
    svc.guardian_confirm(actors["guardian"], device_id, "已收到")


def make_in_production_device(svc, actors, patient_id="p1", checksum="scan-prod-1",
                              batch_id="B001"):
    """推进到已开工（在制品），尚未技师复核。"""
    up = svc.upload_scan(actors["doctor"], checksum, "sha256", patient_id=patient_id)
    scan_id = up["scan"]["scan_id"]
    dev = svc.create_device(actors["doctor"], patient_id, scan_id)
    svc.record_prescription(actors["doctor"], dev["device_id"], {"v": 1})
    svc.schedule_production(actors["doctor"], dev["device_id"], "fab1", {"layer_um": 50})
    svc.assign_material(actors["fab_admin"], dev["device_id"],
                        batch_id, "膜片A", "供应商X", "LOT1")
    svc.start_production(actors["technician"], dev["device_id"])
    return scan_id, dev["device_id"]
