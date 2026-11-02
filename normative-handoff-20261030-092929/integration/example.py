"""Run from the unpacked package: python integration/example.py"""
import json
from pathlib import Path
import sys

from kb_client import open_kb, search, get_norm, source_pdf, TABLES

ROOT = Path(__file__).resolve().parents[1]
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

output = {}
for profile, query in [('road-norms', 'щебеночно песчаная смесь'),
                       ('electrical-norms', 'заземление')]:
    root = ROOT / 'knowledge-base' / profile
    db = open_kb(root)
    try:
        hits = search(db, query, limit=2)
        output[profile] = {
            'counts': {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in TABLES},
            'hits': [{k: hit[k] for k in ('id', 'source_id', 'designation', 'label', 'citations')}
                     for hit in hits],
            'first_pdf': str(source_pdf(root, db, hits[0]['source_id'])) if hits else None,
        }
        if profile == 'electrical-norms':
            norms = get_norm(db, 'ГЭСНм', '08-02-148-02')
            assert norms, 'Known norm missing'
            output[profile]['sample_norm'] = {k: norms[0][k] for k in ('code', 'title', 'unit')}
    finally:
        db.close()
print(json.dumps(output, ensure_ascii=False, indent=2))
