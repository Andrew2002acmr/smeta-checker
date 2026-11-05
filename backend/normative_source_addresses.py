"""One physical-cell handle protocol shared by tools and source validators."""
import hashlib


def cell_source_id(version, page, table_id, cell_id, *, envelope_version=1):
    seed=f'{version}:{page}:{table_id}:{cell_id}'
    if envelope_version:
        seed+=f':physical-source-anchor-v{envelope_version}'
    namespace=hashlib.sha256(seed.encode()).hexdigest()[:24]
    return f'agent:p{page}:cell:{namespace}'


def cell_source_handles(version, page, table_id, cell_id):
    """Locate historical envelopes for one exact cell, without equating proofs."""
    return frozenset(cell_source_id(version,page,table_id,cell_id,envelope_version=v) for v in (0,1))
