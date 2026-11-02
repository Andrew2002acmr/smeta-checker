/** Preserve disjoint line ranges; never turn text selection into one crop. */
export function selectionRectangles(
  rectangles: Pick<DOMRect, 'left' | 'top' | 'right' | 'bottom'>[],
  page: Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>,
): number[][] {
  if (page.width <= 0 || page.height <= 0) return []
  const result: number[][] = []
  for (const box of rectangles) {
    const rect = [
      Math.max(0, (box.left - page.left) / page.width),
      Math.max(0, (box.top - page.top) / page.height),
      Math.min(1, (box.right - page.left) / page.width),
      Math.min(1, (box.bottom - page.top) / page.height),
    ]
    if (!rect.every(Number.isFinite) || rect[2]! <= rect[0]! || rect[3]! <= rect[1]!) continue
    if (
      !result.some((existing) =>
        existing.every((value, i) => Math.abs(value - rect[i]!) < 0.000001),
      )
    )
      result.push(rect)
  }
  return result
}

export function selectionEnvelope(rectangles: number[][]): number[] {
  return [
    Math.min(...rectangles.map((r) => r[0]!)),
    Math.min(...rectangles.map((r) => r[1]!)),
    Math.max(...rectangles.map((r) => r[2]!)),
    Math.max(...rectangles.map((r) => r[3]!)),
  ]
}
