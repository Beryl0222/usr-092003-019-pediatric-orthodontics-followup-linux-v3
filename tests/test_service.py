"""验证基础服务身份和路由行为。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import SERVICE_ID, SERVICE_NAME, build_app, health_payload

_HANDLER = build_app()[3]


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _HANDLER)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_payload(self):
        self.assertEqual(health_payload(), {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME})

    def test_health_route(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), health_payload())

    def test_unknown_route(self):
        req = Request(
            f"{self.base_url}/unknown",
            headers={"X-Actor-Id": "a1", "X-Actor-Role": "clinic_admin"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_business_route_requires_identity(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/patients/x/clinic-view", timeout=2)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
