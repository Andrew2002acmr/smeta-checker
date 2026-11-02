from copy import deepcopy

from pdf_header_geometry import VERSION,geometry_text,numeric_region,whole_line


def word(text,conf=96):
    return dict(text=text,conf=conf)


def proof():
    # Synthetic physical components: h, =, 0, comma, 2, 4, m.
    components=[[0,0,30,46,600],[40,20,24,5,100],[40,33,24,5,100],
        [78,0,28,46,700],[109,40,10,15,100],[130,0,34,46,600],
        [170,0,29,46,530],[208,14,37,32,610]]
    size=[250,56]
    return dict(variants=[dict(scale=1,words=[word('h=0,24m',79)]),
        dict(scale=1.35,words=[word('h=0,24m',60)])],geometry_crop=dict(version=VERSION,
        rapid_words=[dict(text='h=0,24м',confidence=.93)],components=components,image_size=size,
        region=numeric_region(components,size),variants=[dict(height=h,words=[word('0,24')]) for h in (32,48)]))


def test_geometry_needs_strict_numeric_crops_and_independent_whole_literal():
    p=proof()
    assert whole_line(p['variants'])=='h=0,24m'
    assert geometry_text(p)=='h=0,24m'
    for mutate in [
        lambda p:p['geometry_crop']['rapid_words'][0].update(text='h=0,20m'),
        lambda p:p['geometry_crop']['rapid_words'][0].update(text='h=0,24cm'),
        lambda p:p['geometry_crop']['rapid_words'][0].update(confidence=.89),
        lambda p:p['variants'][1]['words'][0].update(text='h=0,20m'),
        lambda p:p['variants'][1]['words'][0].update(text='h=0,24m2'),
        lambda p:p['variants'][0]['words'][0].update(text='Материал h=0,24m'),
        lambda p:p['geometry_crop']['variants'][1]['words'][0].update(conf=89),
        lambda p:p['geometry_crop']['variants'][1]['words'][0].update(text='0,20'),
        lambda p:p['geometry_crop']['variants'][1]['words'][0].update(text='0,24m'),
        lambda p:p['geometry_crop']['variants'][1].update(height=32),
    ]:
        bad=deepcopy(p)
        mutate(bad)
        assert geometry_text(bad) is None


def test_ambiguous_trademark_glyph_requires_independent_literal_and_numeric_proof():
    p=proof()
    p['variants'][0]['words'][0]['text']='h=0,24™'
    assert whole_line(p['variants']) is None
    assert geometry_text(p)=='h=0,24m'
    for change in ['rapid','numeric','both_ambiguous','different_number']:
        bad=deepcopy(p)
        if change=='rapid':
            bad['geometry_crop']['rapid_words'][0]['text']='h=0,20м'
        elif change=='numeric':
            bad['geometry_crop']['variants'][1]['words'][0]['text']='0,20'
        elif change=='both_ambiguous':
            bad['variants'][1]['words'][0]['text']='h=0,24™'
        else:
            bad['variants'][0]['words'][0]['text']='h=0,20™'
        assert geometry_text(bad) is None


def test_geometry_preserves_conflicting_numeric_attempt_as_blocker():
    p=proof()
    p['geometry_crop']['variants'].append(dict(height=64,words=[word('0,20',75)]))
    assert geometry_text(p) is None


def test_adjacent_separate_glyph_boxes_do_not_require_a_blank_pixel_column():
    p=proof()
    components=p['geometry_crop']['components']
    right=max(c[0]+c[2] for c in components[3:-1])
    components[-1][0]=right
    region=numeric_region(components,p['geometry_crop']['image_size'])
    assert region and region[2]==right
    p['geometry_crop']['region']=region
    assert geometry_text(p)=='h=0,24m'
    components[-1][0]=right-1
    assert numeric_region(components,p['geometry_crop']['image_size']) is None


def test_geometry_crop_must_be_the_unambiguous_physical_region_within_source():
    p=proof()
    assert p['geometry_crop']['region']==[76,0,201,56]
    bad=deepcopy(p)
    bad['geometry_crop']['region']=[76,0,251,56]
    assert geometry_text(bad) is None
    bad=deepcopy(p)
    bad['geometry_crop']['components'][-1][0]=245
    assert geometry_text(bad) is None  # Component extends beyond its own crop.
    bad=deepcopy(p)
    bad['geometry_crop']['components'].extend([[280,20,24,5,100],[280,33,24,5,100]])
    bad['geometry_crop']['image_size']=[330,56]
    assert geometry_text(bad) is None  # Two possible equals signs.
    bad=deepcopy(p)
    bad['geometry_crop']['components'].append([246,0,4,15,30])
    assert geometry_text(bad) is None  # Unread superscript beside the unit.


def test_geometry_does_not_accept_a_partial_formula_or_numeric_confidence_reduction():
    p=proof()
    for text in ['0,24m','h=0,24mm','h=0,24m, слой','h=0,24m³','h=0,24?','H=0,24m + h=0,10m']:
        bad=deepcopy(p)
        for variant in bad['variants']:
            variant['words'][0]['text']=text
        bad['geometry_crop']['rapid_words'][0]['text']=text
        assert geometry_text(bad) is None
    for variant in p['geometry_crop']['variants']:
        variant['words'][0]['conf']=60
    assert geometry_text(p) is None
