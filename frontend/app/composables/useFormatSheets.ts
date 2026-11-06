import type { Ref } from 'vue'
import type { FormatDto } from '~/generated/types.gen'

export interface SheetOption {
  label: string
  value: string
}

export function useFormatSheets(result: Ref<FormatDto | null>) {
  return computed<SheetOption[]>(() =>
    (result.value?.sheets ?? []).map((name) => ({ label: name, value: name })),
  )
}
