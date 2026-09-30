"""HTTP JSON API 适配层（仅依赖标准库）。

鉴权通过请求头传递：
    X-Actor-Id / X-Actor-Role / X-Org-Id（可选）
本层只做协议解析与错误码映射，业务规则全部在 LedgerService / Projection。
"""

import json
from http.server import BaseHTTPRequestHandler

from . import catalog as c
from .errors import LedgerError
from .store import Actor


def make_handler(svc, proj):
    class ApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- 基础协议

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise LedgerError(f"请求体不是合法 JSON：{exc}", status=400)

        def _actor(self):
            return Actor.from_headers(
                self.headers.get("X-Actor-Id"),
                self.headers.get("X-Actor-Role"),
                self.headers.get("X-Org-Id"),
            )

        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

        # ------------------------------------------------------------- 路由

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            from urllib.parse import urlparse

            path = urlparse(self.path).path
            try:
                if method == "GET" and path == "/health":
                    from service import health_payload

                    self._send(200, health_payload())
                    return
                actor = self._actor()
                body = self._read_json() if method == "POST" else {}
                status, payload = self._dispatch(method, path, actor, body)
                self._send(status, payload)
            except LedgerError as exc:
                self._send(exc.status, exc.to_dict())
            except Exception as exc:  # 防御性兜底，避免连接挂起
                self._send(500, {"error": "InternalError", "message": str(exc)})

        def _dispatch(self, method, path, actor, body):
            parts = [p for p in path.split("/") if p]
            rules = routes[method]
            for template, handler in rules:
                kwargs = match(template, parts)
                if kwargs is not None:
                    return 200, handler(self, actor, body, **kwargs)
            raise LedgerError("未知路由", status=404)

        def h_register_org(self, actor, body):
            return svc.register_org(
                actor, body["org_id"], body["name"], body["kind"]
            )

        def h_register_patient(self, actor, body):
            return svc.register_patient(
                actor, body["patient_id"], body["name"],
                body["guardian_id"], body["guardian_name"],
                body["home_clinic_id"],
            )

        def h_propose_alias(self, actor, body, patient_id):
            return svc.propose_name_alias(
                actor, patient_id, body["alias"], body.get("reason", "")
            )

        def h_resolve_alias(self, actor, body, patient_id):
            return svc.resolve_name_alias(
                actor, patient_id, body["alias"], bool(body["approve"])
            )

        def h_pending_aliases(self, actor, body):
            return {"items": svc.pending_name_confirmations(actor)}

        # ------------------------------------------------------------- 扫描

        def h_upload_scan(self, actor, body):
            return svc.upload_scan(
                actor,
                checksum=body["checksum"], algorithm=body["algorithm"],
                patient_id=body.get("patient_id"),
                kind=body.get("kind", "intraoral_scan"),
                name_on_scan=body.get("name_on_scan"),
                content_ref=body.get("content_ref"),
                reason=body.get("reason", ""),
            )

        def h_confirm_scan(self, actor, body, scan_id):
            return svc.confirm_scan_identity(actor, scan_id, body["patient_id"])

        def h_verify_scan(self, actor, body, device_id):
            return svc.verify_scan_for_device(
                actor, device_id, body["checksum"], body["algorithm"]
            )

        # ------------------------------------------------------------- 器械

        def h_create_device(self, actor, body):
            return svc.create_device(
                actor, body["patient_id"], body["scan_id"],
                remade_from=body.get("remade_from"),
            )

        def h_prescription(self, actor, body, device_id):
            return svc.record_prescription(actor, device_id, body["details"])

        def h_schedule(self, actor, body, device_id):
            return svc.schedule_production(
                actor, device_id, body["fabrication_org_id"], body["params"]
            )

        def h_material(self, actor, body, device_id):
            return svc.assign_material(
                actor, device_id, body["batch_id"], body["name"],
                body["supplier"], body["lot"]
            )

        def h_start(self, actor, body, device_id):
            return svc.start_production(actor, device_id)

        def h_tech_review(self, actor, body, device_id):
            return svc.tech_review(
                actor, device_id, body["result"], body.get("notes", "")
            )

        def h_finish(self, actor, body, device_id):
            return svc.finish_production(actor, device_id)

        def h_fitting(self, actor, body, device_id):
            return svc.record_fitting(
                actor, device_id, body["conclusion"], body.get("notes", "")
            )

        def h_deliver(self, actor, body, device_id):
            return svc.deliver(actor, device_id, body["instructions"])

        def h_guardian_confirm(self, actor, body, device_id):
            return svc.guardian_confirm(
                actor, device_id, body.get("note", "")
            )

        # ------------------------------------------------------------- 变更

        def h_propose_change(self, actor, body, device_id):
            return svc.propose_prescription_change(
                actor, device_id, body["snapshot"], body.get("reason", "")
            )

        def h_decide_change(self, actor, body, change_id):
            return svc.decide_prescription_change(
                actor, change_id, body["disposition"],
                body["approvals"], body.get("reason", "")
            )

        # ------------------------------------------------------------- 异常

        def h_open_issue(self, actor, body, device_id):
            return svc.open_issue(
                actor, device_id, body["kind"],
                body.get("description", ""), body.get("owner_id"),
            )

        def h_issue_action(self, actor, body, issue_id):
            return svc.record_issue_action(actor, issue_id, body["action"])

        def h_issue_resolve(self, actor, body, issue_id):
            return svc.resolve_issue(
                actor, issue_id, body["resolution"],
                body.get("clinical_conclusion"),
            )

        def h_recall(self, actor, body):
            return svc.open_recall(actor, body["batch_id"], body["reason"])

        # ------------------------------------------------------------- 交接

        def h_handover_start(self, actor, body, patient_id):
            return svc.initiate_handover(
                actor, patient_id, body["to_clinic_id"]
            )

        def h_handover_accept(self, actor, body, handover_id):
            return svc.accept_handover(actor, handover_id)

        def h_handover_reject(self, actor, body, handover_id):
            return svc.reject_handover(actor, handover_id, body["reason"])

        # ------------------------------------------------------------- 视图

        def h_history(self, actor, body, device_id):
            return proj.full_history(actor, device_id)

        def h_work_order(self, actor, body, device_id):
            return proj.fabrication_work_order(actor, device_id)

        def h_shipping_label(self, actor, body, device_id):
            return proj.shipping_label(actor, device_id)

        def h_clinic_view(self, actor, body, patient_id):
            return proj.clinic_view(actor, patient_id)

        def h_guardian_view(self, actor, body, patient_id):
            return proj.guardian_view(actor, patient_id)

        def h_handover_view(self, actor, body, handover_id):
            return proj.handover_pack(actor, handover_id)

    # 路由表在类定义完成后构建并闭包注入
    routes = build_routes(ApiHandler)
    return ApiHandler


