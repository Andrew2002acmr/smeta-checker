"""Synthetic, non-dispatched progressive review fixture. Refuses non-test databases."""
import json
import os
import sys
from pathlib import Path
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from storage_db import open_repository
from storage_blobs import FileBlobStore

root=Path(sys.argv[2])
url=os.environ["TEST_POSTGRES_URL"]
if "/smeta_test_ui" not in url:
    raise SystemExit("Only an isolated E2E database is allowed")
repo=open_repository(url,FileBlobStore(root/"blobs"))
if sys.argv[1]=="seed":
    old=repo.load_run(UUID((root/"run-id.txt").read_text().strip()))
    base=old["config"]["position_inputs"][0]
    positions=[]
    for i in range(12):
        item=json.loads(json.dumps(base))
        item.update(index=i,number=str(i+1))
        if item.get("position_source"):
            item["position_source"]["row"]=i+2
        positions.append(item)
    config={**old["config"],"position_inputs":positions,"prepare_seconds":0.1,"prepare_complete":False}
    run=repo.create_run(old["project_id"],old["vor_version_id"],config,pdf_pins={k:UUID(v) for k,v in config["pdf_versions"].items()})
    job=repo.load_position_job(old["id"],0)
    result=old["indexed_results"][0]["result"]
    for i in range(12):
        repo.create_position_job(run.id,i,queue="llm",payload=job.payload)
        if i<10:
            repo.save_position_result(run.id,i,result)
            repo.finish_position_job(run.id,i,"done",result=result)
    (root/"progressive-run.json").write_text(json.dumps(dict(run_id=str(run.id))))
elif sys.argv[1]=="advance":
    run_id=UUID(json.loads((root/"progressive-run.json").read_text())["run_id"])
    result=repo.load_run(run_id)["indexed_results"][0]["result"]
    repo.save_position_result(run_id,10,result)
    repo.finish_position_job(run_id,10,"done",result=result)
else:
    raise SystemExit("Unknown phase")
