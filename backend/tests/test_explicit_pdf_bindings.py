"""Explicit copies bind identically in preflight and actual queued preparation."""
import tempfile
import unittest
from uuid import UUID
import test_api as fixtures

class ExplicitPdfBindingTests(unittest.TestCase):
    _open_app = fixtures.ApiTests._open_app
    _upload = fixtures.ApiTests._upload
    _upload_pair = fixtures.ApiTests._upload_pair
    _project_id = fixtures.ApiTests._project_id

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.client, self.app = self._open_app(tmp.name)
        self.vor, self.original = self._upload_pair(self.client)
        self.copy = self._upload(self.client, "ИГДИ", "pdf", "renamed-copy.pdf", fixtures._fake_pdf_bytes(), "application/pdf")
        self.project = self._project_id(self.client, "ИГДИ")
        self.url = f"/api/v1/documents/{self.vor['version_id']}/pdf-bindings"

    def test_copy_overrides_existing_original_in_preflight_and_worker(self):
        data = self.client.get(self.url).json()
        self.assertEqual(data, dict(references=["project.pdf"], bindings={}))
        saved = self.client.put(self.url, json={"bindings": {"project.pdf": self.copy["version_id"]}})
        self.assertEqual(saved.status_code, 200, saved.text)
        payload = dict(project_id=self.project, vor_version_id=self.vor["version_id"])
        preflight = self.client.post("/api/v1/documents/preflight", json=payload)
        self.assertEqual(preflight.status_code, 200, preflight.text)
        self.assertFalse(preflight.json()["has_critical_error"])
        self.assertIn("renamed-copy.pdf", preflight.json()["rows"][0]["pdf"])
        response = self.client.post("/api/v1/audits", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = UUID(response.json()["run_id"])
        self.assertTrue(self.app.state.runner.wait(run_id, timeout=60))
        repo = self.app.state.context.repository
        record = repo.load_run(run_id)
        self.assertEqual(record["config"]["pdf_bindings"], {"project.pdf": "renamed-copy.pdf"})
        jobs = repo.list_position_jobs(run_id)
        self.assertEqual(jobs[0].payload["pdf_name"], "renamed-copy.pdf")
        self.client.put(self.url, json={"bindings": {}})
        self.assertEqual(repo.load_run(run_id)["config"]["pdf_bindings"], {"project.pdf": "renamed-copy.pdf"})
        self.assertEqual(self.client.get(self.url).json()["bindings"], {})

    def test_same_filename_documents_are_distinct_with_an_explicit_binding(self):
        from api import _pdf_pool_keys
        other = self._upload(self.client, "ИГДИ", "pdf", "project.pdf", fixtures._fake_pdf_bytes(), "application/pdf", new_document=True)
        response = self.client.put(self.url, json={"bindings": {"project.pdf": other["version_id"]}})
        self.assertEqual(response.status_code, 200, response.text)
        preflight = self.client.post("/api/v1/documents/preflight", json=dict(project_id=self.project, vor_version_id=self.vor["version_id"]))
        self.assertEqual(preflight.status_code, 200, preflight.text)
        self.assertFalse(preflight.json()["has_critical_error"])
        self.assertIn(other["version_id"], preflight.json()["rows"][0]["pdf"])
        keys = _pdf_pool_keys(self.app.state.context, [UUID(self.original["version_id"]), UUID(other["version_id"])])
        self.assertEqual(len(set(keys.values())), 2)

    def test_foreign_pdf_and_unknown_reference_rejected_without_changing_saved_map(self):
        foreign = self._upload(self.client, "Другой проект", "pdf", "foreign.pdf", fixtures._fake_pdf_bytes(), "application/pdf")
        for bindings in ({"project.pdf": foreign["version_id"]}, {"unknown.pdf": self.copy["version_id"]}, {"project.pdf": self.vor["version_id"]}):
            response = self.client.put(self.url, json={"bindings": bindings})
            self.assertIn(response.status_code, (400, 404), response.text)
            self.assertEqual(self.client.get(self.url).json()["bindings"], {})

    def test_explicit_pool_cannot_silently_replace_a_missing_bound_pdf(self):
        self.client.put(self.url, json={"bindings": {"project.pdf": self.copy["version_id"]}})
        payload = dict(project_id=self.project, vor_version_id=self.vor["version_id"], pdf_version_ids=[self.original["version_id"]])
        response = self.client.post("/api/v1/documents/preflight", json=payload)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("binding_pdf_unavailable", response.text)
