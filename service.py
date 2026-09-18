"""景区夜游资源调度的运行入口。

``/health`` 保持稳定的服务身份契约；``/api/...`` 挂载运营中心接口
（容量/锁位、退款规则、安全处置、财务分账、场次审计）。
"""

from __future__ import annotations

import argparse
import json
from http.server import ThreadingHTTPServer

from nighttour import OperationsCenter
from nighttour.api import make_handler

SERVICE_ID = "night-tour-ops"
SERVICE_NAME = "景区夜游资源调度"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_handler(center: OperationsCenter):
    """组合健康检查与业务 API，返回可直接交给 HTTPServer 的 Handler 类。"""

    api_handler = make_handler(center)

    class Handler(api_handler):
        def do_GET(self):  # noqa: N802 - http.server 约定
            if self.path.split("?", 1)[0] == "/health":
                self._write_health()
                return
            super().do_GET()

        def do_POST(self):  # noqa: N802
            if self.path.split("?", 1)[0] == "/health":
                self._write_health()
                return
            super().do_POST()

        def _write_health(self):
            body = json.dumps(health_payload(),
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


# 模块级默认中心与 Handler：单进程运行与契约测试共用；
# ThreadingHTTPServer 下所有状态变更都由领域锁保护。
CENTER = OperationsCenter()
Handler = build_handler(CENTER)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--event-log", default=None,
                        help="事件日志 JSONL 路径；缺省仅保留在内存")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 同时自检领域模块可加载
        from nighttour.models import Session  # noqa: F401
        print("基础检查通过")
        return
    center = (OperationsCenter(event_log_path=args.event_log)
              if args.event_log else CENTER)
    handler_cls = build_handler(center) if args.event_log else Handler
    ThreadingHTTPServer(("0.0.0.0", args.port), handler_cls).serve_forever()


if __name__ == "__main__":
    main()
