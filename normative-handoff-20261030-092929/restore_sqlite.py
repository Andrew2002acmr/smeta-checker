"""Restore the included SQLite SQL dump to a NEW file; no existing DB overwrite."""
import argparse
import gzip
from pathlib import Path
import sqlite3
import sys


def restore(dump, destination):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents replacing a colleague's existing database.
    with destination.open('xb'):
        pass
    db = sqlite3.connect(destination)
    try:
        statement = ''
        # SQL string literals contain the original PDF text, including CRLF.
        # Universal-newline conversion would silently change that text.
        with gzip.open(dump, 'rt', encoding='utf-8', newline='') as stream:
            for line in stream:
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ''
        if statement.strip():
            raise ValueError('Incomplete SQL statement')
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Restored database integrity check failed')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Restored database foreign key check failed')
        return destination
    except BaseException:
        db.close()
        destination.unlink(missing_ok=True)
        raise
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=['road-norms', 'electrical-norms'])
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    target = restore(root / 'sql-dumps' / (args.profile + '.sql.gz'), args.destination)
    print('Restored:', target)
