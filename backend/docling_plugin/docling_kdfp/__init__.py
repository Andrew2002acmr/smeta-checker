"""Docling engine adapter; physical extraction remains in smeta-checker's core."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
from typing import Literal

from docling.datamodel.base_models import Cluster, LayoutPrediction, Table, TableStructurePrediction
from docling.datamodel.pipeline_options import BaseTableStructureOptions
from docling.models.base_table_model import BaseTableStructureModel
from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel

from pdf_table_export import evidence_document, prepare_pages, source_identity, table_data, visible_box


class KdfpTableOptions(BaseTableStructureOptions):
    kind: Literal["kdfp"] = "kdfp"
    cache_dir: str = ".audit_cache/table-extraction"


@contextmanager
def _original_pdf(conv_res, directory):
    backend = conv_res.input._backend
    source = backend.path_or_stream if backend is not None else None
    if isinstance(source, (str, Path)):
        yield Path(source)
        return
    if source is None or not hasattr(source, "read") or not hasattr(source, "seek"):
        raise ValueError("KDFP requires the original PDF path or seekable input stream")
    directory.mkdir(parents=True, exist_ok=True)
    position = source.tell()
    temporary = None
    try:
        with NamedTemporaryFile(dir=directory, suffix=".pdf", delete=False) as stream:
            temporary = Path(stream.name)
            source.seek(0)
            shutil.copyfileobj(source, stream, length=1024*1024)
        source.seek(position)
        yield temporary
    finally:
        source.seek(position)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class KdfpTableStructureModel(BaseTableStructureModel):
    def __init__(self, enabled=True, options=None, **_):
        self.enabled = enabled
        self.options = options or KdfpTableOptions()

    @classmethod
    def get_options_type(cls):
        return KdfpTableOptions

    def predict_tables(self, conv_res, pages):
        if not pages:
            return []
        directory = Path(self.options.cache_dir)
        with _original_pdf(conv_res, directory) as path:
            source = source_identity(path)
            source["filename"] = conv_res.input.file.name
            records = list(prepare_pages(path, [page.page_no for page in pages], cache_dir=directory))
        predictions = []
        for page, record in zip(pages, records, strict=True):
            # The standard Docling table schema cannot hold independent OCR proofs.
            # Keep the full canonical evidence alongside its normal extraction cache.
            destination = directory/source["sha256"]/"evidence"/f"page-{page.page_no:05d}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(evidence_document([record], source), ensure_ascii=False), encoding="utf-8")
            temporary.replace(destination)
            if record["errors"]:
                raise RuntimeError(f"KDFP page {page.page_no} preparation failed: {record['errors']}")
            geometry = record["geometry"]
            if page.size is None or abs(page.size.width-geometry["width"]) > .1 or abs(
                    page.size.height-geometry["height"]) > .1:
                raise ValueError("Docling and original PDF displayed page geometry differ")
            layout = page.predictions.layout or LayoutPrediction()
            clusters = list(layout.clusters)
            next_id = max((cluster.id for cluster in clusters), default=-1)+1
            prediction = TableStructurePrediction()
            for table in record["tables"]:
                box = visible_box(table.get("bbox"), geometry)
                if box is None:
                    raise ValueError("KDFP table has no locatable original PDF geometry")
                bbox = BoundingBox(l=box[0], t=box[1], r=box[2], b=box[3], coord_origin=CoordOrigin.TOPLEFT)
                children = [c for c in clusters if c.bbox.intersection_over_self(bbox) > .8]
                held = {c.id for c in children}
                clusters = [c for c in clusters if c.id not in held]
                cluster = Cluster(id=next_id, label=DocItemLabel.TABLE, bbox=bbox,
                                  confidence=0.0, cells=[], children=children)
                clusters.append(cluster)
                data = table_data(table, geometry)
                prediction.table_map[next_id] = Table(id=next_id, label=DocItemLabel.TABLE,
                    page_no=page.page_no, cluster=cluster, otsl_seq=[], num_rows=data.num_rows,
                    num_cols=data.num_cols, table_cells=data.table_cells)
                next_id += 1
            layout.clusters = clusters
            page.predictions.layout = layout
            predictions.append(prediction)
        return predictions


def table_structure_engines():
    return {"table_structure_engines": [KdfpTableStructureModel]}
