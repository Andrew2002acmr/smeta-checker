export default defineAppConfig({
  ui: {
    colors: {
      primary: 'orange',
      neutral: 'zinc',
      success: 'green',
      warning: 'amber',
      error: 'red',
      info: 'blue',
    },
    button: {
      slots: { base: 'min-h-11 min-w-11 justify-center font-semibold' },
      defaultVariants: { size: 'md' },
    },
    input: { slots: { base: 'min-h-11 text-base' } },
    select: {
      slots: {
        base: 'min-h-11 text-base',
        content: 'w-max min-w-(--reka-select-trigger-width) max-w-[min(40rem,calc(100vw-2rem))]',
        itemLabel: 'whitespace-normal break-words',
        itemDescription: 'whitespace-normal break-words',
      },
    },
    selectMenu: {
      slots: {
        content: 'w-max min-w-(--reka-combobox-trigger-width) max-w-[min(40rem,calc(100vw-2rem))]',
        itemLabel: 'whitespace-normal break-words',
        itemDescription: 'whitespace-normal break-words',
      },
    },
  },
})
