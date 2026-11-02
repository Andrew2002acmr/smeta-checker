"""Equivalent wording for explicit equipment size classes in search queries only."""
import re

ORDINALS={'1':'первого','2':'второго','3':'третьего','4':'четвертого','5':'пятого',
    '6':'шестого','7':'седьмого','8':'восьмого','9':'девятого','10':'десятого'}
TYPE=re.compile(r'(?<![\w.,/–—-])(?P<number>10|[1-9])(?:[-‑–]?(?:го|ого|й))?\s*типоразмер(?:а|ом|у)?\b',re.I)


def machine_query(query):
    """Do not rewrite quantities, model numbers, ranges or a negated class.

    Numeric and ordinal spellings name the same explicit class. Nothing here
    infers a class from a machine brand or chooses/applies a normative code.
    """
    if re.search(r'\d+\s*(?:[-–—/]|или|и)\s*\d+(?:[-‑–]?(?:го|ого|й))?\s*типоразмер',query,re.I):
        return query
    def replace(match):
        if re.search(r'\b(?:не|не менее|не более|от|до|более|менее|около)\s*$',query[:match.start()],re.I):
            return match[0]
        return ORDINALS[match['number']]+' типоразмера'
    return TYPE.sub(replace,query)
