"""Issue 22: compose.yaml starts the minimal production system.

Static checks only: one file defines api + ui + db + redis + pdf/llm
workers with healthchecks, restart policies and persistent volumes.
Live restart/degrade checks run against a real stack, not here.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SERVICES = {"api", "db", "redis", "ui", "worker-pdf", "worker-llm"}
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
        ui_mounts = str(services["ui"].get("volumes"))
        self.assertIn("blobs", ui_mounts)
        self.assertIn("pdfcache", ui_mounts)
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

    def test_ui_serves_streamlit_with_storage(self):
        ui = _compose()["services"]["ui"]
        cmd = " ".join(ui["command"])
        self.assertIn("streamlit", cmd)
        self.assertIn("app.py", cmd)
        env = ui["environment"]
        self.assertIn("db:5432", env.get("DATABASE_URL", ""))
        self.assertIn(".audit_blobs", env.get("BLOB_STORE_URL", ""))
        self.assertIn("8501", str(ui.get("ports")))


if __name__ == "__main__":
    unittest.main()
