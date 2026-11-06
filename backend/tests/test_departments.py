"""Department lifecycle, isolation and lossless migration on a dedicated test DB."""

import tempfile
import unittest
from uuid import UUID

import test_api as fixtures
from departments import DEFAULT_DEPARTMENTS
from storage_db import downgrade, migrate


class DepartmentTests(unittest.TestCase):
    _open_app = fixtures.ApiTests._open_app

    def project(self, client, name="Отделы"):
        reply = client.post("/api/v1/projects", json={"name": name})
        self.assertEqual(reply.status_code, 201, reply.text)
        return reply.json()["id"]

    def departments(self, client, project):
        reply = client.get(f"/api/v1/projects/{project}/departments")
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()

    def upload(self, client, project, department, kind="vor", document_id=None):
        payload = {"project_id": project, "department_id": department, "kind": kind}
        if document_id:
            payload["document_id"] = document_id
        else:
            payload["new_document"] = "true"
        filename, data, mime = ("vor.csv", fixtures._fake_vor_bytes(), "text/csv") if kind == "vor" else ("project.pdf", fixtures._fake_pdf_bytes(), "application/pdf")
        return client.post("/api/v1/documents", data=payload, files={"file": (filename, data, mime)})

    def test_default_departments_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = self._open_app(tmp)
            project = self.project(client)
            items = self.departments(client, project)
            self.assertEqual([item["name"] for item in items], [name for name, _ in DEFAULT_DEPARTMENTS])
            other = self.departments(client, self.project(client, "Другой проект"))
            self.assertTrue({item["id"] for item in items}.isdisjoint({item["id"] for item in other}))
            base = f"/api/v1/projects/{project}/departments"
            self.assertEqual(client.post(base, json={"name": "   "}).status_code, 409)
            reply = client.post(base, json={"name": "Новый отдел"})
            self.assertEqual(reply.status_code, 201, reply.text)
            identity = reply.json()["id"]
            path = f"{base}/{identity}"
            self.assertEqual(client.post(base, json={"name": "новый отдел"}).status_code, 409)
            self.assertEqual(client.patch(path, json={"name": "Переименованный отдел"}).json()["name"], "Переименованный отдел")
            direct = client.post(base, json={"name": "Удаляемый отдел"}).json()
            self.assertEqual(client.delete(f"{base}/{direct['id']}").status_code, 200)
            self.assertEqual(client.patch(path, json={"archived": True}).status_code, 200)
            self.assertNotIn(identity, [item["id"] for item in self.departments(client, project)])
            self.assertEqual(client.get(base + "?archived=true").json()[0]["id"], identity)
            self.assertEqual(client.patch(path, json={"archived": False}).status_code, 200)
            self.assertEqual(client.patch(path, json={"archived": True}).status_code, 200)
            self.assertEqual(client.delete(path).status_code, 200)
            self.assertEqual(client.get(path).status_code, 404)

    def test_documents_pdf_selection_and_run_history_stay_in_department(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, app = self._open_app(tmp)
            project = self.project(client)
            a, b = [item["id"] for item in self.departments(client, project)[:2]]
            vor = self.upload(client, project, a).json()
            foreign_pdf = self.upload(client, project, b, "pdf").json()
            preflight = {"project_id": project, "vor_version_id": vor["version_id"], "pdf_version_ids": []}
            self.assertEqual(client.post("/api/v1/documents/preflight", json=preflight).status_code, 400)
            pdf = self.upload(client, project, a, "pdf").json()
            self.assertEqual(client.post("/api/v1/documents/preflight", json=preflight).status_code, 200)
            preflight["pdf_version_ids"] = [foreign_pdf["version_id"]]
            cross = client.post("/api/v1/documents/preflight", json=preflight)
            self.assertEqual(cross.status_code, 400, cross.text)
            self.assertEqual(cross.json()["error"]["code"], "department_version_mismatch")
            bindings = client.put(f"/api/v1/documents/{vor['version_id']}/pdf-bindings", json={"bindings": {"project.pdf": foreign_pdf["version_id"]}})
            self.assertEqual(bindings.status_code, 400, bindings.text)
            catalog = client.get(f"/api/v1/projects/{project}/catalog?department_id={a}").json()
            self.assertEqual({doc["id"] for doc in catalog}, {vor["document_id"], pdf["document_id"]})
            self.assertEqual(len(client.get(f"/api/v1/projects/{project}/documents?department_id={b}").json()), 1)
            run = app.state.context.repository.create_run(UUID(project), UUID(vor["version_id"]), {}, pdf_pins={"project.pdf": UUID(pdf["version_id"])})
            history = client.get(f"/api/v1/projects/{project}/audits?department_id={a}")
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(history.json()[0]["department_id"], a)
            self.assertEqual(history.json()[0]["run_id"], str(run.id))
            self.assertEqual(client.get(f"/api/v1/projects/{project}/audits?department_id={b}").json(), [])
            counts = {item["id"]: item for item in self.departments(client, project)}
            self.assertEqual(counts[a]["run_count"], 1)
            self.assertEqual(counts[a]["vor_count"], 1)
            self.assertEqual(counts[b]["run_count"], 0)

    def test_archive_and_cross_project_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = self._open_app(tmp)
            project = self.project(client)
            a, b = [item["id"] for item in self.departments(client, project)[:2]]
            other = self.project(client, "Чужой проект")
            self.assertEqual(client.get(f"/api/v1/projects/{other}/departments/{a}").status_code, 404)
            self.assertEqual(client.get(f"/api/v1/projects/{other}/catalog?department_id={a}").status_code, 404)
            wrong_project = self.upload(client, other, a)
            self.assertEqual(wrong_project.status_code, 400, wrong_project.text)
            uploaded = self.upload(client, project, a).json()
            self.assertEqual(self.upload(client, project, b, document_id=uploaded["document_id"]).status_code, 400)
            path = f"/api/v1/projects/{project}/departments/{a}"
            self.assertEqual(client.patch(path, json={"archived": True}).status_code, 200)
            self.assertEqual(self.upload(client, project, a).status_code, 400)
            self.assertEqual(client.delete(path).status_code, 409)
            self.assertEqual(client.patch(path, json={"archived": False}).status_code, 200)
            self.assertEqual(self.upload(client, project, a).status_code, 200)

    def test_migration_preserves_documents_runs_and_pdf_pins_in_surveys(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, app = self._open_app(tmp)
            context = app.state.context
            self.assertIn("smeta_test", context.settings.database_url)
            project = self.project(client)
            department = self.departments(client, project)[0]["id"]
            vor = self.upload(client, project, department).json()
            pdf = self.upload(client, project, department, "pdf").json()
            run = context.repository.create_run(UUID(project), UUID(vor["version_id"]), {}, pdf_pins={"project.pdf": UUID(pdf["version_id"])})
            downgrade(context.settings.database_url, "0011_analysis_artifacts")
            migrate(context.settings.database_url)
            surveys = next(item for item in self.departments(client, project) if item["name"] == "Служба изысканий")
            docs = client.get(f"/api/v1/projects/{project}/catalog?department_id={surveys['id']}")
            self.assertEqual(docs.status_code, 200, docs.text)
            self.assertEqual({doc["id"] for doc in docs.json()}, {vor["document_id"], pdf["document_id"]})
            self.assertEqual(context.repository.run_pinned_versions(run.id), {"project.pdf": UUID(pdf["version_id"])})
            self.assertEqual(client.get(f"/api/v1/audits/{run.id}/progress").json()["department_id"], surveys["id"])
