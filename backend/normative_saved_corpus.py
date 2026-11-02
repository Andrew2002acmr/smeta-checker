"""Explicit review replay retains its immutable normative release.

It is not a fresh search in the active release, and cannot populate that release's
cache. Missing or altered historical evidence is an error, never a fallback.
"""
from functools import lru_cache
import re

from normative_corpus import Corpus


@lru_cache(maxsize=4)
def _load(root,database,sha256,release_id,profile):
    corpus=Corpus(root,relative=database,expected_sha=sha256)
    expected=dict(database=database,sha256=sha256,release_id=release_id,profile=profile)
    if corpus.identity!=expected:
        raise ValueError('Сохранённый нормативный выпуск не соответствует своей привязке')
    return corpus


def review_corpus(active,payload):
    pin=payload.get('kb')
    if pin==active.identity:
        return active
    if (not isinstance(pin,dict) or set(pin)!={'database','sha256','release_id','profile'}
            or pin.get('profile')!=active.identity['profile']
            or not isinstance(pin.get('release_id'),str)
            or not re.fullmatch(r'[a-zA-Z0-9_-]+',pin['release_id'])
            or pin.get('database')!='releases/'+pin['release_id']+'/knowledge.sqlite'
            or not isinstance(pin.get('sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',pin['sha256'])):
        raise ValueError('Для локального пересчёта нужна полная привязка нормативного выпуска')
    return _load(active.root,pin['database'],pin['sha256'],pin['release_id'],pin['profile'])
