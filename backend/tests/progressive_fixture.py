"""Synthetic, non-dispatched progressive review fixtures for the isolated E2E database."""
import json
import os
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url
from storage_blobs import FileBlobStore
from storage_db import open_repository


def seed(root, repo):
    old = repo.load_run(UUID((root / "run-id.txt").read_text().strip()))
    base = old["config"]["position_inputs"][0]
    positions = []
    for i in range(12):
        item = json.loads(json.dumps(base))
        item.update(index=i, number=str(i + 1))
        if item.get("position_source"):
            item["position_source"]["row"] = i + 2
        positions.append(item)
    config = {**old["config"], "position_inputs": positions, "prepare_seconds": 0.1, "prepare_complete": False}
    job = repo.load_position_job(old["id"], 0)
    template = old["indexed_results"][0]["result"]
    from audit_assessment import reduce_assessment

    def resealed(run_id, index: int) -> dict:
        if template.get("schema_version") != "evidence-assessment-v1":
            return dict(template)
        return reduce_assessment({
            "run_id": str(run_id),
            "position_index": index,
            "execution_revision": int(template.get("execution_revision", 0)),
            "fingerprint": template.get("fingerprint", {}),
            "project": {"obligations": (template.get("project") or {}).get("obligations", [])},
            "normative": {"obligations": (template.get("normative") or {}).get("obligations", [])},
            "relationships": {"obligations": (template.get("relationships") or {}).get("obligations", [])},
            "own_quantity": template.get("own_quantity", {}),
            "facts": template.get("facts", []),
            "sources": template.get("sources", []),
            "execution_state": (template.get("execution") or {}).get("state", "complete"),
            "http_attempts": (template.get("execution") or {}).get("http_attempts", 0),
            "tool_executions": (template.get("execution") or {}).get("tool_executions", 0),
            "stop_reason": (template.get("execution") or {}).get("stop_reason", ""),
        }).model_dump(mode="json")

    def create_run():
        run = repo.create_run(old["project_id"], old["vor_version_id"], config,
                              pdf_pins={k: UUID(v) for k, v in config["pdf_versions"].items()})
        for i in range(12):
            repo.create_position_job(run.id, i, queue="llm", payload=job.payload)
            if i < 10:
                result = resealed(run.id, i)
                repo.save_position_result(run.id, i, result)
                repo.finish_position_job(run.id, i, "done", result=result)
        return str(run.id)

    (root / "progressive-run.json").write_text(
        json.dumps({"run_id": create_run(), "resume_run_id": create_run()}), encoding="utf-8"
    )

def advance(root, repo):
    run_id = UUID(json.loads((root / "progressive-run.json").read_text(encoding="utf-8"))["run_id"])
    template = repo.load_run(run_id)["indexed_results"][0]["result"]
    from audit_assessment import reduce_assessment
    result = reduce_assessment({
        "run_id": str(run_id),
        "position_index": 10,
        "execution_revision": int(template.get("execution_revision", 0)),
        "fingerprint": template.get("fingerprint", {}),
        "project": {"obligations": (template.get("project") or {}).get("obligations", [])},
        "normative": {"obligations": (template.get("normative") or {}).get("obligations", [])},
        "relationships": {"obligations": (template.get("relationships") or {}).get("obligations", [])},
        "own_quantity": template.get("own_quantity", {}),
        "facts": template.get("facts", []),
        "sources": template.get("sources", []),
        "execution_state": (template.get("execution") or {}).get("state", "complete"),
        "http_attempts": (template.get("execution") or {}).get("http_attempts", 0),
        "tool_executions": (template.get("execution") or {}).get("tool_executions", 0),
        "stop_reason": (template.get("execution") or {}).get("stop_reason", ""),
    }).model_dump(mode="json") if template.get("schema_version") == "evidence-assessment-v1" else dict(template)
    repo.save_position_result(run_id, 10, result)
    repo.finish_position_job(run_id, 10, "done", result=result)


def main():
    phase, root = sys.argv[1], Path(sys.argv[2])
    url = os.environ["TEST_POSTGRES_URL"]
    if make_url(url).database != "smeta_test_ui":
        raise SystemExit("Only an isolated E2E database is allowed")
    repo = open_repository(url, FileBlobStore(root / "blobs"))
    if phase == "seed":
        seed(root, repo)
    elif phase == "advance":
        advance(root, repo)
    else:
        raise SystemExit("Unknown phase")


if __name__ == "__main__":
    main()
