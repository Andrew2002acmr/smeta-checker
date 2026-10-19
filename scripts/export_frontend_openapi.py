"""Export actual routes/models without opening storage or starting workers."""

import argparse
import json
from pathlib import Path

from api import ApiContext, create_app
from audit_models import AuditSettings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    # No route executes. Injecting inert dependencies makes schema export offline.
    context = ApiContext(settings=AuditSettings(), repository=None, blob_store=None)
    payload = (
        json.dumps(
            create_app(context).openapi(), ensure_ascii=False, sort_keys=True, indent=2
        )
        + "\n"
    )
    path = Path(__file__).resolve().parents[1] / "frontend/openapi.json"
    if args.check:
        if path.read_text(encoding="utf-8") != payload:
            raise SystemExit(
                "OpenAPI is stale; run python -m scripts.export_frontend_openapi"
            )
        print("OpenAPI matches FastAPI")
    else:
        path.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
