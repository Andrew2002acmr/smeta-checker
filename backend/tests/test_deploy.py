"""Issue 22: compose.yaml starts the minimal production system.

Static checks only: one file defines api + workspace + db + redis + pdf/llm
workers with healthchecks, restart policies and persistent volumes.
Live restart/degrade checks run against a real stack, not here.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SERVICES = {"api", "db", "redis", "workspace", "worker-pdf", "worker-llm"}
REQUIRED_VOLUMES = {"pgdata", "redisdata", "blobs", "pdfcache"}


def _compose():
    with open(ROOT / "compose.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class ComposeTests(unittest.TestCase):
    def test_minimal_system_services_present(self):
        services = set(_compose()["services"])
        self.assertTrue(
            REQUIRED_SERVICES <= services,
            f"missing: {sorted(REQUIRED_SERVICES - services)}",
        )

    def test_healthchecks_restart_policies_and_volumes(self):
        doc = _compose()
        services = doc["services"]
        volumes = set((doc.get("volumes") or {}).keys())
        self.assertTrue(
            REQUIRED_VOLUMES <= volumes,
            f"missing volumes: {sorted(REQUIRED_VOLUMES - volumes)}",
        )
        for name in sorted(REQUIRED_SERVICES):
            service = services[name]
            with self.subTest(service=name):
                self.assertIn("healthcheck", service, "no healthcheck")
                self.assertIn("restart", service, "no restart policy")
                self.assertNotEqual(service["restart"], "no")
        self.assertIn("pgdata", str(services["db"].get("volumes")))
        self.assertIn("redisdata", str(services["redis"].get("volumes")))
        self.assertIn("blobs", str(services["api"].get("volumes")))
        for worker in ("worker-pdf", "worker-llm"):
            mounts = str(services[worker].get("volumes"))
            self.assertIn("blobs", mounts)
            self.assertIn("pdfcache", mounts)

    def test_workers_consume_separate_queues(self):
        services = _compose()["services"]
        pdf_cmd = " ".join(services["worker-pdf"]["command"])
        llm_cmd = " ".join(services["worker-llm"]["command"])
        self.assertIn("-Q pdf", pdf_cmd)
        self.assertIn("-Q llm", llm_cmd)
        # CPU-only box: pdf concurrency small, llm (network-bound) larger.
        self.assertIn("AUDIT_PDF_CONCURRENCY", pdf_cmd)
        self.assertIn("AUDIT_LLM_CONCURRENCY", llm_cmd)

    def test_api_requires_token_secret(self):
        api_env = _compose()["services"]["api"]["environment"]
        token = api_env.get("AUDIT_API_TOKEN", "")
        self.assertIn("AUDIT_API_TOKEN", token)
        self.assertIn(":?", token, "compose must fail without the token")

    def test_workspace_proxies_api_on_loopback(self):
        workspace = _compose()["services"]["workspace"]
        env = workspace["environment"]
        self.assertIn("http://api:8000", env.get("NUXT_BACKEND_URL", ""))
        token = env.get("NUXT_BACKEND_TOKEN", "")
        self.assertIn("AUDIT_API_TOKEN", token)
        self.assertIn(":?", token, "compose must fail without the token")
        self.assertIn("127.0.0.1", str(workspace.get("ports")))
        self.assertIn("3000", str(workspace.get("ports")))
        # Network modes: bind + public host + password are all wired.
        self.assertIn("WORKSPACE_BIND", str(workspace.get("ports")))
        self.assertIn("NUXT_PUBLIC_HOST", env)
        self.assertIn("WORKSPACE_PASSWORD", env)


if __name__ == "__main__":
    unittest.main()
