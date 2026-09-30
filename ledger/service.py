"""应用服务：儿童矫治器制作与交接的全部写操作与状态推导。

事件只追加、不修改；本服务把事件流归约成当前状态，并在每次命令前做规则校验。
"""

from datetime import timedelta

from . import catalog as c
from .errors import (
    AuthorizationError,
    Conflict,
    IdentityMismatch,
    NotFound,
    ValidationError,
)
from .store import new_id

DOCTOR_ROLES = {c.ROLE_PRESCRIBING_DOCTOR, c.ROLE_RECEIVING_DOCTOR}

SCAN_KIND_INTRAORAL = "intraoral_scan"
SCAN_KIND_PHOTO = "photo"   # 照片仅作参考，不能作为制作依据


def _redact_scan(scan):
    """命令响应中不回显影像本体存储位置（仍保留在校验事件里，受控调取）。"""
    return {k: v for k, v in scan.items() if k != "content_ref"}


def _due(now, days):
    return (now + timedelta(days=days)).isoformat()


class LedgerService:
    def __init__(self, store):
        self.store = store
        self._replay()

    # ------------------------------------------------------------------ 重建

    def _replay(self):
        self.orgs = {}
        self.patients = {}              # patient_id -> state
        self.name_index = {}            # 姓名或已确认别名 -> patient_id
        self.pending_aliases = {}       # (patient_id, alias) -> task
        self.scans = {}                 # scan_id -> state
        self.checksum_index = {}        # (alg, checksum) -> scan_id
        self.scan_devices = {}          # scan_id -> [device_id]
        self.devices = {}               # device_id -> state
        self.devices_by_patient = {}
        self.devices_by_batch = {}      # batch_id -> [device_id]
        self.issues = {}
        self.recalls = {}
        self.handovers = {}
        self.clinic_access = {}         # clinic_id -> {patient_id}
        for event in self.store.events():
            self._apply(event)

    def _append(self, event_type, stream, payload, actor):
        event = self.store.append(event_type, stream, payload, actor)
        self._apply(event)
        return event

    def _apply(self, e):
        p = e["payload"]
        t = e["type"]
        if t == "OrgRegistered":
            self.orgs[p["org_id"]] = p
        elif t == "PatientRegistered":
            self.patients[p["patient_id"]] = dict(
                p, aliases=[], pending_aliases=[],
                current_clinic_id=p["home_clinic_id"],
            )
            self.name_index[p["name"]] = p["patient_id"]
            self.clinic_access.setdefault(p["home_clinic_id"], set()).add(
                p["patient_id"]
            )
        elif t == "NameAliasProposed":
            self.patients[p["patient_id"]]["pending_aliases"].append(p["alias"])
            self.pending_aliases[(p["patient_id"], p["alias"])] = {
                "patient_id": p["patient_id"],
                "alias": p["alias"],
                "source_org_id": p.get("source_org_id"),
                "reason": p.get("reason"),
                "proposed_by": e["actor"]["id"],
                "ts": e["ts"],
            }
        elif t == "NameAliasConfirmed":
            pat = self.patients[p["patient_id"]]
            pat["pending_aliases"] = [a for a in pat["pending_aliases"] if a != p["alias"]]
            pat["aliases"].append(p["alias"])
            self.name_index[p["alias"]] = p["patient_id"]
            self.pending_aliases.pop((p["patient_id"], p["alias"]), None)
        elif t == "NameAliasRejected":
            pat = self.patients[p["patient_id"]]
            pat["pending_aliases"] = [a for a in pat["pending_aliases"] if a != p["alias"]]
            self.pending_aliases.pop((p["patient_id"], p["alias"]), None)
        elif t == "ScanUploaded":
            self.scans[p["scan_id"]] = dict(
                p, status="uploaded", identity_confirmed=False
            )
            self.checksum_index[(p["algorithm"], p["checksum"])] = p["scan_id"]
        elif t == "ScanIdentityConfirmed":
            scan = self.scans[p["scan_id"]]
            scan["identity_confirmed"] = True
            if p.get("patient_id"):
                scan["patient_id"] = p["patient_id"]
        elif t == "DeviceCreated":
            self.devices[p["device_id"]] = {
                "device_id": p["device_id"],
                "patient_id": p["patient_id"],
                "scan_id": p["scan_id"],
                "clinic_id": p["clinic_id"],
                "stage": c.STAGE_SCAN_RECEIVED,
                "prescription_versions": [],
                "current_prescription_version": 0,
                "changes": {},
                "production_versions": [],
                "fitting": None,
                "delivery": None,
                "guardian_confirmed": False,
                "terminated": False,
                "superseded_by": None,
                "remade_from": p.get("remade_from"),
            }
            self.scan_devices.setdefault(p["scan_id"], []).append(p["device_id"])
            self.devices_by_patient.setdefault(p["patient_id"], []).append(p["device_id"])
        elif t == "PrescriptionRecorded":
            dev = self.devices[p["device_id"]]
            dev["prescription_versions"].append(p)
            dev["current_prescription_version"] = p["version"]
            if not dev["production_versions"]:
                dev["stage"] = c.STAGE_PRESCRIBED
        elif t == "ChangeProposed":
            self.devices[p["device_id"]]["changes"][p["change_id"]] = {
                "change_id": p["change_id"],
                "status": "pending",
                "proposed_version": p["new_version"],
                "snapshot": p["snapshot"],
                "reason": p.get("reason"),
                "proposed_by": e["actor"]["id"],
                "ts": e["ts"],
            }
        elif t == "ChangeDecided":
            dev = self.devices[p["device_id"]]
            change = dev["changes"][p["change_id"]]
            change.update(
                status="decided",
                disposition=p["disposition"],
                reason=p.get("reason"),
                approvals=p["approvals"],
                decided_ts=e["ts"],
            )
            if p["disposition"] in (c.DISPOSITION_REWORK, c.DISPOSITION_CONTINUE):
                # 返工：快照成为新的不可改处方版本；继续使用：旧版本保持现行
                if p["disposition"] == c.DISPOSITION_REWORK:
                    dev["prescription_versions"].append({
                        "device_id": dev["device_id"],
                        "version": change["proposed_version"],
                        "details": change["snapshot"],
                        "prescribed_by": change["proposed_by"],
                        "clinic_id": dev["clinic_id"],
                        "via_change_id": change["change_id"],
                    })
                    dev["current_prescription_version"] = change["proposed_version"]
        elif t == "ProductionScheduled":
            dev = self.devices[p["device_id"]]
            dev["stage"] = c.STAGE_SCHEDULED
            dev["fabrication_org_id"] = p["fabrication_org_id"]
            dev["production_versions"].append(
                {
                    "version": p["production_version"],
                    "prescription_version": p["prescription_version"],
                    "params": p["params"],
                    "material": None,
                    "reviews": [],
                    "started": None,
                    "finished": None,
                }
            )
        elif t == "MaterialAssigned":
            pv = self._pv(self.devices[p["device_id"]], p["production_version"])
            pv["material"] = {k: p[k] for k in ("batch_id", "name", "supplier", "lot")}
            self.devices_by_batch.setdefault(p["batch_id"], []).append(p["device_id"])
        elif t == "ProductionStarted":
            dev = self.devices[p["device_id"]]
            dev["stage"] = c.STAGE_IN_PRODUCTION
            self._pv(dev, p["production_version"])["started"] = e["ts"]
        elif t == "TechReviewed":
            dev = self.devices[p["device_id"]]
            self._pv(dev, p["production_version"])["reviews"].append(dict(p, ts=e["ts"]))
        elif t == "ProductionFinished":
            dev = self.devices[p["device_id"]]
            dev["stage"] = c.STAGE_FINISHED
            self._pv(dev, p["production_version"])["finished"] = e["ts"]
        elif t == "FittingRecorded":
            dev = self.devices[p["device_id"]]
            dev["stage"] = c.STAGE_FITTED
            dev["fitting"] = dict(p, ts=e["ts"])
        elif t == "Delivered":
            dev = self.devices[p["device_id"]]
            dev["stage"] = c.STAGE_DELIVERED
            dev["delivery"] = dict(p, ts=e["ts"])
        elif t == "GuardianConfirmed":
            self.devices[p["device_id"]]["guardian_confirmed"] = True
        elif t == "DeviceTerminated":
            dev = self.devices[p["device_id"]]
            dev["terminated"] = True
            dev["stage"] = c.STAGE_TERMINATED
            dev["termination"] = dict(p, ts=e["ts"])
        elif t == "DeviceSuperseded":
            self.devices[p["device_id"]]["superseded_by"] = p["by_device_id"]
            self.devices[p["device_id"]]["stage"] = c.STAGE_SUPERSEDED
        elif t == "IssueOpened":
            self.issues[p["issue_id"]] = dict(
                p, status=c.ISSUE_OPEN, actions=[], resolution=None
            )
        elif t == "IssueActionTaken":
            self.issues[p["issue_id"]]["status"] = c.ISSUE_ACTION_TAKEN
            self.issues[p["issue_id"]]["actions"].append(dict(p, ts=e["ts"]))
        elif t == "IssueResolved":
            issue = self.issues[p["issue_id"]]
            issue["status"] = c.ISSUE_RESOLVED
            issue["resolution"] = dict(p, ts=e["ts"])
        elif t == "RecallOpened":
            self.recalls[p["recall_id"]] = dict(p, ts=e["ts"], affected=[], issue_ids=[])
        elif t == "RecallScopeLocked":
            recall = self.recalls[p["recall_id"]]
            recall["affected"] = p["affected_device_ids"]
            recall["issue_ids"] = p["issue_ids"]
        elif t == "HandoverInitiated":
            self.handovers[p["handover_id"]] = dict(
                p, status=c.HANDOVER_PENDING
            )
        elif t == "HandoverAccepted":
            h = self.handovers[p["handover_id"]]
            h["status"] = c.HANDOVER_ACCEPTED
            h["accepted_ts"] = e["ts"]
            self.clinic_access.setdefault(h["to_clinic_id"], set()).add(
                h["patient_id"]
            )
            self.patients[h["patient_id"]]["current_clinic_id"] = h["to_clinic_id"]
        elif t == "HandoverRejected":
            h = self.handovers[p["handover_id"]]
            h["status"] = c.HANDOVER_REJECTED
            h["reject_reason"] = p.get("reason")

    @staticmethod
    def _pv(device, version):
        for pv in device["production_versions"]:
            if pv["version"] == version:
                return pv
        raise NotFound(f"生产版本 {version} 不存在")

    # ------------------------------------------------------------------ 权限

    @staticmethod
    def _require_role(actor, roles, hint=""):
        if actor.role not in roles:
            raise AuthorizationError(f"角色 {actor.role} 无权执行该操作。{hint}")

    @staticmethod
    def _require_fabrication(actor, dev):
        if dev.get("fabrication_org_id") != actor.org_id:
            raise AuthorizationError("仅承担该件的加工方可操作生产数据")

    def _patient(self, patient_id):
        pat = self.patients.get(patient_id)
        if not pat:
            raise NotFound(f"患者 {patient_id} 不存在")
        return pat

    def _device(self, device_id):
        dev = self.devices.get(device_id)
        if not dev:
            raise NotFound(f"器械 {device_id} 不存在")
        return dev

    def _active_device(self, device_id):
        dev = self._device(device_id)
        if dev["terminated"]:
            raise Conflict(f"器械 {device_id} 已终止，不能继续制作动作")
        return dev

    def _require_clinic_access(self, actor, patient_id):
        if actor.role == c.ROLE_GUARDIAN:
            pat = self._patient(patient_id)
            if actor.id != pat["guardian_id"]:
                raise AuthorizationError("监护人信息不匹配")
            return
        if actor.org_id and patient_id in self.clinic_access.get(actor.org_id, set()):
            return
        raise AuthorizationError("机构未获得该患者的授权")

    # ------------------------------------------------------------- 机构/患者

    def register_org(self, actor, org_id, name, kind):
        self._require_role(
            actor, {c.ROLE_CLINIC_ADMIN, c.ROLE_FABRICATION_ADMIN},
            hint="（机构登记限管理角色）",
        )
        if org_id in self.orgs:
            raise Conflict("机构已存在")
        if kind not in ("clinic", "fabrication"):
            raise ValidationError("kind 必须为 clinic 或 fabrication")
        expected = "clinic_admin" if kind == "clinic" else "fabrication_admin"
        if actor.role != expected:
            raise AuthorizationError(f"{kind} 机构须由 {expected} 登记")
        self._append(
            "OrgRegistered", f"org:{org_id}",
            {"org_id": org_id, "name": name, "kind": kind}, actor,
        )
        return self.orgs[org_id]

    def register_patient(self, actor, patient_id, name, guardian_id,
                         guardian_name, home_clinic_id):
        self._require_role(actor, {c.ROLE_CLINIC_ADMIN, c.ROLE_PRESCRIBING_DOCTOR})
        if patient_id in self.patients:
            raise Conflict("患者已登记")
        if home_clinic_id not in self.orgs:
            raise ValidationError("归属门诊未登记")
        self._append(
            "PatientRegistered", f"patient:{patient_id}",
            {
                "patient_id": patient_id, "name": name,
                "guardian_id": guardian_id, "guardian_name": guardian_name,
                "home_clinic_id": home_clinic_id,
            },
            actor,
        )
        return self.patients[patient_id]

    def propose_name_alias(self, actor, patient_id, alias, reason=""):
        """姓名转写差异（拼音/外文名等）只登记，不自动并档。"""
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        pat = self._patient(patient_id)
        if alias == pat["name"] or alias in pat["aliases"]:
            return {"status": "already_confirmed", "alias": alias}
        key = (patient_id, alias)
        if key in self.pending_aliases:
            return {"status": "pending", "alias": alias}
        self._append(
            "NameAliasProposed", f"patient:{patient_id}",
            {"patient_id": patient_id, "alias": alias,
             "source_org_id": actor.org_id, "reason": reason},
            actor,
        )
        return {"status": "pending", "alias": alias}

    def resolve_name_alias(self, actor, patient_id, alias, approve):
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        if (patient_id, alias) not in self.pending_aliases:
            raise NotFound("没有待确认的姓名差异")
        etype = "NameAliasConfirmed" if approve else "NameAliasRejected"
        self._append(
            etype, f"patient:{patient_id}",
            {"patient_id": patient_id, "alias": alias,
             "confirmed_by": actor.id},
            actor,
        )
        return {"status": "confirmed" if approve else "rejected", "alias": alias}

    def pending_name_confirmations(self, actor):
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        return list(self.pending_aliases.values())

    # ------------------------------------------------------------------ 扫描

    def upload_scan(self, actor, checksum, algorithm, patient_id=None,
                    kind=SCAN_KIND_INTRAORAL, name_on_scan=None,
                    content_ref=None, reason=""):
        """上传数字印模。同一校验值永远只对应一份扫描，绝不生成两件器械。"""
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        if not checksum or not algorithm:
            raise ValidationError("缺少印模校验值或算法")

        existing_id = self.checksum_index.get((algorithm, checksum))
        if existing_id:
            existing = self.scans[existing_id]
            result = {"status": "duplicate", "scan": _redact_scan(existing)}
            if patient_id and existing.get("patient_id") \
                    and patient_id != existing["patient_id"]:
                # 同一份扫描被指向不同患者：可能是同名转写，也可能是误传，必须人工确认
                self.propose_name_alias(
                    actor, existing["patient_id"],
                    name_on_scan or self._patient(patient_id)["name"],
                    reason="同校验值扫描关联到不同身份",
                )
                raise IdentityMismatch(
                    "扫描校验值已存在但归属患者不同，已转人工确认，禁止据此创建器械",
                    details={"existing_scan_id": existing_id,
                             "existing_patient_id": existing["patient_id"]},
                )
            return result

        pat = self._patient(patient_id) if patient_id else None
        identity_confirmed = True
        if pat and name_on_scan and name_on_scan != pat["name"] \
                and name_on_scan not in pat["aliases"]:
            # 姓名转写差异：挂起，等待人工确认，不阻断扫描存档但阻断建件
            self.propose_name_alias(
                actor, pat["patient_id"], name_on_scan,
                reason=reason or "扫描文件姓名与登记姓名不一致",
            )
            identity_confirmed = False

        scan_id = new_id("scan")
        self._append(
            "ScanUploaded", f"scan:{scan_id}",
            {"scan_id": scan_id, "patient_id": patient_id,
             "checksum": checksum, "algorithm": algorithm, "kind": kind,
             "content_ref": content_ref, "name_on_scan": name_on_scan,
             "uploaded_by_org": actor.org_id},
            actor,
        )
        if identity_confirmed:
            self._append(
                "ScanIdentityConfirmed", f"scan:{scan_id}",
                {"scan_id": scan_id, "patient_id": patient_id,
                 "confirmed_by": actor.id},
                actor,
            )
        return {"status": "uploaded" if identity_confirmed else "awaiting_identity",
                "scan": _redact_scan(self.scans[scan_id])}

    def confirm_scan_identity(self, actor, scan_id, patient_id):
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        scan = self.scans.get(scan_id)
        if not scan:
            raise NotFound("扫描不存在")
        self._patient(patient_id)
        self._append(
            "ScanIdentityConfirmed", f"scan:{scan_id}",
            {"scan_id": scan_id, "patient_id": patient_id,
             "confirmed_by": actor.id},
            actor,
        )
        return _redact_scan(self.scans[scan_id])

    def verify_scan_for_device(self, actor, device_id, checksum, algorithm):
        """跨院复诊：用家长带来/新门诊重扫文件的校验值比对原件。"""
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        self._require_clinic_access(actor, self._device(device_id)["patient_id"])
        dev = self._device(device_id)
        original = self.scans[dev["scan_id"]]
        matched = (original["checksum"], original["algorithm"]) == (checksum, algorithm)
        return {
            "matched": matched,
            "device_id": device_id,
            "original_scan": {
                "scan_id": original["scan_id"], "kind": original["kind"],
                "checksum": original["checksum"], "algorithm": original["algorithm"],
            },
            "notice": None if matched else "校验值不一致：不得照旧重做，需医生评估重新取模或调取原件",
        }

    # ------------------------------------------------------------------ 器械

    def create_device(self, actor, patient_id, scan_id, remade_from=None):
        """显式建件。旧件终止后的重制需传入 remade_from，谱系可追溯。"""
        self._require_role(actor, {c.ROLE_PRESCRIBING_DOCTOR, c.ROLE_RECEIVING_DOCTOR})
        pat = self._patient(patient_id)
        self._require_clinic_access(actor, patient_id)
        scan = self.scans.get(scan_id)
        if not scan:
            raise NotFound("扫描不存在")
        if scan["kind"] != SCAN_KIND_INTRAORAL:
            raise ValidationError(
                "照片不能作为矫治器制作依据，需口内扫描数字印模或经授权调取原始扫描",
            )
        if not scan.get("identity_confirmed"):
            raise IdentityMismatch("扫描身份尚未经人工确认，不能建件")
        if scan.get("patient_id") and scan["patient_id"] != patient_id:
            raise IdentityMismatch("扫描归属患者与申请患者不一致")

        # 一份扫描只能支撑一条在役器械谱系；旧谱系终止/被取代后，才允许显式批准的重制
        for other_id in self.scan_devices.get(scan_id, []):
            other = self.devices[other_id]
            if not other["terminated"] and other["superseded_by"] is None:
                raise Conflict(
                    "该扫描已关联在役器械，重复扫描不得生成第二件器械",
                )

        if remade_from:
            prior = self.devices.get(remade_from)
            if not prior or prior["patient_id"] != patient_id \
                    or prior["scan_id"] != scan_id:
                raise ValidationError("remade_from 必须指向同患者同扫描的既有器械")
            if not prior["terminated"]:
                raise Conflict("重制前旧件必须先经批准终止")

        device_id = new_id("dev")
        self._append(
            "DeviceCreated", f"device:{device_id}",
            {"device_id": device_id, "patient_id": patient_id,
             "scan_id": scan_id, "clinic_id": pat["current_clinic_id"],
             "remade_from": remade_from},
            actor,
        )
        return self.devices[device_id]

    def record_prescription(self, actor, device_id, details):
        """排产前记录处方；每次追加不可变版本。"""
        self._require_role(actor, DOCTOR_ROLES)
        dev = self._active_device(device_id)
        if actor.org_id != dev["clinic_id"]:
            raise AuthorizationError("仅该件归属门诊可记录处方")
        if dev["production_versions"]:
            raise Conflict("已排产，处方变更必须走变更申请与批准流程")
        version = len(dev["prescription_versions"]) + 1
        self._append(
            "PrescriptionRecorded", f"device:{device_id}",
            {"device_id": device_id, "version": version, "details": details,
             "prescribed_by": actor.id, "clinic_id": actor.org_id},
            actor,
        )
        return dev["prescription_versions"][-1]

    def schedule_production(self, actor, device_id, fabrication_org_id, params):
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        dev = self._active_device(device_id)
        if not dev["prescription_versions"]:
            raise ValidationError("尚无医生处方，不能排产")
        if fabrication_org_id not in self.orgs or \
                self.orgs[fabrication_org_id]["kind"] != "fabrication":
            raise ValidationError("加工方未登记")
        rework_count = sum(
            1 for ch in dev["changes"].values()
            if ch.get("disposition") == c.DISPOSITION_REWORK
        )
        if len(dev["production_versions"]) > rework_count:
            raise Conflict("已存在生产任务；返工裁决后才能重新排产")
        production_version = len(dev["production_versions"]) + 1
        self._append(
            "ProductionScheduled", f"device:{device_id}",
            {"device_id": device_id, "production_version": production_version,
             "prescription_version": dev["current_prescription_version"],
             "fabrication_org_id": fabrication_org_id, "params": params},
            actor,
        )
        return dev["production_versions"][-1]

    def assign_material(self, actor, device_id, batch_id, name, supplier, lot):
        self._require_role(actor, {c.ROLE_FABRICATION_ADMIN, c.ROLE_TECHNICIAN})
        dev = self._active_device(device_id)
        self._require_fabrication(actor, dev)
        if not dev["production_versions"]:
            raise Conflict("尚未排产")
        self._append(
            "MaterialAssigned", f"device:{device_id}",
            {"device_id": device_id,
             "production_version": dev["production_versions"][-1]["version"],
             "batch_id": batch_id, "name": name, "supplier": supplier, "lot": lot},
            actor,
        )
        return dev["production_versions"][-1]["material"]

    def start_production(self, actor, device_id):
        self._require_role(actor, {c.ROLE_FABRICATION_ADMIN, c.ROLE_TECHNICIAN})
        dev = self._active_device(device_id)
        self._require_fabrication(actor, dev)
        pv = dev["production_versions"][-1]
        if pv["material"] is None:
            raise ValidationError("未登记材料批次不能开工")
        if pv["started"]:
            raise Conflict("该生产版本已开工")
        self._append(
            "ProductionStarted", f"device:{device_id}",
            {"device_id": device_id, "production_version": pv["version"]},
            actor,
        )
        return {"stage": dev["stage"]}

    def tech_review(self, actor, device_id, result, notes=""):
        """技师复核。返工要求同样追加记录，不抹掉前一次复核。"""
        self._require_role(actor, {c.ROLE_TECHNICIAN})
        dev = self._active_device(device_id)
        self._require_fabrication(actor, dev)
        if result not in ("pass", "rework"):
            raise ValidationError("result 必须为 pass 或 rework")
        pv = dev["production_versions"][-1]
        if not pv["started"]:
            raise Conflict("尚未开工")
        self._append(
            "TechReviewed", f"device:{device_id}",
            {"device_id": device_id, "production_version": pv["version"],
             "result": result, "reviewer_id": actor.id, "notes": notes},
            actor,
        )
        if result == "pass" and dev["stage"] == c.STAGE_IN_PRODUCTION:
            dev["stage"] = c.STAGE_TECH_REVIEWED
        return dev["production_versions"][-1]["reviews"][-1]

    def finish_production(self, actor, device_id):
        self._require_role(actor, {c.ROLE_FABRICATION_ADMIN, c.ROLE_TECHNICIAN})
        dev = self._active_device(device_id)
        self._require_fabrication(actor, dev)
        pv = dev["production_versions"][-1]
        reviews = pv["reviews"]
        if not reviews or reviews[-1]["result"] != "pass":
            raise ValidationError("技师复核未通过，不能报成品")
        self._append(
            "ProductionFinished", f"device:{device_id}",
            {"device_id": device_id, "production_version": pv["version"]},
            actor,
        )
        return {"stage": dev["stage"]}

    def record_fitting(self, actor, device_id, conclusion, notes=""):
        """试戴结论由医生记录；系统不给出临床判断。"""
        self._require_role(actor, DOCTOR_ROLES)
        dev = self._active_device(device_id)
        self._require_clinic_access(actor, dev["patient_id"])
        if dev["stage"] not in (c.STAGE_FINISHED, c.STAGE_FITTED):
            raise Conflict("成品完成后才能记录试戴")
        if conclusion not in ("accepted", "adjusted_then_accepted", "rejected"):
            raise ValidationError("试戴结论取值不合法")
        self._append(
            "FittingRecorded", f"device:{device_id}",
            {"device_id": device_id, "conclusion": conclusion,
             "doctor_id": actor.id, "clinic_id": actor.org_id, "notes": notes},
            actor,
        )
        return dev["fitting"]

    def deliver(self, actor, device_id, instructions):
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        dev = self._active_device(device_id)
        self._require_clinic_access(actor, dev["patient_id"])
        fitting = dev["fitting"]
        if not fitting or fitting["conclusion"] == "rejected":
            raise Conflict("试戴未通过，不能交付")
        self._append(
            "Delivered", f"device:{device_id}",
            {"device_id": device_id, "instructions": instructions,
             "delivered_by": actor.id, "clinic_id": actor.org_id},
            actor,
        )
        return dev["delivery"]

    def guardian_confirm(self, actor, device_id, note=""):
        self._require_role(actor, {c.ROLE_GUARDIAN})
        dev = self._device(device_id)
        pat = self._patient(dev["patient_id"])
        if actor.id != pat["guardian_id"]:
            raise AuthorizationError("仅登记监护人可确认")
        if not dev["delivery"]:
            raise Conflict("器械尚未交付")
        self._append(
            "GuardianConfirmed", f"device:{device_id}",
            {"device_id": device_id, "guardian_id": actor.id, "note": note},
            actor,
        )
        return {"guardian_confirmed": True}

    # ------------------------------------------------------------- 处方变更

    def propose_prescription_change(self, actor, device_id, snapshot, reason=""):
        self._require_role(actor, DOCTOR_ROLES)
        dev = self._active_device(device_id)
        if actor.org_id != dev["clinic_id"]:
            raise AuthorizationError("仅该件归属门诊可发起变更")
        if any(ch["status"] == "pending" for ch in dev["changes"].values()):
            raise Conflict("该器械已有待裁决的变更申请")
        new_version = len(dev["prescription_versions"]) + 1
        change_id = new_id("chg")
        self._append(
            "ChangeProposed", f"device:{device_id}",
            {"change_id": change_id, "device_id": device_id,
             "new_version": new_version, "snapshot": snapshot,
             "reason": reason},
            actor,
        )
        return dev["changes"][change_id]

    def decide_prescription_change(self, actor, change_id, disposition,
                                   approvals, reason=""):
        """对排产/成品之后的变更作出终止、返工或继续使用的裁决。

        approvals: [{"approver_id","role","org_id"}]，按阶段要求双人批准；
        裁决与批准人全部留痕，已发生的制作事实不被覆盖。
        """
        dev, change = self._find_change(change_id)
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        if change["status"] != "pending":
            raise Conflict("该变更已有裁决")
        stage = dev["stage"]
        allowed = self._allowed_dispositions(stage)
        if disposition not in allowed:
            raise Conflict(
                f"当前阶段 {stage} 不允许 {disposition}，允许：{sorted(allowed)}",
            )
        self._check_approvals(stage, disposition, approvals)

        if disposition == c.DISPOSITION_TERMINATE:
            self._decide(actor, dev, change, disposition, approvals, reason)
            self._append(
                "DeviceTerminated", f"device:{dev['device_id']}",
                {"device_id": dev["device_id"], "change_id": change_id,
                 "reason": reason or change.get("reason"),
                 "terminated_by": actor.id},
                actor,
            )
        elif disposition == c.DISPOSITION_REWORK:
            self._decide(actor, dev, change, disposition, approvals, reason)
        elif disposition == c.DISPOSITION_CONTINUE:
            if not reason:
                raise ValidationError("成品后继续使用必须填写书面理由")
            self._decide(actor, dev, change, disposition, approvals, reason)
        return {"change": change}

    def _decide(self, actor, dev, change, disposition, approvals, reason):
        self._append(
            "ChangeDecided", f"device:{dev['device_id']}",
            {"change_id": change["change_id"], "device_id": dev["device_id"],
             "disposition": disposition, "approvals": approvals, "reason": reason},
            actor,
        )

    def _find_change(self, change_id):
        for dev in self.devices.values():
            if change_id in dev["changes"]:
                return dev, dev["changes"][change_id]
        raise NotFound("变更申请不存在")

    @staticmethod
    def _allowed_dispositions(stage):
        if stage in (c.STAGE_SCHEDULED, c.STAGE_IN_PRODUCTION, c.STAGE_TECH_REVIEWED):
            return c.DISPOSITIONS_AFTER_SCHEDULE
        if stage in (c.STAGE_FINISHED, c.STAGE_FITTED, c.STAGE_DELIVERED):
            return c.DISPOSITIONS_AFTER_FINISH
        return c.DISPOSITIONS_BEFORE_SCHEDULE

    @staticmethod
    def _check_approvals(stage, disposition, approvals):
        roles = {a.get("role") for a in approvals}
        if stage in (c.STAGE_SCHEDULED, c.STAGE_IN_PRODUCTION, c.STAGE_TECH_REVIEWED):
            # 医学签批（处方或接诊医生）+ 加工方负责人
            if not (roles & DOCTOR_ROLES) or c.ROLE_FABRICATION_ADMIN not in roles:
                raise AuthorizationError(
                    "排产之后的裁决需医生与加工方负责人共同批准",
                )
        elif stage in (c.STAGE_FINISHED, c.STAGE_FITTED, c.STAGE_DELIVERED):
            if not (roles & DOCTOR_ROLES) or c.ROLE_CLINIC_ADMIN not in roles:
                raise AuthorizationError(
                    "成品之后的裁决需医生与门诊负责人共同批准",
                )
            if disposition == c.DISPOSITION_TERMINATE and len(approvals) < 2:
                raise AuthorizationError("成品终止需双签")
        else:
            if not (roles & DOCTOR_ROLES):
                raise AuthorizationError("需医生批准")

    # ------------------------------------------------------------------ 异常

    def open_issue(self, actor, device_id, kind, description="", owner_id=None,
                   _internal=False):
        allowed_roles = DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN, c.ROLE_GUARDIAN}
        if _internal and kind == c.ISSUE_BATCH_RECALL:
            # 批次召回可由加工方/门诊管理员发起，逐件处置单由内部生成
            allowed_roles |= {c.ROLE_FABRICATION_ADMIN}
        self._require_role(actor, allowed_roles)
        if kind not in c.SLA_DAYS:
            raise ValidationError(f"异常类型 {kind} 不支持")
        dev = self._device(device_id)
        if kind != c.ISSUE_BATCH_RECALL and dev["terminated"]:
            raise Conflict("器械已终止")
        if not _internal:
            self._require_clinic_access(actor, dev["patient_id"])
        issue_id = new_id("iss")
        now = self.store.clock()
        payload = {
            "issue_id": issue_id, "device_id": device_id,
            "patient_id": dev["patient_id"], "kind": kind,
            "description": description,
            "reported_by": actor.id, "reported_by_org": actor.org_id,
            "owner_id": owner_id or actor.id,
            "opened_at": now.isoformat(),
            "due_at": _due(now, c.SLA_DAYS[kind]),
            "risk_notice": c.RISK_NOTICE[kind],
        }
        self._append("IssueOpened", f"issue:{issue_id}", payload, actor)
        return self.issues[issue_id]

    def record_issue_action(self, actor, issue_id, action):
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        issue = self._issue(issue_id)
        self._require_clinic_access(actor, issue["patient_id"])
        self._append(
            "IssueActionTaken", f"issue:{issue_id}",
            {"issue_id": issue_id, "action": action, "actor_id": actor.id},
            actor,
        )
        return self.issues[issue_id]

    def resolve_issue(self, actor, issue_id, resolution, clinical_conclusion=None):
        issue = self._issue(issue_id)
        if issue["kind"] == c.ISSUE_ALLERGY_SUSPECTED:
            # 过敏结论只能由医生作出，系统只记录不判断
            self._require_role(actor, {c.ROLE_PRESCRIBING_DOCTOR, c.ROLE_RECEIVING_DOCTOR})
            if not clinical_conclusion:
                raise ValidationError("过敏疑点必须记录医生的临床结论后方可结案")
        else:
            self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        self._require_clinic_access(actor, issue["patient_id"])
        self._append(
            "IssueResolved", f"issue:{issue_id}",
            {"issue_id": issue_id, "resolution": resolution,
             "clinical_conclusion": clinical_conclusion,
             "resolved_by": actor.id},
            actor,
        )
        return self.issues[issue_id]

    def _issue(self, issue_id):
        issue = self.issues.get(issue_id)
        if not issue:
            raise NotFound("异常单不存在")
        if issue["status"] == c.ISSUE_RESOLVED:
            raise Conflict("异常已结案")
        return issue

    def issue_status(self, issue_id, now=None):
        issue = self.issues.get(issue_id)
        if not issue:
            raise NotFound("异常单不存在")
        if issue["status"] == c.ISSUE_RESOLVED:
            return "resolved"
        moment = (now or self.store.clock()).isoformat()
        return "overdue" if moment > issue["due_at"] else issue["status"]

    def open_issues(self, patient_id=None):
        items = [i for i in self.issues.values() if i["status"] != c.ISSUE_RESOLVED]
        if patient_id:
            items = [i for i in items if i["patient_id"] == patient_id]
        for issue in items:
            issue = dict(issue)
            issue["derived_status"] = self.issue_status(issue["issue_id"])
            yield issue

    def open_recall(self, actor, batch_id, reason):
        """批次召回：锁定受影响范围，逐件生成带期限的处置单，不给临床结论。"""
        self._require_role(actor, {c.ROLE_CLINIC_ADMIN, c.ROLE_FABRICATION_ADMIN})
        recall_id = new_id("rec")
        affected = list(dict.fromkeys(
            d for d in self.devices_by_batch.get(batch_id, [])
            if not self.devices[d]["terminated"]
            and self.devices[d]["superseded_by"] is None
        ))
        self._append(
            "RecallOpened", f"recall:{recall_id}",
            {"recall_id": recall_id, "batch_id": batch_id, "reason": reason,
               "opened_by": actor.id},
            actor,
        )
        issue_ids = []
        for device_id in affected:
            issue = self.open_issue(
                actor, device_id, c.ISSUE_BATCH_RECALL,
                description=f"批次 {batch_id} 召回：{reason}",
                _internal=True,
            )
            issue_ids.append(issue["issue_id"])
        self._append(
            "RecallScopeLocked", f"recall:{recall_id}",
            {"recall_id": recall_id, "affected_device_ids": affected,
             "issue_ids": issue_ids},
            actor,
        )
        return {"recall_id": recall_id, "batch_id": batch_id,
                "affected_device_ids": affected, "issue_ids": issue_ids,
                "risk_notice": c.RISK_NOTICE[c.ISSUE_BATCH_RECALL]}

    # ------------------------------------------------------------------ 交接

    def initiate_handover(self, actor, patient_id, to_clinic_id):
        """换院：快照未结异常（责任人+截止时间原样带走，期限不重置）。"""
        self._require_role(actor, DOCTOR_ROLES | {c.ROLE_CLINIC_ADMIN})
        pat = self._patient(patient_id)
        if actor.org_id != pat["current_clinic_id"]:
            raise AuthorizationError("仅当前接诊机构可发起交接")
        if to_clinic_id not in self.orgs or self.orgs[to_clinic_id]["kind"] != "clinic":
            raise ValidationError("接收机构不是已登记门诊")
        existing = [h for h in self.handovers.values()
                    if h["patient_id"] == patient_id
                    and h["status"] == c.HANDOVER_PENDING]
        if existing:
            raise Conflict("该患者已有待接收的交接单")
        open_issues = [
            {
                "issue_id": i["issue_id"], "device_id": i["device_id"],
                "kind": i["kind"], "owner_id": i["owner_id"],
                "opened_at": i["opened_at"], "due_at": i["due_at"],
                "description": i["description"],
            }
            for i in self.open_issues(patient_id)
        ]
        device_ids = list(self.devices_by_patient.get(patient_id, []))
        handover_id = new_id("hov")
        self._append(
            "HandoverInitiated", f"handover:{handover_id}",
            {"handover_id": handover_id, "patient_id": patient_id,
             "from_clinic_id": actor.org_id, "to_clinic_id": to_clinic_id,
             "device_ids": device_ids, "open_issues": open_issues,
             "initiated_by": actor.id},
            actor,
        )
        return self.handovers[handover_id]

    def accept_handover(self, actor, handover_id):
        h = self.handovers.get(handover_id)
        if not h:
            raise NotFound("交接单不存在")
        if actor.org_id != h["to_clinic_id"]:
            raise AuthorizationError("仅接收机构可接收")
        if h["status"] != c.HANDOVER_PENDING:
            raise Conflict("交接单已处理")
        self._append(
            "HandoverAccepted", f"handover:{handover_id}",
            {"handover_id": handover_id, "accepted_by": actor.id},
            actor,
        )
        return self.handovers[handover_id]

    def reject_handover(self, actor, handover_id, reason):
        h = self.handovers.get(handover_id)
        if not h:
            raise NotFound("交接单不存在")
        if actor.org_id != h["to_clinic_id"]:
            raise AuthorizationError("仅接收机构可拒收")
        self._append(
            "HandoverRejected", f"handover:{handover_id}",
            {"handover_id": handover_id, "reason": reason,
             "rejected_by": actor.id},
            actor,
        )
        return self.handovers[handover_id]