def match(template, parts):
    if len(template) != len(parts):
        return None
    kwargs = {}
    for seg, value in zip(template, parts):
        if seg.startswith("{") and seg.endswith("}"):
            kwargs[seg[1:-1]] = value
        elif seg != value:
            return None
    return kwargs


def build_routes(handler_cls):
    def H(name):
        return getattr(handler_cls, name)

    post = [
        (["orgs"], H("h_register_org")),
        (["patients"], H("h_register_patient")),
        (["name-confirmations"], H("h_pending_aliases")),
        (["patients", "{patient_id}", "name-aliases"], H("h_propose_alias")),
        (["patients", "{patient_id}", "name-alias-decisions"], H("h_resolve_alias")),
        (["scans"], H("h_upload_scan")),
        (["scans", "{scan_id}", "confirm-identity"], H("h_confirm_scan")),
        (["devices", "{device_id}", "verify-scan"], H("h_verify_scan")),
        (["devices"], H("h_create_device")),
        (["devices", "{device_id}", "prescriptions"], H("h_prescription")),
        (["devices", "{device_id}", "schedule"], H("h_schedule")),
        (["devices", "{device_id}", "material"], H("h_material")),
        (["devices", "{device_id}", "production", "start"], H("h_start")),
        (["devices", "{device_id}", "production", "tech-review"], H("h_tech_review")),
        (["devices", "{device_id}", "production", "finish"], H("h_finish")),
        (["devices", "{device_id}", "fitting"], H("h_fitting")),
        (["devices", "{device_id}", "delivery"], H("h_deliver")),
        (["devices", "{device_id}", "guardian-confirmation"], H("h_guardian_confirm")),
        (["devices", "{device_id}", "changes"], H("h_propose_change")),
        (["changes", "{change_id}", "decision"], H("h_decide_change")),
        (["devices", "{device_id}", "issues"], H("h_open_issue")),
        (["issues", "{issue_id}", "actions"], H("h_issue_action")),
        (["issues", "{issue_id}", "resolve"], H("h_issue_resolve")),
        (["recalls"], H("h_recall")),
        (["patients", "{patient_id}", "handovers"], H("h_handover_start")),
        (["handovers", "{handover_id}", "accept"], H("h_handover_accept")),
        (["handovers", "{handover_id}", "reject"], H("h_handover_reject")),
        # 视图也支持带 POST 头查询不必要；这里仅 GET
    ]
    get = [
        (["devices", "{device_id}", "history"], H("h_history")),
        (["devices", "{device_id}", "work-order"], H("h_work_order")),
        (["devices", "{device_id}", "shipping-label"], H("h_shipping_label")),
        (["patients", "{patient_id}", "clinic-view"], H("h_clinic_view")),
        (["patients", "{patient_id}", "guardian-view"], H("h_guardian_view")),
        (["handovers", "{handover_id}"], H("h_handover_view")),
    ]
    return {"GET": get, "POST": post}
