from copy import deepcopy

from pdf_unit_components import VERSION, verified_component_unit


def proof():
    def component(box, text, confidence):
        return dict(bbox=box, reads=[dict(height=h, words=[dict(text=text, conf=confidence)]) for h in (48, 64)])
    return dict(region=[0, 0, 70, 60], variants=[], component_reading=dict(version=VERSION,
        image_size=[70, 60], components=[component([53, 0, 17, 29], '3', 96),
            component([0, 27, 37, 33], 'М', 83)]))


def test_separate_glyph_reads_preserve_superscript_geometry():
    p = proof()
    assert verified_component_unit(p) == 'м3'
    for mutate in (
        lambda q: q['component_reading']['components'][0]['reads'][1]['words'][0].update(text='2'),
        lambda q: q['component_reading']['components'][0]['reads'][0]['words'][0].update(text='9'),
        lambda q: q['component_reading']['components'][0].update(bbox=[53, 28, 17, 29]),
        lambda q: q['component_reading']['components'][0].update(bbox=[80, 0, 17, 29]),
        lambda q: q['component_reading']['components'][0]['reads'][0]['words'][0].update(conf=60),
    ):
        bad = deepcopy(p)
        mutate(bad)
        assert verified_component_unit(bad) is None


def test_component_reading_cannot_outvote_confident_full_crop():
    p = proof()
    p['variants'] = [dict(words=[dict(text='м2', conf=96)])]
    assert verified_component_unit(p) is None
