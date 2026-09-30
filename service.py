"""儿童活动矫治器制作与交接履历服务。

HTTP 层只做协议适配：鉴权、JSON 编解码、错误码映射；
业务规则全部在 ledger.Ledger 中。

鉴权约定（联调用，非生产方案）：
- 业务接口携带 ``Authorization: Bearer <user_id>``；
- 机构与用户登记接口携带 ``X-Bootstrap-Token``。
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ledger import (
    Ledger,
    LedgerError,
    UnauthorizedError,
)

SERVICE_ID = "pediatric-orthodontics-followup"
SERVICE_NAME = "儿童正畸随访决策"
DEFAULT_BOOTSTRAP_TOKEN = "bootstrap-dev-token"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# ---- 路由声明：(方法, 正则, ledger 方法名, 是否引导接口) ---------------------

POST_ROUTES = [
    (r"^/orgs$", "register_org_http", True),
    (r"^/users$", "create_user_http", True),
    (r"^/patients$", "register_patient_http", False),
    (r"^/patients/(?P<id>[^/]+)/confirm-identity$", "confirm_identity_http", False),
    (r"^/patients/(?P<id>[^/]+)/guardians$", "add_guardian_http", False),
    (r"^/patients/(?P<id>[^/]+)/clinic-authorizations$", "authorize_clinic_http", False),
    (r"^/scans$", "register_scan_http", False),
    (r"^/scan-conflicts/(?P<id>[^/]+)/review$", "review_scan_conflict_http", False),
    (r"^/scans/(?P<id>[^/]+)/verify$", "verify_scan_http", False),
    (r"^/prescriptions$", "create_prescription_http", False),
    (r"^/prescriptions/(?P<id>[^/]+)/revisions$", "revise_prescription_http", False),
    (r"^/prescriptions/(?P<id>[^/]+)/approvals$", "approve_revision_http", False),
    (r"^/devices$", "create_device_http", False),
    (r"^/devices/(?P<id>[^/]+)/assign-lab$", "assign_lab_http", False),
    (r"^/devices/(?P<id>[^/]+)/schedule$", "schedule_http", False),
    (r"^/devices/(?P<id>[^/]+)/fittings$", "fitting_http", False),
    (r"^/devices/(?P<id>[^/]+)/deliveries$", "deliver_http", False),
    (r"^/devices/(?P<id>[^/]+)/guardian-confirmations$", "guardian_confirm_http", False),
    (r"^/devices/(?P<id>[^/]+)/shipments$", "shipment_http", False),
    (r"^/devices/(?P<id>[^/]+)/exceptions$", "report_exception_http", False),
    (r"^/devices/(?P<id>[^/]+)/transfers$", "initiate_transfer_http", False),
    (r"^/runs/(?P<id>[^/]+)/start$", "start_http", False),
    (r"^/runs/(?P<id>[^/]+)/check$", "check_http", False),
    (r"^/runs/(?P<id>[^/]+)/remake$", "remake_http", False),
    (r"^/materials/batches$", "register_batch_http", False),
    (r"^/materials/batches/(?P<id>[^/]+)/recall$", "recall_http", False),
    (r"^/exceptions/(?P<id>[^/]+)/resolve$", "resolve_exception_http", False),
    (r"^/exceptions/(?P<id>[^/]+)/assign$", "assign_exception_http", False),
    (r"^/transfers/(?P<id>[^/]+)/accept$", "accept_transfer_http", False),
]

GET_ROUTES = [
    (r"^/devices/(?P<id>[^/]+)/dossier$", "dossier_http"),
    (r"^/devices/(?P<id>[^/]+)/risk$", "risk_http"),
    (r"^/exceptions$", "list_exceptions_http"),
    (r"^/audit$", "audit_http"),
]


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与履历接口。"""

    ledger = Ledger()
    bootstrap_token = DEFAULT_BOOTSTRAP_TOKEN

    # ---- 协议 ----

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._write_json(200, health_payload())
            return
        for pattern, action in GET_ROUTES:
            match = re.match(pattern, path)
            if match:
                self._dispatch(action, match.groupdict())
                return
        self._write_json(404, {"error": f"未知路径: {path}"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        for pattern, action, bootstrap in POST_ROUTES:
            match = re.match(pattern, path)
            if match:
                self._dispatch(action, match.groupdict(), bootstrap=bootstrap)
                return
        self._write_json(404, {"error": f"未知路径: {path}"})

    def _dispatch(self, action: str, path_params: dict, bootstrap: bool = False):
        try:
            payload = self._read_body()
            if bootstrap:
                token = self.headers.get("X-Bootstrap-Token", "")
                if token != self.bootstrap_token:
                    raise UnauthorizedError("引导令牌无效")
                result = getattr(self, action)(payload)
            else:
                actor = self._authenticate()
                query = self._query_params()
                result = getattr(self, action)(payload, actor, path_params, query)
            self._write_json(200, result)
        except LedgerError as exc:
            self._write_json(exc.http_status, {"error": str(exc), "type": type(exc).__name__})
        except KeyError as exc:
            self._write_json(400, {"error": f"缺少必填字段: {exc.args[0]}"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": f"请求格式错误: {exc}"})

    def _authenticate(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise UnauthorizedError("缺少 Bearer 令牌")
        user_id = header[len("Bearer "):].strip()
        actor = self.ledger.users.get(user_id)
        if not actor:
            raise UnauthorizedError("令牌对应的用户不存在")
        return actor

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须为 JSON 对象")
        return data

    def _query_params(self) -> dict:
        if "?" not in self.path:
            return {}
        from urllib.parse import parse_qs
        parsed = parse_qs(self.path.split("?", 1)[1])
        return {k: v[-1] for k, v in parsed.items()}

    def _write_json(self, status: int, body: dict | list):
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return

    # ---- 引导接口 ----

    def register_org_http(self, payload):
        return self.ledger.register_org(
            org_id=payload["org_id"], name=payload["name"], kind=payload["kind"]
        )

    def create_user_http(self, payload):
        return self.ledger.create_user(
            name=payload["name"],
            role=payload["role"],
            org_id=payload.get("org_id"),
            user_id=payload.get("user_id"),
        )

    # ---- 患者与扫描 ----

    def register_patient_http(self, payload, actor, _params, _query):
        return self.ledger.register_patient(payload, actor)

    def confirm_identity_http(self, payload, actor, params, _query):
        return self.ledger.confirm_identity(params["id"], payload, actor)

    def add_guardian_http(self, payload, actor, params, _query):
        return self.ledger.add_guardian(
            params["id"], payload["guardian_user_id"], actor, payload.get("relation", "监护人")
        )

    def authorize_clinic_http(self, payload, actor, params, _query):
        return self.ledger.authorize_clinic(params["id"], payload["clinic_org_id"], payload, actor)

    def register_scan_http(self, payload, actor, _params, _query):
        return self.ledger.register_scan(payload, actor)

    def review_scan_conflict_http(self, payload, actor, params, _query):
        return self.ledger.review_scan_conflict(params["id"], payload, actor)

    def verify_scan_http(self, payload, actor, params, _query):
        return self.ledger.verify_scan(params["id"], actor, payload.get("note", ""))

    # ---- 处方 ----

    def create_prescription_http(self, payload, actor, _params, _query):
        return self.ledger.create_prescription(payload, actor)

    def revise_prescription_http(self, payload, actor, params, _query):
        return self.ledger.revise_prescription(params["id"], payload, actor)

    def approve_revision_http(self, payload, actor, params, _query):
        return self.ledger.approve_revision(params["id"], payload, actor)

    # ---- 器械与加工 ----

    def create_device_http(self, payload, actor, _params, _query):
        return self.ledger.create_device(payload, actor)

    def assign_lab_http(self, payload, actor, params, _query):
        return self.ledger.assign_lab(params["id"], payload, actor)

    def register_batch_http(self, payload, actor, _params, _query):
        return self.ledger.register_material_batch(payload, actor)

    def recall_http(self, payload, actor, params, _query):
        return self.ledger.initiate_recall(params["id"], payload, actor)

    def schedule_http(self, payload, actor, params, _query):
        return self.ledger.schedule_production(params["id"], payload, actor)

    def start_http(self, payload, actor, params, _query):
        return self.ledger.start_manufacturing(params["id"], payload, actor)

    def check_http(self, payload, actor, params, _query):
        return self.ledger.technician_check(params["id"], payload, actor)

    def remake_http(self, payload, actor, params, _query):
        return self.ledger.remake_after_qc_fail(params["id"], payload, actor)

    # ---- 试戴与交付 ----

    def fitting_http(self, payload, actor, params, _query):
        return self.ledger.record_fitting(params["id"], payload, actor)

    def deliver_http(self, payload, actor, params, _query):
        return self.ledger.deliver(params["id"], payload, actor)

    def guardian_confirm_http(self, payload, actor, params, _query):
        return self.ledger.guardian_confirm(params["id"], payload, actor)

    def shipment_http(self, payload, actor, params, _query):
        return self.ledger.create_shipment(params["id"], payload, actor)

    # ---- 异常与交接 ----

    def report_exception_http(self, payload, actor, params, _query):
        return self.ledger.report_exception(params["id"], payload, actor)

    def resolve_exception_http(self, payload, actor, params, _query):
        return self.ledger.resolve_exception(params["id"], payload, actor)

    def assign_exception_http(self, payload, actor, params, _query):
        return self.ledger.assign_exception_responsible(params["id"], payload["user_id"], actor)

    def initiate_transfer_http(self, payload, actor, params, _query):
        return self.ledger.initiate_transfer(params["id"], payload, actor)

    def accept_transfer_http(self, payload, actor, params, _query):
        return self.ledger.accept_transfer(params["id"], payload, actor)

    # ---- 查询 ----

    def dossier_http(self, payload, actor, params, _query):
        return self.ledger.device_dossier(params["id"], actor)

    def risk_http(self, _payload, actor, params, _query):
        return self.ledger.risk_notices(params["id"], actor)

    def list_exceptions_http(self, _payload, actor, _params, query):
        return self.ledger.list_exceptions(
            actor,
            status=query.get("status"),
            overdue_only=query.get("overdue") in {"1", "true", "yes"},
        )

    def audit_http(self, _payload, actor, _params, query):
        return self.ledger.audit_trail(actor, device_id=query.get("device"))


def build_server(host: str, port: int, ledger: Ledger | None = None,
                 bootstrap_token: str = DEFAULT_BOOTSTRAP_TOKEN):
    """构造绑定指定履历库的 HTTP 服务（测试与多实例部署使用）。"""

    class _BoundHandler(Handler):
        pass

    _BoundHandler.ledger = ledger or Ledger()
    _BoundHandler.bootstrap_token = bootstrap_token
    return ThreadingHTTPServer((host, port), _BoundHandler)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    build_server(args.host, args.port).serve_forever()


if __name__ == "__main__":
    main()
