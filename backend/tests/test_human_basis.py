"""Reviewed evidence persists, is version-scoped and never changes machine results."""

import tempfile
import unittest
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

import test_api as fixtures
import test_workspace as workspace_fixtures
from storage_models import HumanBasis


class HumanBasisTests(unittest.TestCase):
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
        self.url = f"/api/v1/audits/{self.run}/positions/0/basis"

    def test_confirm_is_idempotent_persistent_reused_and_revocable(self):
        before = self.client.get(self.url.replace("/basis", "/workspace")).json()["row"]
        response = self.client.post(
            self.url + "/confirm",
            json={"evidence_index": 0},
            headers={"x-workspace-user": "alice"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "alice")
        self.assertEqual(rows[0]["origin"], "confirmed")
        first_id = rows[0]["id"]
        again = self.client.post(self.url + "/confirm", json={"evidence_index": 0})
        self.assertEqual(again.json()[0]["id"], first_id)
        self.assertEqual(
            self.client.get(self.url.replace("/basis", "/workspace")).json()["row"],
            before,
        )
        with Session(self.app.state.context.repository._engine) as session:
            self.assertEqual(session.scalar(select(HumanBasis)).actor, "alice")
        run2 = self._run(self.client, self.app, self.vor, self.pdf)
        second_url = f"/api/v1/audits/{run2}/positions/0/basis"
        self.assertTrue(self.client.get(second_url).json()[0]["reused"])
        removed = self.client.delete(second_url + "/" + first_id)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(self.client.get(self.url).json(), [])
        with Session(self.app.state.context.repository._engine) as session:
            self.assertIsNotNone(session.get(HumanBasis, UUID(first_id)).revoked_at)
        confirmed_again = self.client.post(
            self.url + "/confirm", json={"evidence_index": 0}
        )
        self.assertEqual(confirmed_again.status_code, 200, confirmed_again.text)
        self.assertNotEqual(confirmed_again.json()[0]["id"], first_id)

    def test_custom_selection_extracts_pdf_text_and_validates_page_rect_and_document(
        self,
    ):
        selection = {
            "version_id": self.pdf["version_id"],
            "page": 1,
            "rect": [0.05, 0.04, 0.9, 0.8],
            "comment": "Выбрана нужная строка",
        }
        response = self.client.post(self.url, json=selection)
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()[0]
        self.assertIn("Copper cable", row["quote"])
        self.assertEqual(row["origin"], "specified")
        self.assertEqual(row["comment"], selection["comment"])
        self.assertGreater(len(row["region"]["highlights"]), 0)
        for rect in ([0.9, 0, 0.1, 1], [-1, 0, 1, 1], [0, 0, 1, 2]):
            self.assertEqual(
                self.client.post(
                    self.url, json={**selection, "rect": rect}
                ).status_code,
                422,
            )
        self.assertEqual(
            self.client.post(self.url, json={**selection, "page": 50}).status_code, 400
        )
        self.assertEqual(
            self.client.post(
                self.url, json={**selection, "version_id": str(uuid4())}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                self.url, json={**selection, "quote": "invented PDF text"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                self.url + "/confirm", json={"evidence_index": 999}
            ).status_code,
            400,
        )

    def test_other_pdf_version_does_not_inherit_a_user_confirmation(self):
        self.client.post(self.url + "/confirm", json={"evidence_index": 0})
        pdf2 = self._upload(
            self.client,
            "ИГДИ",
            "pdf",
            "project.pdf",
            fixtures._fake_pdf_bytes(),
            "application/pdf",
            new_document=True,
        )
        run2 = self._run(self.client, self.app, self.vor, pdf2)
        self.assertEqual(
            self.client.get(f"/api/v1/audits/{run2}/positions/0/basis").json(), []
        )

    def test_mutations_require_api_authentication(self):
        self.app.state.context.settings.api_token = "synthetic-only"
        self.assertEqual(
            self.client.post(
                self.url + "/confirm", json={"evidence_index": 0}
            ).status_code,
            401,
        )

    def test_preview_and_save_agree_and_stale_preview_is_rejected(self):
        payload = {
            "version_id": self.pdf["version_id"],
            "page": 1,
            "rect": [0.05, 0.04, 0.9, 0.8],
            "mode": "area",
        }
        response = self.client.post(self.url + "/preview", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(self.client.get(self.url).json(), [])
        changed = self.client.post(
            self.url, json={**payload, "selection_digest": "0" * 64}
        )
        self.assertEqual(changed.status_code, 400, changed.text)
        saved = self.client.post(
            self.url, json={**payload, "selection_digest": preview["selection_digest"]}
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()[0]["quote"], preview["quote"])
        self.assertEqual(saved.json()[0]["region"], preview["region"])
        self.assertEqual(
            self.client.post(
                self.url + "/preview", json={**payload, "mode": "text"}
            ).status_code,
            400,
        )
