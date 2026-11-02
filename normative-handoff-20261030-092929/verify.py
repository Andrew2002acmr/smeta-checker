"""Verify a relocated normative package without writing to its databases."""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'integration'))
from kb_client import contained, database_path, open_kb, search, source_pdf, TABLES


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def verify(root):
    checked = 0
    for line in (root / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        digest, relative = line.split('  ', 1)
        path = contained(root, relative)
        if sha(path) != digest:
            raise ValueError('Checksum mismatch: ' + relative)
        checked += 1
    profiles = {}
    package = json.loads((root / 'PACKAGE.json').read_text(encoding='utf-8'))
    for profile in package['profiles']:
        corpus = root / 'knowledge-base' / profile['name']
        db = open_kb(corpus)
        try:
            if db.execute('PRAGMA integrity_check').fetchall()[0][0] != 'ok':
                raise ValueError('SQLite integrity error: ' + profile['name'])
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Foreign key error: ' + profile['name'])
            counts = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in TABLES}
            if counts != profile['counts']:
                raise ValueError('Table counts changed: ' + profile['name'])
            for row in db.execute('SELECT id,sha256,pages FROM sources'):
                path = source_pdf(corpus, db, row['id'])
                if sha(path) != row['sha256']:
                    raise ValueError('PDF/source hash mismatch: ' + str(path))
            for item in (database_path(corpus).parent / 'exports').glob('*.jsonl'):
                table = item.stem
                if table not in TABLES:
                    continue
                with item.open(encoding='utf-8') as f:
                    actual = sum(1 for line in f if line.strip())
                if actual != counts[table]:
                    raise ValueError('JSONL count mismatch: ' + str(item))
            hits = search(db, 'щебень' if profile['name'] == 'road-norms' else 'заземление', limit=2)
            if not hits:
                raise ValueError('FTS search returned no hits: ' + profile['name'])
            profiles[profile['name']] = {'integrity': 'ok', 'foreign_keys': 'ok',
                                         'counts': counts, 'search_hits': len(hits)}
        finally:
            db.close()
    return {'ok': True, 'files_checked': checked, 'profiles': profiles}


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(verify(Path(__file__).resolve().parent), ensure_ascii=False, indent=2))
