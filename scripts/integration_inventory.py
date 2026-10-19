"""Inventory integration contracts without importing either application.

Uses only the standard library. Reads source files and the existing local
OpenAPI snapshot; does not connect to a database, OCR engine or LLM.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path

METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
UPSTREAM_FILES = (
    "api.py",
    "audit_models.py",
    "audit_tasks.py",
    "audit_service.py",
    "audit_reports.py",
    "vor_parse.py",
    "vor_rules.py",
    "pdf_text.py",
    "pdf_binding.py",
    "storage_db.py",
    "storage_models.py",
    "compose.yaml",
    "pyproject.toml",
    "uv.lock",
)
LOCAL_FILES = (
    "frontend/package.json",
    "frontend/nuxt.config.ts",
    "frontend/openapi.json",
    "frontend/app/composables/useBackend.ts",
    "frontend/app/pages/upload.vue",
    "frontend/app/pages/history.vue",
    "frontend/app/pages/projects/[projectId]/index.vue",
    "frontend/app/pages/projects/[projectId]/runs/[runId].vue",
    "frontend/app/components/AuditWorkspace.vue",
    "frontend/app/components/PdfPanel.vue",
    "frontend/app/components/OfficeEditor.vue",
    "frontend/server/api/[...path].ts",
    "frontend/server/utils/access.ts",
    "app/frontend_contracts.py",
    "app/demo_api.py",
    "app/demo_workspace.py",
    "app/document_structure.py",
    "app/demo_intake.py",
    "app/editor_store.py",
    "app/web_editor.py",
)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def routes(tree: ast.Module) -> list[dict]:
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute) or func.attr not in METHODS:
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                continue
            path = decorator.args[0].value
            if not isinstance(path, str) or not path.startswith("/api/"):
                continue
            result.append(
                {
                    "method": func.attr.upper(),
                    "path": path,
                    "handler": node.name,
                    "line": node.lineno,
                    "response_model": next(
                        (
                            ast.unparse(item.value)
                            for item in decorator.keywords
                            if item.arg == "response_model"
                        ),
                        None,
                    ),
                }
            )
    return sorted(result, key=lambda item: (item["path"], item["method"]))


def models(tree: ast.Module) -> dict:
    result = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields = {
            item.target.id: ast.unparse(item.annotation)
            for item in node.body
            if isinstance(item, ast.AnnAssign)
            and isinstance(item.target, ast.Name)
            and item.target.id != "model_config"
        }
        enum_values = {
            item.targets[0].id: item.value.value
            for item in node.body
            if isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Name)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        }
        if fields or enum_values:
            result[node.name] = {
                "line": node.lineno,
                "fields": fields,
                "string_constants": enum_values,
            }
    return result


def hashes(root: Path, names: tuple[str, ...]) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    local = args.local_root.resolve(strict=True)
    upstream_routes = routes(parse(root / "api.py"))
    snapshot = json.loads((local / "frontend/openapi.json").read_text(encoding="utf-8"))
    local_routes = sorted(
        f"{method.upper()} {path}"
        for path, operations in snapshot["paths"].items()
        if path.startswith("/api/projects")
        for method in operations
        if method in METHODS
    )
    upstream_keys = {f"{item['method']} {item['path']}" for item in upstream_routes}
    tracked = subprocess.check_output(
        ["git", "ls-files", "*.py"], cwd=root, text=True, encoding="utf-8"
    ).splitlines()
    for name in tracked:
        parse(root / name)
    package = json.loads((local / "frontend/package.json").read_text(encoding="utf-8"))
    data = {
        "scope": "Static source analysis; local API routes come from a saved snapshot.",
        "upstream_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "upstream_python_files_parsed": len(tracked),
        "upstream_routes": upstream_routes,
        "local_project_routes_from_snapshot": local_routes,
        "exact_route_overlap": sorted(upstream_keys.intersection(local_routes)),
        "upstream_api_models": models(parse(root / "api.py")),
        "upstream_domain_models": models(parse(root / "audit_models.py")),
        "local_frontend_models": models(parse(local / "app/frontend_contracts.py")),
        "frontend_dependencies": package["dependencies"],
        "source_sha256": {
            "upstream": hashes(root, UPSTREAM_FILES),
            "local": hashes(local, LOCAL_FILES),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "upstream_api_operations": len(upstream_routes),
                "local_project_operations_in_snapshot": len(local_routes),
                "exact_route_overlap": len(data["exact_route_overlap"]),
                "python_files_parsed": len(tracked),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
