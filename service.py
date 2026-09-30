"""儿童矫治器制作与交接后端的服务入口。

在原有健康检查基础上装配仅追加事件存储、领域服务与 JSON API。
默认内存存储，传 --event-log 可使用 JSONL 持久化（重启后履历完整重建）。
"""

import argparse
import json
from http.server import ThreadingHTTPServer

from ledger import EventStore, LedgerService, Projection
from ledger.webapi import make_handler

SERVICE_ID = "pediatric-orthodontics-followup"
SERVICE_NAME = "儿童矫治器制作与交接"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_app(event_log=None):
    """装配存储、领域服务、投影与 HTTP Handler 类。"""
    store = EventStore(path=event_log)
    svc = LedgerService(store)
    proj = Projection(svc)
    handler = make_handler(svc, proj)
    return store, svc, proj, handler


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--event-log", help="JSONL 事件日志路径，缺省为内存存储")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        store, _svc, _proj, handler = build_app(args.event_log)
        assert handler is not None and store.seq >= 0
        print("基础检查通过")
        return
    _store, _svc, _proj, handler = build_app(args.event_log)
    ThreadingHTTPServer((args.host, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
