import { describe, expect, it } from 'vitest'
import { selectionRectangles, selectionEnvelope } from '../../app/utils/pdfSelection'

describe('PDF text selection geometry', () => {
  const page = { left: 10, top: 20, width: 100, height: 100 }
  it('keeps separate line fragments rather than selecting their empty bounding gap', () => {
    const ranges = selectionRectangles(
      [
        { left: 20, top: 40, right: 80, bottom: 50 },
        { left: 40, top: 80, right: 90, bottom: 90 },
      ],
      page,
    )
    expect(ranges).toEqual([
      [0.1, 0.2, 0.7, 0.3],
      [0.3, 0.6, 0.8, 0.7],
    ])
    expect(selectionEnvelope(ranges)).toEqual([0.1, 0.2, 0.8, 0.7])
  })
  it('deduplicates range boxes and clips against the canvas, not its border', () => {
    const box = { left: 0, top: 0, right: 50, bottom: 50 }
    expect(selectionRectangles([box, box], page)).toEqual([[0, 0, 0.4, 0.3]])
  })
  it('rejects empty and nonfinite geometry', () => {
    expect(
      selectionRectangles(
        [
          { left: 20, top: 20, right: 20, bottom: 50 },
          { left: NaN, top: 0, right: 50, bottom: 50 },
        ],
        page,
      ),
    ).toEqual([])
    expect(selectionRectangles([], { ...page, width: 0 })).toEqual([])
  })
})
