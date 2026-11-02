"""Ready positions are reviewable before completion; revisions and OCR survive."""
import tempfile
import unittest
from uuid import UUID
from unittest.mock import patch
from sqlalchemy import update
from sqlalchemy.orm import Session
import test_api as fixtures
import test_workspace as workspace_fixtures
from storage_models import Run
from test_basis_fragments import TSV

class ReviewHistoryTests(unittest.TestCase):
    _open_app = fixtures.ApiTests._open_app
    _upload = fixtures.ApiTests._upload
    _upload_pair = fixtures.ApiTests._upload_pair
    _project_id = fixtures.ApiTests._project_id
    _run = workspace_fixtures.WorkspaceTests._run

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.client, self.app = self._open_app(tmp.name)
        self.vor, self.pdf = self._upload_pair(self.client)
        self.run = self._run(self.client, self.app, self.vor, self.pdf)
        self.repo = self.app.state.context.repository
        self.base = f"/api/v1/audits/{self.run}"
        self.url = self.base + "/positions/0/basis"
        self.selection = dict(version_id=self.pdf["version_id"], page=1, rect=[0.05,0.05,0.5,0.2], mode="multi", fragments=[dict(page=1,rect=[0.05,0.05,0.5,0.2],corrected_text="Verified text",text_checked=True)])

    def test_ready_position_review_while_another_is_pending_and_after_cancel(self):
        record = self.repo.load_run(UUID(self.run))
        pending = {**record["config"]["position_inputs"][0], "index": 1}
        self.repo.update_run_config(UUID(self.run), {"position_inputs": [*record["config"]["position_inputs"],pending]})
        self.repo.create_position_job(UUID(self.run), 1, queue="llm", payload={})
        with Session(self.repo._engine) as session:
            session.execute(update(Run).where(Run.id == UUID(self.run)).values(status="running"))
            session.commit()
        summary = self.client.get(self.base + "/summary")
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["ready_positions"], [0])
        self.assertEqual(self.client.get(self.base + "/rows").json()["total"],1)
        self.assertEqual(self.client.get(self.base + "/positions/0/workspace").status_code,200)
        self.assertEqual(self.client.get(self.base + "/positions/1/workspace").status_code,409)
        confirmed = self.client.post(self.url + "/confirm",json=dict(evidence_index=0))
        self.assertEqual(confirmed.status_code,200,confirmed.text)
        self.repo.finish_run(UUID(self.run), "cancelled")
        self.assertEqual(self.client.get(self.base + "/rows").status_code,200)
        self.assertEqual(len(self.client.get(self.url).json()),1)

    def test_resume_endpoint_preserves_results_and_manual_basis(self):
        run_id = UUID(self.run)
        original = self.repo.load_run(run_id)["indexed_results"]
        confirmed = self.client.post(self.url + "/confirm", json=dict(evidence_index=0))
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.repo.finish_run(run_id, "cancelled")
        resumed = self.client.post(self.base + "/resume")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["status"], "running")
        self.assertIsNone(resumed.json()["finished_at"])
        self.assertEqual(self.repo.load_run(run_id)["indexed_results"], original)
        self.assertEqual(self.client.get(self.url).json(), confirmed.json())
        self.assertEqual(self.client.post(self.base + "/resume").status_code, 409)

    def test_resume_rejects_completed_and_unknown_runs(self):
        self.assertEqual(self.client.post(self.base + "/resume").status_code, 409)
        self.assertEqual(self.client.post("/api/v1/audits/00000000-0000-0000-0000-000000000000/resume").status_code, 404)

    def test_changed_results_are_retained_idempotently_and_confirmation_is_version_bound(self):
        versions = self.repo.result_versions(UUID(self.run),0)
        baseline = len(versions)
        self.assertGreaterEqual(baseline,1)
        self.client.post(self.url + "/confirm",json=dict(evidence_index=0))
        result = self.repo.load_run(UUID(self.run))["indexed_results"][0]["result"]
        self.repo.save_position_result(UUID(self.run),0,result)
        self.assertEqual(len(self.repo.result_versions(UUID(self.run),0)),baseline)
        result["found_volume"] = "46"
        self.repo.save_position_result(UUID(self.run),0,result)
        self.assertEqual(len(self.repo.result_versions(UUID(self.run),0)),baseline+1)
        self.assertTrue(self.client.get(self.url).json()[0]["outdated"])
        history = self.client.get(self.base + "/positions/0/history")
        self.assertEqual(history.status_code,200,history.text)
        self.assertEqual(len(history.json()["results"]),baseline+1)

    def test_draft_and_ocr_persist_without_confirmation_and_images_are_scoped(self):
        draft = self.client.post(self.url + "/drafts",json=self.selection)
        self.assertEqual(draft.status_code,200,draft.text)
        self.assertEqual(self.client.post(self.url + "/drafts",json=self.selection).json()["id"],draft.json()["id"])
        with patch("ocr_engine.run_tsv",return_value=TSV):
            response=self.client.post(self.url + "/recognize",json=self.selection)
        self.assertEqual(response.status_code,200,response.text)
        history=self.client.get(self.base + "/positions/0/history")
        self.assertEqual(history.status_code,200,history.text)
        self.assertEqual({a["kind"] for a in history.json()["artifacts"]},{"ocr","draft"})
        self.assertEqual(self.client.get(self.url).json(),[])
        image=self.client.get(self.base + f"/positions/0/history/artifact/{draft.json()['id']}/images/0")
        self.assertEqual(image.status_code,200)
        self.assertTrue(image.content.startswith(b"\x89PNG"))
        self.assertEqual(self.client.get(self.base + f"/positions/0/history/artifact/{draft.json()['id']}/images/2").status_code,404)
        extraction=self.client.get(self.base + "/positions/0/extraction?page=1")
        self.assertEqual(extraction.status_code,200,extraction.text)
        self.assertIn("Copper",extraction.json()["text"])

    def test_revoked_basis_remains_in_history(self):
        saved=self.client.post(self.url,json=self.selection)
        self.assertEqual(saved.status_code,200,saved.text)
        self.client.delete(self.url+"/"+saved.json()[0]["id"])
        self.assertEqual(self.client.get(self.url).json(),[])
        history=self.client.get(self.base+"/positions/0/history").json()
        self.assertIsNotNone(history["bases"][0]["revoked_at"])
        self.assertTrue(history["bases"][0]["revoked_at"].endswith("Z"))
