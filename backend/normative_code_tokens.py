"""Discovery aliases for spaced/mixed-alphabet engineering codes; no text rewriting."""
import re
import unicodedata

PATTERN=re.compile(r'(?<!\w)([A-ZА-ЯЁ]{1,4})(\s*[-–]?\s*)(\d+(?:[.,]\d+)*)'
    r'(?:(?P<attached>[A-Za-zА-Яа-яЁё]{1,4})|[ \t]+(?P<spaced>[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё]{0,3})\b)?')
LOOKALIKES=str.maketrans({'a':'а','b':'в','c':'с','e':'е','h':'н','k':'к','m':'м',
    'o':'о','p':'р','t':'т','x':'х','y':'у'})
STOP_PREFIXES={'СЛОЙ','ТИП','ПУТЬ','ОСЬ','ПК','СМ','ММ','КГ'}
STOP_SUFFIXES={'НА','ПО','В','И','С','ДО','ОТ','ПРИ','СМ','ММ','М','КГ','Т'}


def code_tokens(text):
    output=set()
    text=unicodedata.normalize('NFKC',str(text))
    for match in PATTERN.finditer(text):
        prefix=match[1]
        if prefix in STOP_PREFIXES or (match[2] and prefix in {'В','К','С','О','У','И'}):
            continue
        base=(prefix+match[3].replace(',','.')).casefold().translate(LOOKALIKES)
        output.add(base)
        suffix=match['attached'] or match['spaced']
        if suffix and suffix.upper() not in STOP_SUFFIXES:
            output.add(base+suffix.casefold().translate(LOOKALIKES))
    return output
