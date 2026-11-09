"""Pinned optional local models for short literal units and ordered numbers."""
import hashlib
import json
import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT='/opt/smeta/paired-tessdata'
BEST_REVISION='e12c65a915945e4c28e237a9b52bc4a8f39a0cec'
MODELS: tuple[dict[str,Any],...]=(
    dict(id='cyrillic-best',kind='units',directory='best',language='script/Cyrillic',oem=1,
        file='script/Cyrillic.traineddata',sha256='4ad9e44d219e2c18ac1d7dccc79863408acde090d9875908517606942a951018',
        repository='tessdata_best',revision=BEST_REVISION),
    dict(id='russian-legacy',kind='units',directory='legacy',language='rus',oem=0,
        file='rus.traineddata',sha256='681be2c2bead1bc7bd235df88c44e8e60ae73ae866840c0ad4e3b4c247bd37c2',
        repository='tessdata',revision='4.1.0'),
    dict(id='english-best',kind='values',directory='best',language='eng',oem=1,
        file='eng.traineddata',sha256='8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba',
        repository='tessdata_best',revision=BEST_REVISION),
)


@lru_cache(maxsize=4)
def _verified(root) -> tuple[dict[str,Any],...]:
    models: list[dict[str,Any]]=[]
    for model in MODELS:
        directory=Path(root)/model['directory']
        path=directory/model['file']
        if not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest()!=model['sha256']:
            raise ValueError('Paired OCR model hash mismatch: '+model['id'])
        models.append(dict(model,tessdata_dir=str(directory)))
    return tuple(models)


def configured():
    return _verified(os.environ.get('SMETA_PAIRED_TESSDATA_ROOT',ROOT))


def signature():
    models=configured()
    if not models:
        return None
    identity=dict(models=[{key:model[key]for key in ('id','sha256','oem','language')}for model in models],
        plans=[dict(model_id=plan['model']['id'],height=plan['height'],psm=plan['psm'],
            preprocessing=plan['preprocessing'],resampling=plan.get('resampling','lanczos'))
            for kind in ('units','values')for plan in variants(kind)])
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()


def variants(kind) -> Iterator[dict[str,Any]]:
    """Fixed optical plans, independent of the expected spelling or quantity."""
    for model in configured():
        if model['kind']!=kind:
            continue
        if model['id']=='cyrillic-best':
            for height in (24,32):
                yield dict(height=height,psm=7,preprocessing='gray',model=model)
        else:
            for height in ((32,64)if kind=='units'else(32,48)):
                yield dict(height=height,psm=7,preprocessing='binary',model=model)
            if kind=='units':
                for height in (32,64):
                    yield dict(height=height,psm=7,preprocessing='gray',model=model)
        if kind=='units':
            # Binary strokes can be diluted by interpolation on very short
            # labels. Preserve their edges at two fixed, distinct heights.
            for height in (32,64):
                yield dict(height=height,psm=7,preprocessing='binary',resampling='nearest',model=model)
