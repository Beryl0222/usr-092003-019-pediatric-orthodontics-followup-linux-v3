"""只读投影：按授权返回器械履历、进度与最小化加工数据。

数据最小化原则：
- 加工方只能拿到完成制作所需的数据（处方、参数、扫描引用，无监护人身份信息）；
- 接诊机构与监护人按授权查看交付及处置进度；
- 普通物流单只含配送必要信息，不含影像、扫描内容与身份材料。
"""

from . import catalog as c
from .errors import AuthorizationError, NotFound


SENSITIVE_SCAN_FIELDS = ("content_ref",)   # 扫描影像本体引用不进入任何投影


def _public_scan(scan):
    return {
        "scan_id": scan["scan_id"],
        "kind": scan["kind"],
        "checksum": scan["checksum"],
        "algorithm": scan["algorithm"],
        "identity_confirmed": scan["identity_confirmed"],
    }


class Projection:
    def __init__(self, svc):
        self.svc = svc

    # ----------------------------------------------------------- 内部组装

    def _device_or_404(self, device_id):
        dev = self.svc.devices.get(device_id)
        if not dev:
            raise NotFound(f"器械 {device_id} 不存在")
        return dev

    def _patient_or_404(self, patient_id):
        return self.svc._patient(patient_id)

    def _timeline(self, dev):
        """从事件流提取真实发生顺序的关键节点（含失败/返工版本）。"""
        items = []
        for e in self.svc.store.events(f"device:{dev['device_id']}"):
            p = e["payload"]
            if e["type"] == "PrescriptionRecorded":
                items.append({"node": "prescription", "version": p["version"],
                              "by": p["prescribed_by"], "ts": e["ts"]})
            elif e["type"] == "ChangeDecided":
                items.append({"node": "change_decision",
                              "change_id": p["change_id"],
                              "disposition": p["disposition"],
                              "approvals": p["approvals"], "ts": e["ts"]})
            elif e["type"] == "MaterialAssigned":
                items.append({"node": "material", "batch_id": p["batch_id"],
                              "lot": p["lot"], "by": e["actor"]["id"], "ts": e["ts"]})
            elif e["type"] == "TechReviewed":
                items.append({"node": "tech_review", "result": p["result"],
                              "reviewer_id": p["reviewer_id"], "ts": e["ts"]})
            elif e["type"] == "FittingRecorded":
                items.append({"node": "fitting", "conclusion": p["conclusion"],
                              "doctor_id": p["doctor_id"], "ts": e["ts"]})
            elif e["type"] == "Delivered":
                items.append({"node": "delivery", "by": p["delivered_by"],
                              "instructions": p["instructions"], "ts": e["ts"]})
            elif e["type"] == "GuardianConfirmed":
                items.append({"node": "guardian_confirmation",
                              "guardian_id": p["guardian_id"], "ts": e["ts"]})
            elif e["type"] in ("DeviceTerminated", "DeviceSuperseded"):
                node = "device_terminated" if e["type"] == "DeviceTerminated" \
                    else "device_superseded"
                items.append({"node": node, "payload": p, "ts": e["ts"]})
        return items

    # -------------------------------------------------------------- 视图

    def full_history(self, actor, device_id):
        """任一器械最终都应能反查：扫描、处方、材料、制作人员、试戴、监护确认。"""
        dev = self._device_or_404(device_id)
        pat = self._patient_or_404(dev["patient_id"])
        self._require_device_access(actor, dev)

        current_rx = next(
            (v for v in reversed(dev["prescription_versions"])
             if v["version"] == dev["current_prescription_version"]),
            None,
        )
        return {
            "device_id": dev["device_id"],
            "patient": {"patient_id": pat["patient_id"], "name": pat["name"]},
            "scan": _public_scan(self.svc.scans[dev["scan_id"]]),
            "prescription": {
                "current_version": dev["current_prescription_version"],
                "current": current_rx and current_rx["details"],
                "versions": [
                    {"version": v["version"], "by": v["prescribed_by"],
                     "clinic_id": v["clinic_id"], "details": v["details"]}
                    for v in dev["prescription_versions"]
                ],
            },
            "fabrication": self._fabrication_block(dev),
            "fitting": dev["fitting"],
            "delivery": dev["delivery"],
            "guardian_confirmed": dev["guardian_confirmed"],
            "stage": dev["stage"],
            "remade_from": dev.get("remade_from"),
            "superseded_by": dev["superseded_by"],
            "timeline": self._timeline(dev),
            "event_count": len(self.svc.store.events(f"device:{dev['device_id']}")),
        }

    def _fabrication_block(self, dev):
        actors_by_version = {}
        for e in self.svc.store.events(f"device:{dev['device_id']}"):
            p = e["payload"]
            if e["type"] in ("ProductionScheduled", "MaterialAssigned",
                             "ProductionStarted", "ProductionFinished"):
                actors_by_version.setdefault(p["production_version"], {})[
                    {
                        "ProductionScheduled": "scheduled_by",
                        "MaterialAssigned": "material_by",
                        "ProductionStarted": "started_by",
                        "ProductionFinished": "finished_by",
                    }[e["type"]]
                ] = e["actor"]["id"]
        return [
            {
                "production_version": pv["version"],
                "prescription_version": pv["prescription_version"],
                "params": pv["params"],
                "material": pv["material"],
                "reviews": pv["reviews"],
                "started": pv["started"],
                "finished": pv["finished"],
                "personnel": actors_by_version.get(pv["version"], {}),
            }
            for pv in dev["production_versions"]
        ]

    def fabrication_work_order(self, actor, device_id):
        """加工方视图：只含完成制作所需，不含患者姓名、监护人等身份材料。"""
        dev = self._device_or_404(device_id)
        if actor.role != c.ROLE_FABRICATION_ADMIN and actor.role != c.ROLE_TECHNICIAN:
            raise AuthorizationError("仅加工方可查看加工单")
        if dev.get("fabrication_org_id") != actor.org_id:
            raise AuthorizationError("该件未分派给本加工方")
        if not dev["prescription_versions"]:
            raise NotFound("尚未形成加工任务")
        rx = next(
            v for v in reversed(dev["prescription_versions"])
            if v["version"] == dev["current_prescription_version"]
        )
        return {
            "device_id": dev["device_id"],
            "work_order": {
                "prescription_version": rx["version"],
                "details": rx["details"],
                "params": dev["production_versions"][-1]["params"]
                if dev["production_versions"] else None,
                "scan": {
                    "checksum": self.svc.scans[dev["scan_id"]]["checksum"],
                    "algorithm": self.svc.scans[dev["scan_id"]]["algorithm"],
                    # 影像本体引用不随加工单下发；加工方通过受控通道按校验值调取
                    "delivery": "controlled_pull_by_checksum",
                },
            },
            "stage": dev["stage"],
            "terminated": dev["terminated"],
        }

    def clinic_view(self, actor, patient_id):
        """接诊机构：授权范围内查看器械、交付说明与异常处置进度。"""
        self.svc._require_clinic_access(actor, patient_id)
        pat = self._patient_or_404(patient_id)
        devices = []
        for device_id in self.svc.devices_by_patient.get(patient_id, []):
            dev = self.svc.devices[device_id]
            devices.append({
                "device_id": device_id, "stage": dev["stage"],
                "scan_checksum": self.svc.scans[dev["scan_id"]]["checksum"],
                "current_prescription_version":
                    dev["current_prescription_version"],
                "material_batches": [
                    pv["material"]["batch_id"]
                    for pv in dev["production_versions"] if pv["material"]
                ],
                "fitting_conclusion": dev["fitting"] and dev["fitting"]["conclusion"],
                "delivery_instructions":
                    dev["delivery"] and dev["delivery"]["instructions"],
                "guardian_confirmed": dev["guardian_confirmed"],
                "terminated": dev["terminated"],
            })
        return {
            "patient_id": patient_id,
            "name": pat["name"],
            "devices": devices,
            "issues": [self._issue_view(i) for i in self.svc.open_issues(patient_id)],
        }

    def guardian_view(self, actor, patient_id):
        """监护人：交付与处置进度，不含加工参数等内部信息。"""
        pat = self._patient_or_404(patient_id)
        if actor.role != c.ROLE_GUARDIAN or actor.id != pat["guardian_id"]:
            raise AuthorizationError("仅该患者监护人可查看")
        devices = []
        for device_id in self.svc.devices_by_patient.get(patient_id, []):
            dev = self.svc.devices[device_id]
            devices.append({
                "device_id": device_id, "stage": dev["stage"],
                "instructions": dev["delivery"] and dev["delivery"]["instructions"],
                "guardian_confirmed": dev["guardian_confirmed"],
                "terminated": dev["terminated"],
            })
        return {
            "patient_id": patient_id,
            "devices": devices,
            "issues": [self._issue_view(i) for i in self.svc.open_issues(patient_id)],
        }

    def shipping_label(self, actor, device_id):
        """普通物流单：去标识化，不含影像与身份材料。"""
        dev = self._device_or_404(device_id)
        if actor.role not in (c.ROLE_FABRICATION_ADMIN, c.ROLE_TECHNICIAN,
                              c.ROLE_CLINIC_ADMIN, c.ROLE_PRESCRIBING_DOCTOR):
            raise AuthorizationError("无权生成物流单")
        return {
            "shipment_type": "dental_appliance",
            "reference": dev["device_id"],
            "destination_org": dev["clinic_id"],   # 机构代号，无患者身份
            "contains_scan_images": False,
            "contains_identity_documents": False,
        }

    def _issue_view(self, issue):
        return {
            "issue_id": issue["issue_id"], "device_id": issue["device_id"],
            "kind": issue["kind"], "status": issue["status"],
            "derived_status": self.svc.issue_status(issue["issue_id"]),
            "owner_id": issue["owner_id"], "opened_at": issue["opened_at"],
            "due_at": issue["due_at"], "risk_notice": issue["risk_notice"],
            "description": issue["description"],
        }

    def handover_pack(self, actor, handover_id):
        h = self.svc.handovers.get(handover_id)
        if not h:
            raise NotFound("交接单不存在")
        if actor.org_id not in (h["from_clinic_id"], h["to_clinic_id"]):
            raise AuthorizationError("仅交接双方机构可查看")
        return self._handover_summary(h)

    def _handover_summary(self, h):
        histories = []
        for device_id in h["device_ids"]:
            dev = self.svc.devices.get(device_id)
            if dev:
                histories.append(self._history_refs(dev))
        return {
            "handover_id": h["handover_id"], "patient_id": h["patient_id"],
            "from_clinic_id": h["from_clinic_id"],
            "to_clinic_id": h["to_clinic_id"], "status": h["status"],
            "device_ids": h["device_ids"],
            "device_history_refs": histories,
            "open_issues": h["open_issues"],
        }

    def _history_refs(self, dev):
        return {
            "device_id": dev["device_id"],
            "history": {
                "scan_id": dev["scan_id"],
                "prescription_version": dev["current_prescription_version"],
                "material_batches": [
                    pv["material"]["batch_id"]
                    for pv in dev["production_versions"] if pv["material"]
                ],
                "fitting": dev["fitting"] and {
                    "conclusion": dev["fitting"]["conclusion"],
                    "doctor_id": dev["fitting"]["doctor_id"],
                },
                "guardian_confirmed": dev["guardian_confirmed"],
            },
        }

    def _require_device_access(self, actor, dev):
        if actor.role in (c.ROLE_FABRICATION_ADMIN, c.ROLE_TECHNICIAN):
            if dev.get("fabrication_org_id") != actor.org_id:
                raise AuthorizationError("加工方仅可查被分派的器械")
            return
        self.svc._require_clinic_access(actor, dev["patient_id"])
