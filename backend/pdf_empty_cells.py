"""Distinguish a physical standalone horizontal stroke from empty paper or zero."""
VERSION = 'physical-printed-dash-v1'


def empty_paper(image):
    """Prove literal blankness only after removing long physical grid lines."""
    import cv2
    import numpy as np
    gray = np.array(image.convert('L'))
    if gray.size > 4_000_000 or min(gray.shape) < 8 or float(np.median(gray)) < 250:
        return None
    height, width = gray.shape
    ink = (gray < 250).astype('uint8') * 255
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(8, round(height * .85)))))
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(8, round(width * .85)), 1)))
    # Geometry-derived cell bboxes can cut a border rule lengthwise. A column
    # or row is still a rule when most of it is ink: remove it wherever it
    # lies, and also remove short edge-attached stroke stubs so a split border
    # does not become a text candidate. Nothing interior is removed by position.
    tall = np.where(vertical.sum(axis=0) >= height * 200)[0]
    wide = np.where(horizontal.sum(axis=1) >= width * 200)[0]
    rules = np.zeros_like(ink)
    rules[:, tall] = 255
    rules[wide, :] = 255
    edge = np.zeros_like(ink)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = 255
    stubs = cv2.bitwise_and(ink, cv2.dilate(cv2.bitwise_and(ink, edge), np.ones((3, 3), np.uint8)))
    remaining = cv2.bitwise_and(ink, cv2.bitwise_not(cv2.bitwise_or(rules, stubs)))
    count, _, stats, _ = cv2.connectedComponentsWithStats(remaining)
    if count > 1 and any(area >= 3 for area in stats[1:, cv2.CC_STAT_AREA]):
        return None
    return dict(version='physical-empty-paper-v1', image_size=[width, height],
        threshold=250, removed_only_long_grid_lines=True, meaning='empty_paper_not_numeric_zero')


def printed_dash(image):
    import cv2
    import numpy as np
    gray = np.array(image.convert('L'))
    if gray.size > 4_000_000 or min(gray.shape) < 8:
        return None
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink)
    height, width = gray.shape
    inside = [list(map(int, s)) for s in stats[1:] if s[4] >= 3 and
        s[0] > 1 and s[1] > 1 and s[0] + s[2] < width - 1 and s[1] + s[3] < height - 1]
    if len(inside) != 1:
        return None
    x, y, w, h, area = inside[0]
    if w < 3 * h or area < .7 * w * h or w > width * .7 or not .2 * height < y + h / 2 < .85 * height:
        return None
    return dict(version=VERSION, image_size=[width, height], components=inside,
        pixel_bbox=[x, y, x + w, y + h], meaning='printed_dash_not_numeric_zero')
