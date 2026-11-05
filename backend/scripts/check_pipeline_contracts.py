"""Run the declared offline contract suite, never the application audit queue."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def main():
    backend = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--matrix",
        type=Path,
        default=backend.parent / "docs/evidence/pipeline-stage-contracts.json",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    matrix = json.loads(args.matrix.read_text("utf-8"))
    nodes = sorted({node for stage in matrix["stages"] for node in stage["tests"]})
    if not nodes or any(
        not n.startswith("tests/test_") or not n.endswith(".py") or ".." in n
        for n in nodes
    ):
        raise ValueError("Only explicit test modules are permitted")
    args.out.mkdir(parents=True, exist_ok=True)
    xml = args.out / "junit.xml"
    if xml.exists():
        xml.unlink()
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "pytest",
            "--offline-contracts",
            *nodes,
            "-q",
            "--basetemp=" + str(args.out.resolve() / "pytest-temp"),
            "--junitxml=" + str(xml.resolve()),
        ],
        cwd=backend,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    (args.out / "pytest.log").write_text(result.stdout + result.stderr, "utf-8")
    cases = list(ET.parse(xml).getroot().iter("testcase")) if xml.exists() else []
    stages = []
    for stage in matrix["stages"]:
        names = {n.removesuffix(".py").replace("/", ".") for n in stage["tests"]}
        selected = [c for c in cases if c.get("classname") in names]
        failed = sum(
            c.find("failure") is not None or c.find("error") is not None
            for c in selected
        )
        skipped = sum(c.find("skipped") is not None for c in selected)
        status = (
            "not_measured"
            if not stage["tests"]
            else "failed"
            if failed
            else "not_executed"
            if not selected
            else "partial"
            if skipped
            else "contract_checks_passed"
        )
        stages.append(
            dict(
                id=stage["id"],
                status=status,
                tests=len(selected),
                failed=failed,
                skipped=skipped,
                limitation=stage["known_limit"],
            )
        )
    report = dict(
        schema=1,
        scope=matrix["scope"],
        pytest_exit_code=result.returncode,
        unique_test_cases=len(cases),
        stages=stages,
        routerai_requests=0,
        model_requests=0,
        project_audit_dispatched=False,
        model_quality_measured=False,
        network_guard="python_socket_connections_denied",
        source_code_sha256={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(backend.glob("normative_*.py"))
        },
        matrix_sha256=hashlib.sha256(args.matrix.read_bytes()).hexdigest(),
    )
    (args.out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "source_code_sha256"},
            ensure_ascii=False,
        )
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
