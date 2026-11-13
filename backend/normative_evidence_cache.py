"""Conservative cache for original evidence across pure verdict changes."""
from copy import deepcopy
from functools import lru_cache
import hashlib
from pathlib import Path
from uuid import UUID

from analysis_replay import digest, get, put

# Only modules that consume existing evidence without selecting/reading sources
# are exempted. Adding another exemption requires explicit compatibility tests.
PURE_PROJECTIONS = frozenset({'normative_verdict.py', 'normative_workbook_arithmetic.py'})


def source_fingerprint(root):
    patterns = ('audit_*.py', 'vor_*.py', 'normative_*.py', 'pdf_*.py', 'ocr_*.py',
        'analysis_replay.py', 'human_basis.py', 'workspace_service.py', 'document_preparation.py', 'uv.lock')
    paths = {p for pattern in patterns for p in root.glob(pattern) if p.name not in PURE_PROJECTIONS}
    return digest({p.name:hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        for p in sorted(paths)})


@lru_cache(maxsize=1)
def source_contract():
    return dict(schema=1, source_pipeline=source_fingerprint(Path(__file__).resolve().parent))


def cache_identity(binding, contract):
    stable = deepcopy(binding)
    stable.pop('algorithm')  # Current validators always recompute the decision.
    return dict(schema=1, stage='normative_evidence_cache', project_id=binding['project_id'],
        binding=stable, source_contract=deepcopy(contract))


def store_evidence_index(context, record, index, saved, binding):
    from normative_evidence_snapshot import read_answer
    reference = saved['payload']['position_check'].get('coverage', {}).get('agent_evidence_snapshot')
    if not reference or reference.get('source_contract') != source_contract():
        return  # Old captures have no certified source-generation contract.
    artifact = context.repository.analysis_artifact(record['project_id'], reference['key'])
    if artifact is None or artifact['payload']['sha256'] != reference['sha256']:
        raise ValueError('Original evidence is missing or changed')
    read_answer(artifact['payload'], binding)
    put(context, cache_identity(binding, source_contract()), dict(
        origin=dict(run_id=str(record['id']), index=index, review_id=str(saved['id'])),
        evidence=deepcopy(reference)))


def replay_evidence(context, record, index, binding, actor):
    from normative_evidence_snapshot import revalidate_answer
    artifact = get(context, cache_identity(binding, source_contract()))
    if artifact is None:
        return None
    origin = artifact['origin']
    row = context.repository.normative_review(UUID(origin['run_id']), origin['index'], UUID(origin['review_id']))
    if (row['kind'] != 'check' or row['payload']['position_check'].get('execution_state') != 'complete'
            or row['payload']['position_check'].get('coverage', {}).get('agent_evidence_snapshot') != artifact['evidence']):
        raise ValueError('Original evidence index is inconsistent')
    row = deepcopy(row)
    row['payload']['work_item_id'] = f"{record['id']}:{index}"
    return revalidate_answer(context, record, index, row, actor)
