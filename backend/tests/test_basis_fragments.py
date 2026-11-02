"""Multi-page regions, literal OCR and corrections stay separately attributable."""
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID
import pymupdf
import test_api as fixtures
import test_workspace as workspace_fixtures

TSV = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t1\t1\t60\t20\t90\t222,78\n"

def scan_pdf():
    with pymupdf.open() as source:
        p = source.new_page(width=500, height=300)
        p.insert_text((40, 60), "222.78", fontsize=24)
        image = p.get_pixmap().tobytes("png")
    with pymupdf.open() as pdf:
        for _ in range(2):
            p = pdf.new_page(width=500, height=300)
            p.insert_image(p.rect, stream=image)
        return pdf.tobytes()

class BasisFragmentsTests(unittest.TestCase):
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
        self.payload = dict(version_id=self.pdf["version_id"], page=1, rect=[0.05, 0.05, 0.5, 0.15], mode="multi", fragments=[dict(page=1, rect=[0.05, 0.05, 0.5, 0.15]), dict(page=1, rect=[0.6, 0.3, 0.9, 0.4])])

    def test_disjoint_frames_preview_and_persist_without_enclosing_gap(self):
        preview = self.client.post(self.url + "/preview", json=self.payload)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(len(preview.json()["fragments"]), 2)
        saved = self.client.post(self.url, json={**self.payload, "selection_digest": preview.json()["selection_digest"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        item = saved.json()[0]
        self.assertEqual(len(item["fragments"]), 2)
        self.assertEqual(item["fragments"][1]["rect"], self.payload["fragments"][1]["rect"])
        for fragment in item["fragments"]:
            self.assertTrue(self.app.state.context.blob_store.get(fragment["image_key"]).startswith(b"\x89PNG"))

    def test_ocr_receipt_reused_on_save_corrections_do_not_replace_original(self):
        with patch("ocr_engine.run_tsv", return_value=TSV) as ocr:
            recognized = self.client.post(self.url + "/recognize", json=self.payload)
            self.assertEqual(recognized.status_code, 200, recognized.text)
            self.assertEqual(ocr.call_count, 2)
            self.assertEqual(recognized.json()["fragments"][0]["ocr_text"], "222,78")
            payload = {**self.payload, "ocr_key": recognized.json()["ocr_key"]}
            payload["fragments"][0].update(corrected_text="222,79", text_checked=True, role="volume")
            preview = self.client.post(self.url + "/preview", json=payload)
            self.assertEqual(preview.status_code, 200, preview.text)
            saved = self.client.post(self.url, json={**payload, "selection_digest": preview.json()["selection_digest"]})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(ocr.call_count, 2, "Save must not run OCR again")
            fragment = saved.json()[0]["fragments"][0]
            self.assertEqual(fragment["ocr_text"], "222,78")
            self.assertEqual(fragment["corrected_text"], "222,79")
            self.assertTrue(fragment["text_checked"])
            payload["fragments"][0]["rect"] = [0.1, 0.1, 0.4, 0.2]
            bad = self.client.post(self.url + "/preview", json=payload)
            self.assertEqual(bad.status_code, 400, bad.text)

    def test_scan_regions_on_two_pages_are_preserved_as_one_basis(self):
        # New isolated run pins an image-only, two-page PDF.
        scan = self._upload(self.client, "ИГДИ", "pdf", "project.pdf", scan_pdf(), "application/pdf", document_id=self.pdf["document_id"])
        # Preparation is irrelevant here; reuse a finished result with exact PDF pins.
        context = self.app.state.context
        record = context.repository.load_run(UUID(self.run))
        record["config"]["pdf_versions"] = {"project.pdf": scan["version_id"]}
        context.repository.update_run_config(UUID(self.run), record["config"])
        payload = {**self.payload, "version_id": scan["version_id"], "fragments": [dict(page=1, rect=[0.05, 0.05, 0.5, 0.3]), dict(page=2, rect=[0.05, 0.05, 0.5, 0.3])]}
        with patch("ocr_engine.run_tsv", return_value=TSV):
            recognized = self.client.post(self.url + "/recognize", json=payload)
        self.assertEqual(recognized.status_code, 200, recognized.text)
        self.assertEqual([f["extracted_text"] for f in recognized.json()["fragments"]], ["", ""])
        saved = self.client.post(self.url, json={**payload, "ocr_key": recognized.json()["ocr_key"], "selection_digest": recognized.json()["selection_digest"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual([f["page"] for f in saved.json()[0]["fragments"]], [1, 2])

    def test_invalid_regions_and_ocr_failure_do_not_save_basis(self):
        payload = {**self.payload, "fragments": [dict(page=999, rect=[0, 0, 1, 1])]}
        self.assertEqual(self.client.post(self.url + "/preview", json=payload).status_code, 400)
        self.assertEqual(self.client.post(self.url + "/preview", json={**self.payload, "fragments": self.payload["fragments"] * 11}).status_code, 422)
        with patch("ocr_engine.run_tsv", side_effect=RuntimeError("OCR timeout")):
            self.assertEqual(self.client.post(self.url + "/recognize", json=self.payload).status_code, 503)
        self.assertEqual(self.client.get(self.url).json(), [])


class CropGeometryTests(unittest.TestCase):
    def test_rotated_page_crop_follows_display_coordinates(self):
        from basis_fragments import crop_png
        from PIL import Image
        import io
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), pymupdf.open() as pdf:
                p = pdf.new_page(width=500, height=300)
                native = pymupdf.Rect(40, 40, 120, 100)
                p.draw_rect(native, color=(1, 0, 0), fill=(1, 0, 0))
                p.set_rotation(rotation)
                display = native * p.rotation_matrix
                rect = [display.x0 / p.rect.width, display.y0 / p.rect.height, display.x1 / p.rect.width, display.y1 / p.rect.height]
                with Image.open(io.BytesIO(crop_png(p, rect))) as image:
                    pixel = image.getpixel((image.width // 2, image.height // 2))
                self.assertGreater(pixel[0], 240)
                self.assertLess(pixel[1], 10)
