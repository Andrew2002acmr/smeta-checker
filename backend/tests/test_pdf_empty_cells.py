from PIL import Image, ImageDraw

from pdf_empty_cells import printed_dash, empty_paper


def cell():
    image = Image.new('L', (250, 70), 255)
    ImageDraw.Draw(image).rectangle((0, 0, 249, 69), outline=0, width=1)
    return image


def test_physical_dash_remains_a_non_numeric_mark():
    image = cell()
    ImageDraw.Draw(image).rectangle((115, 38, 135, 41), fill=0)
    assert printed_dash(image)['meaning'] == 'printed_dash_not_numeric_zero'
    assert empty_paper(image) is None


def test_blank_zero_digit_equals_and_negative_number_are_not_a_dash():
    assert printed_dash(cell()) is None
    assert empty_paper(cell())['meaning'] == 'empty_paper_not_numeric_zero'
    for shape in ('zero', 'one', 'equals', 'negative'):
        image = cell()
        draw = ImageDraw.Draw(image)
        if shape == 'zero':
            draw.ellipse((115, 15, 132, 48), outline=0, width=3)
        elif shape == 'one':
            draw.rectangle((120, 15, 123, 48), fill=0)
        else:
            draw.rectangle((100, 38, 118, 41), fill=0)
            draw.rectangle((100, 30, 118, 33) if shape == 'equals' else (130, 15, 134, 48), fill=0)
        assert printed_dash(image) is None
        assert empty_paper(image) is None


def test_faint_text_and_non_white_background_are_not_proven_empty():
    image = cell()
    ImageDraw.Draw(image).text((90, 25), '123', fill=235)
    assert empty_paper(image) is None
    assert empty_paper(Image.new('L', (100, 40), 245)) is None
