import { describe, expect, it } from 'vitest'
import { collectDropFiles, entryFiles, type DropEntry } from '../../app/utils/dropFiles'

function fileEntry(name: string): DropEntry {
  return {
    isFile: true,
    isDirectory: false,
    name,
    file: (success) => success(new File(['x'], name)),
    createReader: () => {
      throw new Error('file has no reader')
    },
  }
}

function directoryEntry(name: string, children: DropEntry[], batches?: DropEntry[][]): DropEntry {
  const queue = batches ?? [children]
  return {
    isFile: false,
    isDirectory: true,
    name,
    file: (_success, failure) => failure?.(new Error('is a directory')),
    createReader: () => {
      const pending = queue.map((batch) => [...batch])
      return {
        readEntries: (success) => success(pending.shift() ?? []),
      }
    },
  }
}

describe('folder drag&drop traversal', () => {
  it('collects nested files, preserving ordinary file drops', async () => {
    const nested = directoryEntry('root', [
      fileEntry('vor.xlsx'),
      directoryEntry('sub', [fileEntry('project.pdf')]),
    ])
    const files = await collectDropFiles({
      items: [{ webkitGetAsEntry: () => nested }],
      files: [],
    })
    expect(files.map((file) => file.name).toSorted()).toEqual(['project.pdf', 'vor.xlsx'])
  })

  it('reads directories in batches until an empty batch', async () => {
    const children = Array.from({ length: 150 }, (_, index) => fileEntry(`doc-${index}.pdf`))
    const batches = [children.slice(0, 100), children.slice(100), []]
    const files = await collectDropFiles({
      items: [{ webkitGetAsEntry: () => directoryEntry('big', [], batches) }],
      files: [],
    })
    expect(files).toHaveLength(150)
  })

  it('mixes files and directories in one drop', async () => {
    const files = await collectDropFiles({
      items: [
        { webkitGetAsEntry: () => fileEntry('loose.csv') },
        { webkitGetAsEntry: () => directoryEntry('folder', [fileEntry('nested.pdf')]) },
      ],
      files: [],
    })
    expect(files.map((file) => file.name).toSorted()).toEqual(['loose.csv', 'nested.pdf'])
  })

  it('falls back to the file list without entry support', async () => {
    const fallback = [new File(['x'], 'plain.pdf')]
    await expect(collectDropFiles({ items: [{}], files: fallback })).resolves.toEqual(fallback)
  })

  it('propagates traversal errors so the drop zone can report them', async () => {
    const broken: DropEntry = {
      isFile: true,
      isDirectory: false,
      name: 'broken.pdf',
      file: (_success, failure) => failure?.(new Error('unreadable')),
      createReader: () => {
        throw new Error('no reader')
      },
    }
    await expect(entryFiles(broken)).rejects.toThrow('unreadable')
  })
})
