"""Preserve validated citation edges when a later confirmed answer drops them.

Never restore a state, choose another source, or override a model's doubt.
"""

from copy import deepcopy

from analysis_replay import digest


class ClaimMemory:
    def __init__(self):
        self.claims = {}

    def context(self):
        return [
            {
                k: (v[:250] if k == "explanation" else deepcopy(v))
                for k, v in saved["item"].items()
                if k
                in {
                    "claim_ids",
                    "project_source_ids",
                    "normative_source_ids",
                    "explanation",
                }
            }
            for saved in list(self.claims.values())[-8:]
        ]

    def restore(self, answer, sources, events):
        from normative_source_registry import same_normative_evidence
        lookup = {s["id"]: s for s in sources}
        if len(lookup)!=len(sources):
            return
        for item in answer.items:
            saved = self.claims.get(tuple(sorted(item.claim_ids)))
            if (
                saved is None
                or item.state != "confirmed"
                or item.project_source_ids
                or not item.normative_source_ids
                or not same_normative_evidence(item.normative_source_ids,
                    saved["item"]["normative_source_ids"],sources)
            ):
                continue
            if any(
                sid not in lookup or digest(lookup[sid]) != sha
                for sid, sha in saved["sources"].items()
            ):
                continue
            before = item.model_dump()
            item.project_source_ids = list(saved["item"]["project_source_ids"])
            events.append(
                dict(
                    kind="claim_citation_continuity_v1",
                    claim_ids=list(item.claim_ids),
                    previous_item=deepcopy(saved["item"]),
                    current_original=before,
                    restored_project_source_ids=list(item.project_source_ids),
                    source_sha256=deepcopy(saved["sources"]),
                    current_normative_source_sha256={sid:digest(lookup[sid]) for sid in item.normative_source_ids},
                )
            )

    def remember(self, answer, sources, known_claims):
        from normative_position_check import validate_items

        lookup = {s["id"]: s for s in sources}
        for item in answer.items:
            if (
                item.state != "confirmed"
                or not item.project_source_ids
                or not item.normative_source_ids
                or not item.claim_ids
                or any(
                    not c.startswith(("method", "normative:", "attribute:"))
                    for c in item.claim_ids
                )
            ):
                continue
            rows = validate_items([item], sources, known_claims)
            if len(rows) != 1 or rows[0]["state"] != "confirmed":
                continue
            row = rows[0]
            ids = set(row["project_source_ids"] + row["normative_source_ids"])
            self.claims[tuple(sorted(row["claim_ids"]))] = dict(
                item=deepcopy(row), sources={sid: digest(lookup[sid]) for sid in ids}
            )
