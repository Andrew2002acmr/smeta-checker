export interface DropDirectoryReader {
  readEntries(success: (entries: DropEntry[]) => void, failure?: (error: unknown) => void): void
}

export interface DropEntry {
  readonly isFile: boolean
  readonly isDirectory: boolean
  readonly name: string
  file(success: (file: File) => void, failure?: (error: unknown) => void): void
  createReader(): DropDirectoryReader
}

export interface DropSource {
  readonly items?: ArrayLike<{ webkitGetAsEntry?: unknown }> | null
  readonly files?: ArrayLike<File> | null
}

/** Read a directory reader until an empty batch; a single readEntries call returns at most ~100 entries. */
export async function readReaderEntries(reader: DropDirectoryReader): Promise<DropEntry[]> {
  const all: DropEntry[] = []
  let batch: DropEntry[]
  do {
    batch = await new Promise<DropEntry[]>((resolve, reject) => reader.readEntries(resolve, reject))
    all.push(...batch)
  } while (batch.length)
  return all
}

/** Resolve a dropped entry to files, recursing into directories. Rejects on read errors. */
export async function entryFiles(entry: DropEntry): Promise<File[]> {
  if (entry.isDirectory) {
    const children = await readReaderEntries(entry.createReader())
    return (await Promise.all(children.map(entryFiles))).flat()
  }
  if (entry.isFile) {
    return [await new Promise<File>((resolve, reject) => entry.file(resolve, reject))]
  }
  return []
}

/**
 * Collect files from a drop source. Uses entry traversal when the browser
 * exposes dropped folders; falls back to the plain file list otherwise.
 */
export async function collectDropFiles(source: DropSource): Promise<File[]> {
  const items = source.items ? Array.from(source.items) : []
  const entries: DropEntry[] = []
  for (const item of items) {
    const get = item.webkitGetAsEntry
    if (typeof get !== 'function') continue
    const entry = (get as () => unknown).call(item) as DropEntry | null
    if (entry) entries.push(entry)
  }
  if (!entries.length) return source.files ? Array.from(source.files) : []
  return (await Promise.all(entries.map(entryFiles))).flat()
}
