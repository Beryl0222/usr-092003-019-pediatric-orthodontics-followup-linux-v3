"""儿童活动矫治器制作与交接履历核心。

领域模型与传输框架无关：所有写操作都是显式的、带操作人的业务动作，
扫描、处方版本、加工参数、材料批次、复核、试戴与交接只追加、不覆盖。
系统只记录事实、提示风险与受影响范围，不代替医生作临床判断。
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta, timezone
from itertools import count

# ---- 角色 -----------------------------------------------------------------

ROLE_DOCTOR = "DOCTOR"                # 医生（处方、试戴、临床决定）
ROLE_CLINIC_STAFF = "CLINIC_STAFF"    # 接诊机构工作人员
ROLE_TECHNICIAN = "TECHNICIAN"        # 加工技师
ROLE_LAB_ADMIN = "LAB_ADMIN"          # 加工中心负责人
ROLE_GUARDIAN = "GUARDIAN"            # 监护人

CLINIC_ROLES = {ROLE_DOCTOR, ROLE_CLINIC_STAFF}
LAB_ROLES = {ROLE_TECHNICIAN, ROLE_LAB_ADMIN}

# ---- 状态与策略 ------------------------------------------------------------

PATTERN_IDENTITY_PENDING = "IDENTITY_PENDING"
PATTERN_CONFIRMED = "CONFIRMED"
PATTERN_MERGED = "MERGED"

# 异常处置期限（自然日），仅为机构运营默认期限，不代表临床判断
EXCEPTION_DEADLINE_DAYS = {
    "LOSS": 10,
    "DAMAGE": 7,
    "ALLERGY_SUSPECTED": 7,
    "BATCH_RECALL": 15,
}
EXCEPTION_TYPES = set(EXCEPTION_DEADLINE_DAYS)

RUN_SCHEDULED = "SCHEDULED"
RUN_IN_PRODUCTION = "IN_PRODUCTION"
RUN_QC_PASS = "QC_PASS"
RUN_QC_FAIL = "QC_FAIL"
RUN_TERMINATED = "TERMINATED"
RUN_SUPERSEDED_BY_REWORK = "SUPERSEDED_BY_REWORK"

DEVICE_PLANNED = "PLANNED"
DEVICE_SCHEDULED = "SCHEDULED"
DEVICE_IN_PRODUCTION = "IN_PRODUCTION"
DEVICE_READY = "READY"
DEVICE_DELIVERED = "DELIVERED"
DEVICE_LOST = "LOST"
DEVICE_TERMINATED = "TERMINATED"

FIT_OK = "OK"
FIT_ADJUSTED = "ADJUSTED"
FIT_FAIL = "FAIL"

_CHECKSUM_RE = re.compile(r"^(sha256|sha1|sha512):[0-9a-f]+$")


# ---- 错误 ------------------------------------------------------------------


class LedgerError(Exception):
    http_status = 400


class ValidationError(LedgerError):
    http_status = 400


class AuthError(LedgerError):
    http_status = 403


class UnauthorizedError(LedgerError):
    http_status = 401


class NotFoundError(LedgerError):
    http_status = 404


class ConflictError(LedgerError):
    http_status = 409


class StateError(LedgerError):
    http_status = 422


# ---- 主服务 ----------------------------------------------------------------


class Ledger:
    def __init__(self, now_fn=None):
        self._lock = threading.RLock()
        self._seq = count(1)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.users: dict[str, dict] = {}
        self.orgs: dict[str, dict] = {}
        self.patients: dict[str, dict] = {}
        self.scans: dict[str, dict] = {}
        self.prescriptions: dict[str, dict] = {}
        self.devices: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.exceptions: dict[str, dict] = {}
        self.transfers: dict[str, dict] = {}
        self.shipments: dict[str, dict] = {}
        self.scan_conflicts: dict[str, dict] = {}
        # checksum -> [scan_id]
        self._scan_index: dict[str, list[str]] = {}
        # id_doc_hash -> patient_id
        self._patient_doc_index: dict[str, str] = {}
        self.audit: list[dict] = []

    # ---- 基础工具 ----

    def _id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq)}"

    def now(self) -> datetime:
        return self._now_fn()

    def today(self) -> date:
        return self.now().date()

    def _iso(self) -> str:
        return self.now().isoformat()

    def _audit(self, actor: dict, action: str, target: str, **details):
        entry = {
            "seq": len(self.audit) + 1,
            "at": self._iso(),
            "actor_id": actor["id"],
            "actor_name": actor["name"],
            "actor_role": actor["role"],
            "org_id": actor.get("org_id"),
            "action": action,
            "target": target,
            "details": details,
        }
        self.audit.append(entry)
        return entry

    @staticmethod
    def _require(payload: dict, field: str):
        value = payload.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValidationError(f"缺少必填字段: {field}")
        return value

    # ---- 用户与机构 ----

    def register_org(self, org_id: str, name: str, kind: str) -> dict:
        if kind not in {"CLINIC", "LAB"}:
            raise ValidationError("机构类型必须为 CLINIC 或 LAB")
        if org_id in self.orgs:
            raise ConflictError(f"机构已存在: {org_id}")
        org = {"id": org_id, "name": name, "kind": kind}
        self.orgs[org_id] = org
        return org

    def create_user(
        self,
        name: str,
        role: str,
        org_id: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        if role not in {
            ROLE_DOCTOR,
            ROLE_CLINIC_STAFF,
            ROLE_TECHNICIAN,
            ROLE_LAB_ADMIN,
            ROLE_GUARDIAN,
        }:
            raise ValidationError(f"未知角色: {role}")
        if role != ROLE_GUARDIAN and org_id not in self.orgs:
            raise ValidationError("必须归属于已登记机构")
        uid = user_id or self._id("U")
        if uid in self.users:
            raise ConflictError(f"用户已存在: {uid}")
        user = {
            "id": uid,
            "name": name,
            "role": role,
            "org_id": org_id,
            "org_name": self.orgs[org_id]["name"] if org_id else None,
        }
        self.users[uid] = user
        return user

    def user(self, user_id: str) -> dict:
        user = self.users.get(user_id)
        if not user:
            raise NotFoundError(f"用户不存在: {user_id}")
        return user

    def _role(self, actor: dict, *roles: str) -> dict:
        if actor["role"] not in roles:
            raise AuthError(f"角色 {actor['role']} 无权执行该操作")
        return actor

    def _org(self, actor: dict) -> dict:
        org = self.orgs.get(actor.get("org_id") or "")
        if not org:
            raise AuthError("操作者无所属机构")
        return org

    # ---- 患者与身份 ----

    def register_patient(self, payload: dict, actor: dict) -> dict:
        """登记患儿。同一身份证件但姓名转写不同时，转人工确认，不自动新建。"""
        self._role(actor, *CLINIC_ROLES)
        name = str(self._require(payload, "legal_name")).strip()
        id_doc_type = str(self._require(payload, "id_doc_type"))
        id_doc_hash = str(self._require(payload, "id_doc_hash")).strip().lower()
        birth_date = str(self._require(payload, "birth_date"))
        guardian_name = str(self._require(payload, "guardian_name")).strip()

        with self._lock:
            existing_id = self._patient_doc_index.get(id_doc_hash)
            if existing_id:
                existing = self.patients[existing_id]
                if name != existing["legal_name"]:
                    existing["aliases"].append(
                        {"name": name, "source": payload.get("name_source", "未说明"), "at": self._iso()}
                    )
                    # 姓名转写差异：已有档案回到待人工确认状态
                    existing["status"] = PATTERN_IDENTITY_PENDING
                    existing["name_variant"] = name
                self._audit(actor, "patient.match_existing", existing["id"], submitted_name=name)
                return {**existing, "matched_existing": True, "name_variant": name != existing["legal_name"]}

            patient = {
                "id": self._id("PT"),
                "legal_name": name,
                "aliases": [],
                "name_variant": None,
                "birth_date": birth_date,
                "id_doc_type": id_doc_type,
                "id_doc_hash": id_doc_hash,
                "guardian_name": guardian_name,
                "status": PATTERN_IDENTITY_PENDING,
                "confirmations": [],
                "owner_org": {"id": actor["org_id"], "name": actor["org_name"]},
                "shared_orgs": [],
                "guardian_user_ids": [],
                "merged_into": None,
                "created_by": actor["id"],
                "created_at": self._iso(),
            }
            self.patients[patient["id"]] = patient
            self._patient_doc_index[id_doc_hash] = patient["id"]
            self._audit(actor, "patient.register", patient["id"])
            return patient

    def confirm_identity(self, patient_id: str, payload: dict, actor: dict) -> dict:
        """人工确认患儿身份与法定姓名（处理同音字、简写等转写差异）。"""
        self._role(actor, *CLINIC_ROLES)
        with self._lock:
            patient = self._patient(patient_id)
            self._require_clinic_access(patient, actor)
            confirmed_name = str(self._require(payload, "confirmed_legal_name")).strip()
            confirmation = {
                "at": self._iso(),
                "by": actor["id"],
                "by_name": actor["name"],
                "confirmed_legal_name": confirmed_name,
                "note": payload.get("note", ""),
            }
            patient["confirmations"].append(confirmation)
            patient["legal_name"] = confirmed_name
            patient["name_variant"] = None
            patient["status"] = PATTERN_CONFIRMED
            self._audit(actor, "patient.confirm_identity", patient_id, name=confirmed_name)
            return patient

    def add_guardian(self, patient_id: str, guardian_user_id: str, actor: dict, relation: str = "监护人") -> dict:
        self._role(actor, *CLINIC_ROLES)
        guardian = self.user(guardian_user_id)
        self._role(guardian, ROLE_GUARDIAN)
        with self._lock:
            patient = self._patient(patient_id)
            self._require_clinic_access(patient, actor)
            if guardian_user_id not in patient["guardian_user_ids"]:
                patient["guardian_user_ids"].append(guardian_user_id)
            self._audit(actor, "patient.add_guardian", patient_id, guardian=guardian_user_id, relation=relation)
            return patient

    def authorize_clinic(self, patient_id: str, org_id: str, payload: dict, actor: dict) -> dict:
        """授权一家门诊查看患儿履历。

        归属门诊可主动共享；监护人也可在异地就诊时当面授权接诊门诊，
        使外地门诊无需联系原诊所即可查看交付与处置进度、上报异常、安排补制。
        授权范围与期限留痕，不改变档案归属。
        """
        org = self.orgs.get(org_id)
        if not org or org["kind"] != "CLINIC":
            raise ValidationError("授权对象必须是已登记门诊")
        with self._lock:
            patient = self._patient(patient_id)
            scope = str(payload.get("scope", "VIEW_AND_FOLLOWUP"))
            if scope not in {"VIEW_ONLY", "VIEW_AND_FOLLOWUP"}:
                raise ValidationError("scope 必须为 VIEW_ONLY 或 VIEW_AND_FOLLOWUP")
            if actor["role"] in CLINIC_ROLES:
                self._require_clinic_access(patient, actor)
                grantor_kind = "CLINIC"
            elif actor["role"] == ROLE_GUARDIAN:
                if actor["id"] not in patient["guardian_user_ids"]:
                    raise AuthError("仅授权监护人可代表患儿共享档案")
                grantor_kind = "GUARDIAN"
            else:
                raise AuthError("仅归属门诊或监护人可授权共享")
            existing = next((o for o in patient["shared_orgs"] if o["id"] == org_id), None)
            entry = {
                "id": org_id,
                "name": org["name"],
                "scope": scope,
                "granted_by": {"id": actor["id"], "name": actor["name"], "kind": grantor_kind},
                "granted_at": self._iso(),
                "expires_on": payload.get("expires_on"),
                "purpose": payload.get("purpose", ""),
            }
            if existing:
                existing.update(entry)
            else:
                patient["shared_orgs"].append(entry)
            self._audit(actor, "patient.authorize_clinic", patient_id, org=org_id, scope=scope)
            return patient

    def _patient(self, patient_id: str) -> dict:
        patient = self.patients.get(patient_id)
        if not patient:
            raise NotFoundError(f"患儿不存在: {patient_id}")
        if patient["status"] == PATTERN_MERGED:
            raise StateError(f"档案已合并至 {patient['merged_into']}，请使用主档案")
        return patient

    def _require_confirmed(self, patient: dict):
        if patient["status"] != PATTERN_CONFIRMED:
            raise StateError("患儿身份尚未经人工确认，不得排产或制作")

    def _require_clinic_access(self, patient: dict, actor: dict, write: bool = True):
        if actor["role"] not in CLINIC_ROLES:
            raise AuthError("仅接诊机构可访问")
        org_id = actor["org_id"]
        if patient["owner_org"]["id"] == org_id:
            return
        shared = next((o for o in patient["shared_orgs"] if o["id"] == org_id), None)
        if not shared:
            raise AuthError("该机构未被授权查看此患儿档案")
        if write and shared.get("scope", "VIEW_AND_FOLLOWUP") != "VIEW_AND_FOLLOWUP":
            raise AuthError("该机构仅获只读授权，不得登记或变更制作/处置记录")

    # ---- 数字印模/扫描 ----

    def register_scan(self, payload: dict, actor: dict) -> dict:
        """上传扫描。重复上传不产生新扫描，更不可能产生第二件器械。"""
        self._role(actor, *CLINIC_ROLES)
        patient_id = self._require(payload, "patient_id")
        checksum = self._canonical_checksum(str(self._require(payload, "checksum")))
        file_ref = str(self._require(payload, "file_ref"))

        with self._lock:
            patient = self._patient(patient_id)
            self._require_clinic_access(patient, actor)

            for scan_id in self._scan_index.get(checksum, []):
                existing = self.scans[scan_id]
                if existing["patient_id"] == patient_id:
                    self._audit(actor, "scan.duplicate_upload", existing["id"], deduplicated=True)
                    return {**existing, "deduplicated": True}
                # 同一文件却挂在另一位患儿名下：可能是重复建档/姓名差异，挂起人工裁决
                conflict_id = self._id("SCX")
                conflict = {
                    "id": conflict_id,
                    "checksum": checksum,
                    "file_ref": file_ref,
                    "upload_patient_id": patient_id,
                    "existing_patient_id": existing["patient_id"],
                    "existing_scan_id": existing["id"],
                    "status": "PENDING_REVIEW",
                    "opened_at": self._iso(),
                    "review": None,
                }
                self.scan_conflicts[conflict_id] = conflict
                self._audit(actor, "scan.identity_conflict", conflict_id, existing_scan=existing["id"])
                raise ConflictError(
                    f"扫描校验值与另一位患儿的档案相同，已生成人工裁决单 {conflict_id}；裁决前不得据此制作"
                )

            scan = {
                "id": self._id("SC"),
                "patient_id": patient_id,
                "checksum": checksum,
                "algorithm": checksum.split(":", 1)[0],
                "file_ref": file_ref,
                "scan_type": payload.get("scan_type", "IMPRESSION"),
                "bytes_count": payload.get("bytes_count"),
                "taken_at": payload.get("taken_at"),
                "uploaded_by": actor["id"],
                "uploaded_at": self._iso(),
                "verified": None,
                "used_by_devices": [],
            }
            self.scans[scan["id"]] = scan
            self._scan_index.setdefault(checksum, []).append(scan["id"])
            self._audit(actor, "scan.register", scan["id"], patient=patient_id)
            return scan

    def review_scan_conflict(self, conflict_id: str, payload: dict, actor: dict) -> dict:
        """人工裁决同校验值跨档案冲突。

        MERGE_DUPLICATE_RECORD：两处实为同一患儿（如重复建档、姓名转写不同），
        上传档案并入既有档案，既有档案回到待身份确认，不产生新扫描。
        REJECT_UPLOAD：不同患儿或文件误挂，本次上传作废，两边均不得据此制作。
        """
        self._role(actor, *CLINIC_ROLES)
        decision = str(self._require(payload, "decision"))
        if decision not in {"MERGE_DUPLICATE_RECORD", "REJECT_UPLOAD"}:
            raise ValidationError("decision 必须为 MERGE_DUPLICATE_RECORD 或 REJECT_UPLOAD")
        with self._lock:
            conflict = self.scan_conflicts.get(conflict_id)
            if not conflict:
                raise NotFoundError(f"裁决单不存在: {conflict_id}")
            if conflict["status"] != "PENDING_REVIEW":
                raise StateError("该裁决单已处理")
            upload_patient = self._patient(conflict["upload_patient_id"])
            self._require_clinic_access(upload_patient, actor)

            review = {
                "at": self._iso(),
                "by": actor["id"],
                "by_name": actor["name"],
                "decision": decision,
                "note": payload.get("note", ""),
            }
            conflict["review"] = review
            conflict["status"] = "RESOLVED"

            if decision == "MERGE_DUPLICATE_RECORD":
                target = self._patient(conflict["existing_patient_id"])
                upload_patient["status"] = PATTERN_MERGED
                upload_patient["merged_into"] = target["id"]
                target["aliases"].append(
                    {"name": upload_patient["legal_name"], "source": "扫描冲突合并", "at": self._iso()}
                )
                target["status"] = PATTERN_IDENTITY_PENDING
            self._audit(actor, "scan.conflict_reviewed", conflict_id, decision=decision)
            return conflict

    def verify_scan(self, scan_id: str, actor: dict, note: str = "") -> dict:
        """接诊方复核数字印模校验值（重新计算后一致才签名）。"""
        self._role(actor, *CLINIC_ROLES)
        with self._lock:
            scan = self._scan(scan_id)
            patient = self._patient(scan["patient_id"])
            self._require_clinic_access(patient, actor)
            if scan["verified"]:
                raise StateError("该扫描已完成校验，无需重复签名")
            scan["verified"] = {
                "at": self._iso(),
                "by": actor["id"],
                "by_name": actor["name"],
                "checksum": scan["checksum"],
                "note": note,
            }
            self._audit(actor, "scan.verify", scan_id)
            return scan

    def _scan(self, scan_id: str) -> dict:
        scan = self.scans.get(scan_id)
        if not scan:
            raise NotFoundError(f"扫描不存在: {scan_id}")
        return scan

    @staticmethod
    def _canonical_checksum(checksum: str) -> str:
        checksum = checksum.strip().lower()
        if not _CHECKSUM_RE.match(checksum):
            raise ValidationError("校验值格式应为 algorithm:hex（如 sha256:ab12…）")
        return checksum

    # ---- 处方与版本 ----

    def create_prescription(self, payload: dict, actor: dict) -> dict:
        self._role(actor, ROLE_DOCTOR)
        patient_id = self._require(payload, "patient_id")
        content = payload.get("content")
        if not isinstance(content, dict) or not content:
            raise ValidationError("处方内容 content 必须为非空对象")
        with self._lock:
            patient = self._patient(patient_id)
            self._require_clinic_access(patient, actor)
            rx = {
                "id": self._id("RX"),
                "patient_id": patient_id,
                "current_version": 1,
                "versions": [
                    {
                        "version": 1,
                        "content": content,
                        "change_reason": "初版处方",
                        "status": "ACTIVE",
                        "created_by": actor["id"],
                        "created_by_name": actor["name"],
                        "created_at": self._iso(),
                        "approval": {
                            "at": self._iso(),
                            "by": actor["id"],
                            "by_name": actor["name"],
                            "decision": "ISSUE",
                        },
                    }
                ],
            }
            self.prescriptions[rx["id"]] = rx
            self._audit(actor, "prescription.create", rx["id"], patient=patient_id, version=1)
            return rx

    def revise_prescription(self, prescription_id: str, payload: dict, actor: dict) -> dict:
        """提交处方变更。是否已排产/已成成品决定后续批准路径，此处仅挂起新版本。"""
        self._role(actor, ROLE_DOCTOR)
        content = payload.get("content")
        if not isinstance(content, dict) or not content:
            raise ValidationError("处方内容 content 必须为非空对象")
        reason = str(self._require(payload, "change_reason"))
        with self._lock:
            rx = self._rx(prescription_id)
            patient = self._patient(rx["patient_id"])
            self._require_clinic_access(patient, actor)
            new_version = {
                "version": rx["current_version"] + 1,
                "content": content,
                "change_reason": reason,
                "status": "PENDING_APPROVAL",
                "created_by": actor["id"],
                "created_by_name": actor["name"],
                "created_at": self._iso(),
                "approval": None,
            }
            rx["versions"].append(new_version)
            rx["current_version"] = new_version["version"]
            self._audit(
                actor,
                "prescription.revise",
                rx["id"],
                version=new_version["version"],
                reason=reason,
            )
            return rx

    def approve_revision(
        self,
        prescription_id: str,
        payload: dict,
        actor: dict,
    ) -> dict:
        """批准处方变更。

        排产前：责任医生直接启用。
        已排产/制作中：医生可决定终止或返工；返工还需加工中心负责人会签。
        已成成品/已交付：医生决定返工（需加工方会签）或现有成品继续使用；
        不得终止已交付成品。任何情况下历史版本与已发生的制作事实均保留。
        """
        self._role(actor, ROLE_DOCTOR)
        version = int(self._require(payload, "version"))
        device_id = payload.get("device_id")
        decision = str(self._require(payload, "decision"))
        if decision not in {"ACTIVATE", "TERMINATE", "REWORK", "CONTINUE"}:
            raise ValidationError("decision 必须为 ACTIVATE/TERMINATE/REWORK/CONTINUE")
        lab_approver = None
        if payload.get("lab_approver_id"):
            lab_approver = self.user(str(payload["lab_approver_id"]))

        with self._lock:
            rx = self._rx(prescription_id)
            patient = self._patient(rx["patient_id"])
            self._require_clinic_access(patient, actor)
            v = next((item for item in rx["versions"] if item["version"] == version), None)
            if v is None:
                raise NotFoundError(f"处方版本不存在: v{version}")
            if v["status"] != "PENDING_APPROVAL":
                raise StateError(f"v{version} 已处理，不得重复批准或覆盖")

            if device_id:
                device = self._device(device_id)
                if device["patient_id"] != rx["patient_id"] or device["prescription_id"] != rx["id"]:
                    raise ValidationError("器械与处方不匹配")
            else:
                # 尚无器械建档：只允许排产前直接启用
                bound = [d for d in self.devices.values() if d["prescription_id"] == rx["id"]]
                if bound:
                    raise ValidationError("该处方已有关联器械，批准变更必须指定 device_id")
                device = None

            if device is None:
                if decision != "ACTIVATE":
                    raise ValidationError("尚无器械，直接 ACTIVATE 即可")
                self._activate_version(rx, v, actor, "ACTIVATE", note="器械建档前变更")
                self._audit(actor, "prescription.approve_revision", rx["id"], version=version, decision=decision)
                return rx

            latest_run = device["runs"][-1] if device["runs"] else None
            phase = self._production_phase(device)

            if phase == "pre_production":
                if decision != "ACTIVATE":
                    raise ValidationError("尚未排产，直接 ACTIVATE 即可，不存在终止/返工对象")
                self._activate_version(rx, v, actor, "ACTIVATE", note="排产前变更")
            elif phase == "in_production":
                if decision == "ACTIVATE":
                    raise ValidationError("已排产，必须明确 TERMINATE 或 REWORK 的批准责任")
                if decision == "CONTINUE":
                    raise ValidationError("制作尚未完成，不存在可继续使用的成品")
                if decision == "TERMINATE":
                    self._terminate_run(latest_run, actor, v)
                    device["status"] = DEVICE_TERMINATED
                    self._activate_version(rx, v, actor, "TERMINATE", note="在制件终止，新版本另案制作")
                else:  # REWORK
                    self._apply_rework(rx, v, device, latest_run, actor, lab_approver)
            else:  # finished
                if decision == "ACTIVATE":
                    raise ValidationError("成品已产生，必须明确 REWORK 或 CONTINUE")
                if decision == "TERMINATE":
                    raise StateError("成品已下线或已交付，不能终止既成事实；请选择 REWORK 或 CONTINUE")
                if decision == "REWORK":
                    self._apply_rework(rx, v, device, latest_run, actor, lab_approver)
                else:  # CONTINUE
                    self._activate_version(
                        rx, v, actor, "CONTINUE",
                        note="责任医生批准现有成品继续使用，已发生制作事实不变",
                    )
                    device["continuance_approvals"].append(
                        {"at": self._iso(), "by": actor["id"], "by_name": actor["name"], "version": version}
                    )
            self._audit(actor, "prescription.approve_revision", rx["id"], version=version, decision=decision,
                        device=device_id, lab_approver=(lab_approver or {}).get("id"))
            return rx

    def _activate_version(self, rx: dict, v: dict, actor: dict, decision: str, note: str):
        for old in rx["versions"]:
            if old["status"] == "ACTIVE":
                old["status"] = "SUPERSEDED"
        v["status"] = "ACTIVE"
        v["approval"] = {
            "at": self._iso(),
            "by": actor["id"],
            "by_name": actor["name"],
            "decision": decision,
            "note": note,
        }

    def _terminate_run(self, run: dict, actor: dict, v: dict):
        run["status"] = RUN_TERMINATED
        run["terminated"] = {
            "at": self._iso(),
            "by": actor["id"],
            "by_name": actor["name"],
            "prescription_version": v["version"],
            "note": "处方变更后责任医生批准终止在制件",
        }

    def _apply_rework(self, rx, v, device, old_run, actor, lab_approver):
        if not lab_approver:
            raise AuthError("排产后返工必须经加工中心负责人会签")
        self._role(lab_approver, ROLE_LAB_ADMIN)
        if old_run and lab_approver["org_id"] != old_run["lab_org_id"]:
            raise AuthError("会签人必须是承担该件加工的加工中心负责人")
        # 旧批次制作事实原样保留
        old_run["status"] = RUN_SUPERSEDED_BY_REWORK
        old_run["superseded"] = {
            "at": self._iso(),
            "by": actor["id"],
            "by_name": actor["name"],
            "lab_approver": lab_approver["id"],
            "lab_approver_name": lab_approver["name"],
            "prescription_version": v["version"],
        }
        self._activate_version(
            rx, v, actor, "REWORK",
            note=f"医生 {actor['name']} 与加工中心负责人 {lab_approver['name']} 会签返工",
        )
        v["approval"]["lab_approver"] = {"id": lab_approver["id"], "name": lab_approver["name"], "at": self._iso()}
        scan = self._scan(device["scan_id"])
        new_run = self._new_run(device, v["version"], rework_of=old_run)
        new_run["lab_org_id"] = old_run["lab_org_id"]
        new_run["lab_org_name"] = old_run["lab_org_name"]
        new_run["scheduled_by"] = lab_approver["id"]
        new_run["params"] = dict(old_run["params"] or {})
        new_run["params_carried_from_run"] = old_run["id"]
        new_run["prescription_snapshot"] = {"version": v["version"], "content": v["content"]}
        new_run["scan_snapshot"] = {
            "scan_id": scan["id"],
            "file_ref": scan["file_ref"],
            "checksum": scan["checksum"],
            "algorithm": scan["algorithm"],
            "verified_by": (scan["verified"] or {}).get("by"),
        }
        device["status"] = DEVICE_SCHEDULED
        return new_run

    def _rx(self, prescription_id: str) -> dict:
        rx = self.prescriptions.get(prescription_id)
        if not rx:
            raise NotFoundError(f"处方不存在: {prescription_id}")
        return rx

    @staticmethod
    def _production_phase(device: dict) -> str:
        run = device["runs"][-1] if device["runs"] else None
        if run is None:
            return "pre_production"
        if run["status"] in {RUN_SCHEDULED, RUN_IN_PRODUCTION, RUN_QC_FAIL}:
            return "in_production"
        return "finished"

    # ---- 器械与加工 ----

    def create_device(self, payload: dict, actor: dict) -> dict:
        self._role(actor, ROLE_DOCTOR)
        patient_id = self._require(payload, "patient_id")
        scan_id = self._require(payload, "scan_id")
        prescription_id = self._require(payload, "prescription_id")
        replacement_of = payload.get("replacement_of")
        reuse_attested = bool(payload.get("reuse_scan_attested"))

        with self._lock:
            patient = self._patient(patient_id)
            self._require_clinic_access(patient, actor)
            self._require_confirmed(patient)
            scan = self._scan(scan_id)
            if scan["patient_id"] != patient_id:
                raise ValidationError("扫描与患儿不匹配")
            if not scan["verified"]:
                raise StateError("数字印模尚未完成校验值复核，不得开制")
            rx = self._rx(prescription_id)
            if rx["patient_id"] != patient_id:
                raise ValidationError("处方与患儿不匹配")
            active = next(v for v in rx["versions"] if v["status"] == "ACTIVE")

            origin = None
            if replacement_of:
                origin = self._device(replacement_of)
                if origin["patient_id"] != patient_id:
                    raise ValidationError("补制原件与患儿不匹配")
                if origin["status"] not in {DEVICE_LOST, DEVICE_TERMINATED} and not origin.get("damaged"):
                    raise StateError("仅丢失、已终止或破损的器械可走补制流程；正常成品不得照旧复制")
                if not reuse_attested:
                    raise ValidationError("补制复用旧印模必须由医生显式签署 reuse_scan_attested")
                open_excs = [
                    e for e in (self.exceptions[i] for i in origin["exceptions"])
                    if e["status"] == "OPEN"
                ]
                if open_excs:
                    raise StateError(
                        "原件存在未结案异常，须先处置或在新件异常清单中显式承接，不得静默复制"
                    )

            if scan["used_by_devices"] and not origin:
                raise ConflictError(
                    "该扫描已用于另一件器械；如为丢件/终止/破损后补制且经医生确认印模仍有效，"
                    "必须显式提交 replacement_of 与 reuse_scan_attested"
                )


            device = {
                "id": self._id("DV"),
                "patient_id": patient_id,
                "prescription_id": prescription_id,
                "prescription_version_at_issue": active["version"],
                "scan_id": scan_id,
                "status": DEVICE_PLANNED,
                "runs": [],
                "fittings": [],
                "deliveries": [],
                "guardian_confirmations": [],
                "exceptions": [],
                "shipments": [],
                "transfer_history": [],
                "continuance_approvals": [],
                "replacement_of": replacement_of,
                "replaced_by": None,
                "assigned_lab": None,
                "created_by": actor["id"],
                "created_at": self._iso(),
            }
            self.devices[device["id"]] = device
            scan["used_by_devices"].append(device["id"])
            if origin:
                origin["replaced_by"] = device["id"]
            self._audit(
                actor, "device.create", device["id"],
                scan=scan_id, prescription=prescription_id,
                version=active["version"], replacement_of=replacement_of,
                reuse_attested=reuse_attested,
            )
            return device

    def assign_lab(self, device_id: str, payload: dict, actor: dict) -> dict:
        """接诊机构将一件器械指派给某加工中心；未被指派的加工方拿不到任何数据。"""
        self._role(actor, *CLINIC_ROLES)
        lab_org_id = str(self._require(payload, "lab_org_id"))
        lab = self.orgs.get(lab_org_id)
        if not lab or lab["kind"] != "LAB":
            raise ValidationError("目标必须是已登记加工中心")
        with self._lock:
            device = self._device(device_id)
            self._require_clinic_access(self._patient(device["patient_id"]), actor)
            if device["runs"]:
                raise StateError("已有排产记录，不得改派；如需更换加工方须走返工会签")
            device["assigned_lab"] = {"id": lab["id"], "name": lab["name"],
                                      "by": actor["id"], "at": self._iso()}
            self._audit(actor, "device.assign_lab", device_id, lab=lab_org_id)
            return device

    def register_material_batch(self, payload: dict, actor: dict) -> dict:
        self._role(actor, ROLE_LAB_ADMIN)
        self._org(actor)
        batch = {
            "id": self._id("MAT"),
            "material_code": str(self._require(payload, "material_code")),
            "material_name": str(self._require(payload, "material_name")),
            "lot_no": str(self._require(payload, "lot_no")),
            "supplier": str(self._require(payload, "supplier")),
            "lab_org_id": actor["org_id"],
            "lab_org_name": actor["org_name"],
            "registered_by": actor["id"],
            "registered_at": self._iso(),
            "recall": None,
        }
        with self._lock:
            self.batches[batch["id"]] = batch
            self._audit(actor, "material.register_batch", batch["id"], lot=batch["lot_no"])
            return batch

    def schedule_production(self, device_id: str, payload: dict, actor: dict) -> dict:
        """加工中心接单排产，快照处方版本、印模校验值与加工参数。"""
        self._role(actor, ROLE_LAB_ADMIN)
        params = payload.get("manufacturing_params")
        if not isinstance(params, dict) or not params:
            raise ValidationError("manufacturing_params 必须为非空对象")
        with self._lock:
            device = self._device(device_id)
            assigned = device.get("assigned_lab")
            if not assigned or assigned["id"] != actor["org_id"]:
                raise AuthError("该器械未指派给本加工中心，无权接单与查看数据")
            if device["runs"]:
                raise StateError("当前器械已存在制作批次；返工批次由返工会签自动生成，无需重新排产")
            rx = self._rx(device["prescription_id"])
            active = next(v for v in rx["versions"] if v["status"] == "ACTIVE")
            scan = self._scan(device["scan_id"])
            run = self._new_run(device, active["version"])
            run["lab_org_id"] = actor["org_id"]
            run["lab_org_name"] = actor["org_name"]
            run["params"] = dict(params)
            run["prescription_snapshot"] = {"version": active["version"], "content": active["content"]}
            run["scan_snapshot"] = {
                "scan_id": scan["id"],
                "file_ref": scan["file_ref"],
                "checksum": scan["checksum"],
                "algorithm": scan["algorithm"],
                "verified_by": (scan["verified"] or {}).get("by"),
            }
            run["scheduled_by"] = actor["id"]
            device["status"] = DEVICE_SCHEDULED
            self._audit(actor, "production.schedule", run["id"], device=device_id, version=active["version"])
            return run

    def _new_run(self, device: dict, version: int, rework_of: dict | None = None) -> dict:
        run = {
            "id": self._id("RUN"),
            "device_id": device["id"],
            "seq": len(device["runs"]) + 1,
            "prescription_version": version,
            "status": RUN_SCHEDULED,
            "params": None,
            "prescription_snapshot": None,
            "scan_snapshot": None,
            "material_batch_id": None,
            "material_snapshot": None,
            "maker": None,
            "checker": None,
            "qc": None,
            "lab_org_id": None,
            "lab_org_name": None,
            "scheduled_by": None,
            "started_at": None,
            "finished_at": None,
            "rework_of_run": rework_of["id"] if rework_of else None,
        }
        device["runs"].append(run)
        self.runs[run["id"]] = run
        return run

    def start_manufacturing(self, run_id: str, payload: dict, actor: dict) -> dict:
        self._role(actor, ROLE_TECHNICIAN)
        with self._lock:
            run = self._run(run_id)
            if actor["org_id"] != run["lab_org_id"]:
                raise AuthError("仅承担该件的加工中心可填报制作")
            if run["status"] != RUN_SCHEDULED:
                raise StateError(f"制作任务状态 {run['status']}，不可开始制作")
            batch_id = self._require(payload, "material_batch_id")
            batch = self.batches.get(batch_id)
            if not batch:
                raise NotFoundError(f"材料批次不存在: {batch_id}")
            if batch["recall"]:
                raise StateError("该材料批次已召回，不得用于制作；请更换批次并留痕")
            run["status"] = RUN_IN_PRODUCTION
            run["material_batch_id"] = batch_id
            run["material_snapshot"] = {
                "batch_id": batch_id,
                "material_code": batch["material_code"],
                "material_name": batch["material_name"],
                "lot_no": batch["lot_no"],
                "supplier": batch["supplier"],
            }
            run["maker"] = {"id": actor["id"], "name": actor["name"], "at": self._iso()}
            run["started_at"] = self._iso()
            run["equipment"] = payload.get("equipment")
            device = self._device(run["device_id"])
            device["status"] = DEVICE_IN_PRODUCTION
            self._audit(actor, "production.start", run_id, batch=batch_id)
            return run

    def technician_check(self, run_id: str, payload: dict, actor: dict) -> dict:
        """技师复核（须与制作者不同人）。不合格件不得进入试戴。"""
        self._role(actor, ROLE_TECHNICIAN)
        result = str(self._require(payload, "result"))
        if result not in {"PASS", "FAIL"}:
            raise ValidationError("result 必须为 PASS 或 FAIL")
        with self._lock:
            run = self._run(run_id)
            if actor["org_id"] != run["lab_org_id"]:
                raise AuthError("仅承担该件的加工中心可复核")
            if run["status"] != RUN_IN_PRODUCTION:
                raise StateError("仅制作中的任务可提交复核")
            if run["maker"] and run["maker"]["id"] == actor["id"]:
                raise StateError("制作者不得复核本人的件，须由另一名技师复核")
            run["checker"] = {"id": actor["id"], "name": actor["name"], "at": self._iso()}
            run["qc"] = {
                "result": result,
                "items": payload.get("check_items", []),
                "note": payload.get("note", ""),
                "at": self._iso(),
            }
            run["status"] = RUN_QC_PASS if result == "PASS" else RUN_QC_FAIL
            run["finished_at"] = self._iso()
            device = self._device(run["device_id"])
            if result == "PASS":
                device["status"] = DEVICE_READY
            self._audit(actor, "production.check", run_id, result=result)
            return run

    def remake_after_qc_fail(self, run_id: str, payload: dict, actor: dict) -> dict:
        """复核不合格：加工中心自行返工（处方版本不变），不合格批次事实保留。"""
        self._role(actor, ROLE_LAB_ADMIN)
        with self._lock:
            run = self._run(run_id)
            if actor["org_id"] != run["lab_org_id"]:
                raise AuthError("仅承担该件的加工中心可返工")
            if run["status"] != RUN_QC_FAIL:
                raise StateError("仅复核不合格的批次可由加工方返工")
            device = self._device(run["device_id"])
            old_run = run
            old_run["status"] = RUN_SUPERSEDED_BY_REWORK
            old_run["superseded"] = {
                "at": self._iso(),
                "by": actor["id"],
                "by_name": actor["name"],
                "reason": "QC_FAIL_LAB_REWORK",
            }
            rx = self._rx(device["prescription_id"])
            active = next(v for v in rx["versions"] if v["status"] == "ACTIVE")
            scan = self._scan(device["scan_id"])
            new_run = self._new_run(device, active["version"], rework_of=old_run)
            new_run["lab_org_id"] = old_run["lab_org_id"]
            new_run["lab_org_name"] = old_run["lab_org_name"]
            new_run["scheduled_by"] = actor["id"]
            new_run["params"] = dict(payload.get("manufacturing_params") or old_run["params"] or {})
            new_run["params_carried_from_run"] = old_run["id"]
            new_run["prescription_snapshot"] = {"version": active["version"], "content": active["content"]}
            new_run["scan_snapshot"] = {
                "scan_id": scan["id"],
                "file_ref": scan["file_ref"],
                "checksum": scan["checksum"],
                "algorithm": scan["algorithm"],
                "verified_by": (scan["verified"] or {}).get("by"),
            }
            device["status"] = DEVICE_SCHEDULED
            self._audit(actor, "production.remake_qc_fail", new_run["id"], old_run=old_run["id"])
            return new_run

    def _run(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if not run:
            raise NotFoundError(f"制作任务不存在: {run_id}")
        return run

    # ---- 材料召回 ----

    def initiate_recall(self, batch_id: str, payload: dict, actor: dict) -> dict:
        """批次召回：圈定受影响器械并逐件挂出有期限的召回处置单。

        系统只提示风险与范围，是否停用/更换由医生逐件临床判断。
        """
        self._role(actor, ROLE_LAB_ADMIN)
        reason = str(self._require(payload, "reason"))
        deadline_days = int(payload.get("deadline_days", EXCEPTION_DEADLINE_DAYS["BATCH_RECALL"]))
        with self._lock:
            batch = self.batches.get(batch_id)
            if not batch:
                raise NotFoundError(f"材料批次不存在: {batch_id}")
            if actor["org_id"] != batch["lab_org_id"]:
                raise AuthError("仅批次登记方可发起召回")
            if batch["recall"]:
                raise StateError("该批次已处于召回状态")
            batch["recall"] = {
                "at": self._iso(),
                "by": actor["id"],
                "by_name": actor["name"],
                "reason": reason,
                "deadline_days": deadline_days,
            }
            affected = []
            for run in self.runs.values():
                if run["material_batch_id"] != batch_id:
                    continue
                device = self._device(run["device_id"])
                # 同一件器械同一批次只挂一张未结召回单
                if any(
                    e["type"] == "BATCH_RECALL" and e["status"] == "OPEN" and e["batch_id"] == batch_id
                    for e in device["exceptions"]
                ):
                    continue
                exc = self._open_exception(
                    device,
                    exc_type="BATCH_RECALL",
                    description=f"材料批次 {batch['lot_no']}（{batch['material_name']}）召回：{reason}",
                    opener=actor,
                    responsible_id=payload.get("responsible_person_id"),
                    deadline_days=deadline_days,
                    batch_id=batch_id,
                )
                affected.append(exc["id"])
            self._audit(actor, "material.recall", batch_id, affected=affected, count=len(affected))
            return {"batch_id": batch_id, "recall": batch["recall"], "affected_exception_ids": affected,
                    "affected_count": len(affected)}

    # ---- 试戴、交付与监护确认 ----

    def record_fitting(self, device_id: str, payload: dict, actor: dict) -> dict:
        self._role(actor, ROLE_DOCTOR)
        conclusion = str(self._require(payload, "conclusion"))
        if conclusion not in {FIT_OK, FIT_ADJUSTED, FIT_FAIL}:
            raise ValidationError("conclusion 必须为 OK/ADJUSTED/FAIL")
        with self._lock:
            device = self._device(device_id)
            self._require_clinic_access(self._patient(device["patient_id"]), actor)
            run = device["runs"][-1]
            if run["status"] != RUN_QC_PASS:
                raise StateError("仅复核合格的成品可试戴")
            fitting = {
                "at": self._iso(),
                "by": actor["id"],
                "by_name": actor["name"],
                "run_id": run["id"],
                "conclusion": conclusion,
                "clinical_note": payload.get("clinical_note", ""),
            }
            device["fittings"].append(fitting)
            self._audit(actor, "fitting.record", device_id, conclusion=conclusion)
            return fitting

    def deliver(self, device_id: str, payload: dict, actor: dict) -> dict:
        self._role(actor, *CLINIC_ROLES)
        instructions = str(self._require(payload, "delivery_instructions")).strip()
        with self._lock:
            device = self._device(device_id)
            self._require_clinic_access(self._patient(device["patient_id"]), actor)
            current_run = device["runs"][-1]
            if not device["fittings"]:
                raise StateError("当前批次尚无试戴记录，不得交付")
            last_fitting = device["fittings"][-1]
            if last_fitting["run_id"] != current_run["id"]:
                raise StateError("最近一次试戴不对应当前制作批次（如返工后须重新试戴），不得交付")
            if last_fitting["conclusion"] == FIT_FAIL:
                raise StateError("当前批次试戴未通过，不得交付")
            if device["status"] in {DEVICE_TERMINATED, DEVICE_LOST}:
                raise StateError(f"器械状态 {device['status']}，不得交付")
            record = {
                "at": self._iso(),
                "by": actor["id"],
                "by_name": actor["name"],
                "delivery_instructions": instructions,
                "run_id": current_run["id"],
                "prescription_version": self._rx(device["prescription_id"])["current_version"],
            }
            device["deliveries"].append(record)
            device["status"] = DEVICE_DELIVERED
            self._audit(actor, "device.deliver", device_id)
            return record

    def guardian_confirm(self, device_id: str, payload: dict, actor: dict) -> dict:
        self._role(actor, ROLE_GUARDIAN)
        acknowledged = payload.get("acknowledged_items")
        if not isinstance(acknowledged, list) or not acknowledged:
            raise ValidationError("监护人需勾选并确认交付说明条目")
        with self._lock:
            device = self._device(device_id)
            patient = self._patient(device["patient_id"])
            if actor["id"] not in patient["guardian_user_ids"]:
                raise AuthError("仅该患儿授权监护人可确认")
            if not device["deliveries"]:
                raise StateError("尚未登记交付，监护确认无从附着")
            confirmation = {
                "at": self._iso(),
                "guardian_user_id": actor["id"],
                "guardian_name": actor["name"],
                "acknowledged_items": acknowledged,
                "signature_text": str(self._require(payload, "signature_text")),
                "delivery_at": device["deliveries"][-1]["at"],
            }
            device["guardian_confirmations"].append(confirmation)
            self._audit(actor, "device.guardian_confirm", device_id)
            return confirmation

    def create_shipment(self, device_id: str, payload: dict, actor: dict) -> dict:
        """登记普通物流。影像与身份材料一律禁止随物流单流转。"""
        self._role(actor, *CLINIC_ROLES)
        carrier = str(self._require(payload, "carrier"))
        tracking_no = str(self._require(payload, "tracking_no"))
        forbidden = [k for k in payload if k in {"identity_attachments", "photos", "images", "id_documents"}]
        if forbidden:
            raise ValidationError(f"影像与身份材料不得随普通物流单流转: {', '.join(sorted(forbidden))}")
        attachments = payload.get("attachments") or []
        for att in attachments:
            if isinstance(att, dict) and att.get("kind") in {"PHOTO", "ID_DOCUMENT", "SCAN_FILE"}:
                raise ValidationError("物流单仅可携带非身份类纸质说明，影像/印模/身份证件禁止附寄")
        with self._lock:
            device = self._device(device_id)
            self._require_clinic_access(self._patient(device["patient_id"]), actor)
            shipment = {
                "id": self._id("SH"),
                "device_id": device_id,
                "carrier": carrier,
                "tracking_no": tracking_no,
                "attachments": attachments,
                "created_by": actor["id"],
                "created_at": self._iso(),
            }
            self.shipments[shipment["id"]] = shipment
            device["shipments"].append(shipment["id"])
            self._audit(actor, "shipment.create", shipment["id"], device=device_id)
            return self.shipping_label(shipment["id"], actor)

    def shipping_label(self, shipment_id: str, actor: dict) -> dict:
        """物流面单投影：不含姓名、不含影像，运单号脱敏。"""
        shipment = self.shipments.get(shipment_id)
        if not shipment:
            raise NotFoundError(f"物流单不存在: {shipment_id}")
        device = self._device(shipment["device_id"])
        if actor["role"] in CLINIC_ROLES:
            self._require_clinic_access(self._patient(device["patient_id"]), actor, write=False)
        elif actor["role"] in LAB_ROLES:
            if not any(r["lab_org_id"] == actor["org_id"] for r in device["runs"]):
                raise AuthError("加工方无权查看该物流单")
        elif actor["role"] == ROLE_GUARDIAN:
            if actor["id"] not in self._patient(device["patient_id"])["guardian_user_ids"]:
                raise AuthError("监护人未获授权")
        tracking = shipment["tracking_no"]
        hint = tracking[:2] + "****" + tracking[-2:] if len(tracking) > 4 else "****"
        return {
            "shipment_id": shipment["id"],
            "device_code": device["id"],
            "carrier": shipment["carrier"],
            "tracking_no_hint": hint,
            "notice": "面单不含患儿姓名与影像材料",
        }

    # ---- 异常（丢失、破损、过敏疑点、召回） ----

    def report_exception(self, device_id: str, payload: dict, actor: dict) -> dict:
        exc_type = str(self._require(payload, "type"))
        if exc_type not in EXCEPTION_TYPES:
            raise ValidationError(f"异常类型必须为 {sorted(EXCEPTION_TYPES)}")
        if exc_type == "BATCH_RECALL":
            raise ValidationError("批次召回须由加工方通过召回流程发起")
        with self._lock:
            device = self._device(device_id)
            patient = self._patient(device["patient_id"])
            if actor["role"] == ROLE_GUARDIAN:
                if actor["id"] not in patient["guardian_user_ids"]:
                    raise AuthError("仅授权监护人可报告")
                if not payload.get("responsible_person_id"):
                    raise ValidationError("监护人上报的异常必须指定接诊机构责任人")
            else:
                self._require_clinic_access(patient, actor)
            exc = self._open_exception(
                device,
                exc_type=exc_type,
                description=str(self._require(payload, "description")),
                opener=actor,
                responsible_id=payload.get("responsible_person_id"),
                deadline_days=payload.get("deadline_days"),
            )
            if exc_type == "LOSS":
                exc["status_before_loss"] = device["status"]
                device["status"] = DEVICE_LOST
            if exc_type == "DAMAGE":
                device["damaged"] = True
            return exc

    def _open_exception(
        self, device: dict, exc_type: str, description: str, opener: dict,
        responsible_id: str | None, deadline_days: int | None, batch_id: str | None = None,
    ) -> dict:
        responsible_id = responsible_id or (opener["id"] if opener["role"] in CLINIC_ROLES else None)
        if responsible_id:
            responsible = self.user(responsible_id)
            responsible_view = {"id": responsible["id"], "name": responsible["name"], "role": responsible["role"]}
        else:
            responsible_view = None
        days = int(deadline_days or EXCEPTION_DEADLINE_DAYS[exc_type])
        exc = {
            "id": self._id("EX"),
            "device_id": device["id"],
            "type": exc_type,
            "status": "OPEN",
            "description": description,
            "opened_at": self._iso(),
            "opened_by": {"id": opener["id"], "name": opener["name"], "role": opener["role"]},
            "deadline": (self.today() + timedelta(days=days)).isoformat(),
            "deadline_days": days,
            "responsible_person": responsible_view,
            "resolution": None,
            "resolved_at": None,
            "batch_id": batch_id,
            "risk_advisory": self._risk_advisory(exc_type, device, batch_id),
        }
        self.exceptions[exc["id"]] = exc
        device["exceptions"].append(exc["id"])
        self._audit(opener, "exception.open", exc["id"], device=device["id"], type=exc_type,
                    deadline=exc["deadline"])
        return exc

    def _risk_advisory(self, exc_type: str, device: dict, batch_id: str | None) -> str:
        if exc_type == "ALLERGY_SUSPECTED":
            batches = [r.get("material_snapshot") for r in device["runs"] if r.get("material_snapshot")]
            lots = sorted({b["lot_no"] for b in batches})
            return (
                "过敏疑点：系统仅列出该件使用过的材料批次 "
                f"{lots if lots else '（无在用批次记录）'}，不作因果判断；是否停用、更换或就医由医生决定。"
            )
        if exc_type == "BATCH_RECALL":
            return "批次召回：系统提示受影响范围与处置期限，不代替医生决定是否停用/更换。"
        if exc_type == "LOSS":
            return "丢失：补制须重新确认身份并由医生显式确认印模与处方版本，不得凭旧照直接照旧重做。"
        return ""

    def resolve_exception(self, exception_id: str, payload: dict, actor: dict) -> dict:
        self._role(actor, *CLINIC_ROLES)
        outcome = str(self._require(payload, "outcome"))
        with self._lock:
            exc = self.exceptions.get(exception_id)
            if not exc:
                raise NotFoundError(f"异常单不存在: {exception_id}")
            device = self._device(exc["device_id"])
            self._require_clinic_access(self._patient(device["patient_id"]), actor)
            if exc["status"] != "OPEN":
                raise StateError("异常已结案")
            exc["status"] = "RESOLVED"
            exc["resolution"] = {
                "at": self._iso(),
                "by": actor["id"],
                "by_name": actor["name"],
                "outcome": outcome,
                "note": payload.get("note", ""),
                "clinical_decision_by": actor["id"] if actor["role"] == ROLE_DOCTOR else payload.get("doctor_id"),
            }
            exc["resolved_at"] = self._iso()
            if exc["type"] == "LOSS" and outcome == "RECOVERED":
                device["status"] = exc.get("status_before_loss", DEVICE_DELIVERED)
            if exc["type"] == "DAMAGE" and outcome == "REPAIRED":
                device["damaged"] = False
            self._audit(actor, "exception.resolve", exception_id, outcome=outcome)
            return exc

    def assign_exception_responsible(self, exception_id: str, user_id: str, actor: dict) -> dict:
        self._role(actor, *CLINIC_ROLES)
        with self._lock:
            exc = self.exceptions.get(exception_id)
            if not exc:
                raise NotFoundError(f"异常单不存在: {exception_id}")
            device = self._device(exc["device_id"])
            self._require_clinic_access(self._patient(device["patient_id"]), actor)
            person = self.user(user_id)
            exc["responsible_person"] = {"id": person["id"], "name": person["name"], "role": person["role"]}
            self._audit(actor, "exception.assign", exception_id, responsible=user_id)
            return exc

    def list_exceptions(self, actor: dict, status: str | None = None, overdue_only: bool = False) -> list[dict]:
        """异常清单。机构只能看到本机构有权患者的异常；逾期按截止日期计算。"""
        items = []
        today_ = self.today()
        for exc in self.exceptions.values():
            device = self.devices[exc["device_id"]]
            patient = self.patients[device["patient_id"]]
            if actor["role"] in CLINIC_ROLES:
                org_id = actor["org_id"]
                if patient["owner_org"]["id"] != org_id and not any(o["id"] == org_id for o in patient["shared_orgs"]):
                    continue
            elif actor["role"] in LAB_ROLES:
                if not any(r["lab_org_id"] == actor["org_id"] for r in device["runs"]):
                    continue
            elif actor["role"] == ROLE_GUARDIAN:
                if actor["id"] not in patient["guardian_user_ids"]:
                    continue
            if status and exc["status"] != status:
                continue
            view = dict(exc)
            view["overdue"] = exc["status"] == "OPEN" and date.fromisoformat(exc["deadline"]) < today_
            if overdue_only and not view["overdue"]:
                continue
            items.append(view)
        return items

    # ---- 风险提示 ----

    def risk_notices(self, device_id: str, actor: dict) -> dict:
        """返回风险提示与受影响范围。只提示，不判断。"""
        with self._lock:
            device = self._device(device_id)
            patient = self._patient(device["patient_id"])
            self._authorize_view(patient, device, actor)
            notices = []
            for exc_id in device["exceptions"]:
                exc = self.exceptions[exc_id]
                if exc["type"] in {"ALLERGY_SUSPECTED", "BATCH_RECALL"} and exc["status"] == "OPEN":
                    notices.append({
                        "exception_id": exc["id"],
                        "type": exc["type"],
                        "advisory": exc["risk_advisory"],
                        "deadline": exc["deadline"],
                    })
            affected_scope = []
            for run in device["runs"]:
                if not run["material_batch_id"]:
                    continue
                batch = self.batches[run["material_batch_id"]]
                if not batch["recall"]:
                    continue
                count = sum(
                    1 for r in self.runs.values()
                    if r["material_batch_id"] == batch["id"]
                )
                affected_scope.append({
                    "batch_id": batch["id"],
                    "lot_no": batch["lot_no"],
                    "material_name": batch["material_name"],
                    "affected_run_count": count,
                    "recall_reason": batch["recall"]["reason"],
                })
            return {"device_id": device_id, "notices": notices, "recall_scope": affected_scope,
                    "disclaimer": "系统仅提示风险与受影响范围，临床判断由医生作出"}

    # ---- 跨院交接 ----

    def initiate_transfer(self, device_id: str, payload: dict, actor: dict) -> dict:
        self._role(actor, *CLINIC_ROLES)
        to_org_id = str(self._require(payload, "to_org_id"))
        to_org = self.orgs.get(to_org_id)
        if not to_org or to_org["kind"] != "CLINIC":
            raise ValidationError("接收方必须是已登记口腔门诊")
        with self._lock:
            device = self._device(device_id)
            patient = self._patient(device["patient_id"])
            if patient["owner_org"]["id"] != actor["org_id"]:
                raise AuthError("仅当前归属门诊可发起交接")
            if to_org_id == actor["org_id"]:
                raise ValidationError("不能转交给本机构")
            open_excs = [self.exceptions[e] for e in device["exceptions"] if self.exceptions[e]["status"] == "OPEN"]
            missing = [
                e["id"] for e in open_excs
                if not e.get("responsible_person") or not e.get("deadline")
            ]
            if missing:
                raise StateError(f"未结异常缺少责任人或截止时间，须补齐后再交接: {missing}")
            transfer = {
                "id": self._id("TR"),
                "device_id": device_id,
                "patient_id": patient["id"],
                "from_org": dict(patient["owner_org"]),
                "to_org": {"id": to_org["id"], "name": to_org["name"]},
                "status": "PENDING_ACCEPTANCE",
                "initiated_by": {"id": actor["id"], "name": actor["name"]},
                "initiated_at": self._iso(),
                "carried_exceptions": [
                    {
                        "exception_id": e["id"],
                        "type": e["type"],
                        "description": e["description"],
                        "deadline": e["deadline"],
                        "responsible_person": e["responsible_person"],
                        "overdue": date.fromisoformat(e["deadline"]) < self.today(),
                    }
                    for e in open_excs
                ],
                "acceptance": None,
            }
            self.transfers[transfer["id"]] = transfer
            if not any(o["id"] == to_org_id for o in patient["shared_orgs"]):
                patient["shared_orgs"].append({"id": to_org["id"], "name": to_org["name"]})
            device["transfer_history"].append(transfer["id"])
            self._audit(actor, "transfer.initiate", transfer["id"], device=device_id,
                        open_exceptions=len(open_excs))
            return transfer

    def accept_transfer(self, transfer_id: str, payload: dict, actor: dict) -> dict:
        self._role(actor, *CLINIC_ROLES)
        with self._lock:
            transfer = self.transfers.get(transfer_id)
            if not transfer:
                raise NotFoundError(f"交接单不存在: {transfer_id}")
            if transfer["to_org"]["id"] != actor["org_id"]:
                raise AuthError("仅接收门诊可接受交接")
            if transfer["status"] != "PENDING_ACCEPTANCE":
                raise StateError("交接单已处理")
            acknowledged = payload.get("acknowledged_exception_ids") or []
            carried = {e["exception_id"] for e in transfer["carried_exceptions"]}
            if set(acknowledged) != carried:
                raise ValidationError("接收门诊必须逐项确认带交的未结异常（不得遗漏）")
            device = self._device(transfer["device_id"])
            patient = self._patient(transfer["patient_id"])
            transfer["status"] = "ACCEPTED"
            transfer["acceptance"] = {
                "at": self._iso(),
                "by": actor["id"],
                "by_name": actor["name"],
                "acknowledged_exception_ids": acknowledged,
            }
            old_org = patient["owner_org"]["id"]
            patient["owner_org"] = dict(transfer["to_org"])
            patient["shared_orgs"] = [o for o in patient["shared_orgs"] if o["id"] != old_org]
            self._audit(actor, "transfer.accept", transfer_id, device=transfer["device_id"])
            return transfer

    # ---- 器械履历与授权视图 ----

    def _device(self, device_id: str) -> dict:
        device = self.devices.get(device_id)
        if not device:
            raise NotFoundError(f"器械不存在: {device_id}")
        return device

    def _authorize_view(self, patient: dict, device: dict, actor: dict):
        if actor["role"] in CLINIC_ROLES:
            self._require_clinic_access(patient, actor, write=False)
        elif actor["role"] in LAB_ROLES:
            assigned = device.get("assigned_lab")
            if not (assigned and assigned["id"] == actor["org_id"]) and not any(
                r["lab_org_id"] == actor["org_id"] for r in device["runs"]
            ):
                raise AuthError("加工方仅可见被指派或自己承担过的件")
        elif actor["role"] == ROLE_GUARDIAN:
            if actor["id"] not in patient["guardian_user_ids"]:
                raise AuthError("监护人未获该器械授权")
        else:
            raise AuthError("未知角色")

    def device_dossier(self, device_id: str, actor: dict) -> dict:
        """按授权返回器械完整履历。任何一件都能反查全部制作与交接事实。"""
        with self._lock:
            device = self._device(device_id)
            patient = self._patient(device["patient_id"])
            self._authorize_view(patient, device, actor)
            if actor["role"] in CLINIC_ROLES:
                return self._clinic_dossier(device, patient)
            if actor["role"] in LAB_ROLES:
                return self._lab_dossier(device, patient, actor)
            return self._guardian_dossier(device, patient, actor)

    def _common_header(self, device: dict, patient: dict) -> dict:
        return {
            "device_id": device["id"],
            "status": device["status"],
            "patient": {
                "id": patient["id"],
                "birth_date": patient["birth_date"],
                "guardian_name": patient["guardian_name"],
            },
        }

    def _clinic_dossier(self, device: dict, patient: dict) -> dict:
        rx = self._rx(device["prescription_id"])
        scan = self._scan(device["scan_id"])
        return {
            **self._common_header(device, patient),
            "view": "CLINIC_FULL",
            "patient": {
                "id": patient["id"],
                "legal_name": patient["legal_name"],
                "aliases": patient["aliases"],
                "birth_date": patient["birth_date"],
                "id_doc_type": patient["id_doc_type"],
                "id_doc_hash": patient["id_doc_hash"],
                "guardian_name": patient["guardian_name"],
                "status": patient["status"],
                "identity_confirmations": patient["confirmations"],
                "owner_org": patient["owner_org"],
            },
            "scan": {
                "scan_id": scan["id"],
                "file_ref": scan["file_ref"],
                "checksum": scan["checksum"],
                "algorithm": scan["algorithm"],
                "verified": scan["verified"],
                "uploaded_at": scan["uploaded_at"],
            },
            "prescription": {
                "id": rx["id"],
                "current_version": rx["current_version"],
                "versions": rx["versions"],
            },
            "production_runs": device["runs"],
            "fittings": device["fittings"],
            "deliveries": device["deliveries"],
            "guardian_confirmations": device["guardian_confirmations"],
            "exceptions": [self.exceptions[e] for e in device["exceptions"]],
            "shipments": [self.shipments[s] for s in device["shipments"]],
            "transfers": [self.transfers[t] for t in device["transfer_history"]],
            "lineage": {
                "replacement_of": device["replacement_of"],
                "replaced_by": device["replaced_by"],
                "continuance_approvals": device["continuance_approvals"],
            },
            "created_at": device["created_at"],
        }

    def _lab_dossier(self, device: dict, patient: dict, actor: dict) -> dict:
        """加工方最小视图：只含完成制作所必需的数据，无姓名、无证件、无影像。"""
        rx = self._rx(device["prescription_id"])
        active = next(v for v in rx["versions"] if v["status"] == "ACTIVE")
        scan = self._scan(device["scan_id"])
        lab_org_id = actor["org_id"]
        own_runs = [r for r in device["runs"] if r["lab_org_id"] == lab_org_id]
        # 加工方只能看到与自家批次有关的召回信息，不看患者侧异常叙述
        batch_ids = {r["material_batch_id"] for r in own_runs if r["material_batch_id"]}
        recalls = []
        for batch_id in batch_ids:
            batch = self.batches[batch_id]
            if batch["recall"]:
                recalls.append({
                    "batch_id": batch["id"],
                    "lot_no": batch["lot_no"],
                    "recall_reason": batch["recall"]["reason"],
                    "affected_run_count": sum(
                        1 for r in self.runs.values()
                        if r["material_batch_id"] == batch["id"] and r["lab_org_id"] == lab_org_id
                    ),
                })
        return {
            "view": "LAB_MINIMIZED",
            "device_id": device["id"],
            "status": device["status"],
            "patient": {"id": patient["id"]},  # 仅内部代号，不含任何身份信息
            "scan": {
                "file_ref": scan["file_ref"],
                "checksum": scan["checksum"],
                "algorithm": scan["algorithm"],
            },
            "prescription": {"version": active["version"], "fabrication": active["content"]},
            "production_runs": [
                {
                    "run_id": r["id"],
                    "seq": r["seq"],
                    "status": r["status"],
                    "params": r["params"],
                    "scan_snapshot": r["scan_snapshot"],
                    "prescription_snapshot": r["prescription_snapshot"],
                    "material_snapshot": r["material_snapshot"],
                    "maker": r["maker"],
                    "checker": r["checker"],
                    "qc": r["qc"],
                    "rework_of_run": r["rework_of_run"],
                }
                for r in own_runs
            ],
            "material_recalls": recalls,
            "fitting_conclusions": [f["conclusion"] for f in device["fittings"]],
        }

    def _guardian_dossier(self, device: dict, patient: dict, actor: dict) -> dict:
        shipments = []
        for sid in device["shipments"]:
            try:
                shipments.append(self.shipping_label(sid, actor))
            except AuthError:
                continue
        return {
            "view": "GUARDIAN",
            "device_id": device["id"],
            "status": device["status"],
            "patient": {"id": patient["id"], "birth_date": patient["birth_date"]},
            "fittings": [
                {"at": f["at"], "conclusion": f["conclusion"]} for f in device["fittings"]
            ],
            "deliveries": [
                {"at": d["at"], "delivery_instructions": d["delivery_instructions"]}
                for d in device["deliveries"]
            ],
            "confirmations": device["guardian_confirmations"],
            "shipments": shipments,
            "exceptions": [
                {
                    "exception_id": self.exceptions[e]["id"],
                    "type": self.exceptions[e]["type"],
                    "status": self.exceptions[e]["status"],
                    "deadline": self.exceptions[e]["deadline"],
                    "responsible_person": self.exceptions[e]["responsible_person"],
                }
                for e in device["exceptions"]
            ],
            "transfers": [
                {
                    "transfer_id": t,
                    "from_org": self.transfers[t]["from_org"],
                    "to_org": self.transfers[t]["to_org"],
                    "status": self.transfers[t]["status"],
                }
                for t in device["transfer_history"]
            ],
        }

    # ---- 审计 ----

    def audit_trail(self, actor: dict, device_id: str | None = None) -> list[dict]:
        """审计轨迹（接诊机构可调取本机构相关条目）。"""
        entries = []
        for entry in self.audit:
            if device_id and entry["details"].get("device") != device_id and entry["target"] != device_id:
                continue
            if actor["role"] in CLINIC_ROLES:
                if entry["org_id"] and entry["org_id"] != actor["org_id"]:
                    # 跨院后的条目仍允许新 owner 看到：通过患者共享关系成本较高，
                    # 审计接口仅返回本机构操作人产生的记录；完整事实以履历为准。
                    continue
            elif actor["role"] in LAB_ROLES:
                if entry["org_id"] != actor["org_id"]:
                    continue
            else:
                raise AuthError("审计轨迹不对该角色开放")
            entries.append(entry)
        return entries
