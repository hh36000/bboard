import type { ComponentProps } from 'react'
import { keepEditorVisible } from '@/lib/keep-editor-visible'

export default function BoardTextArea(props: ComponentProps<'textarea'>) {
  return (
    <textarea
      {...props}
      ref={keepEditorVisible}
      spellCheck={false}
      className="block min-h-full w-full field-sizing-content resize-none overflow-hidden border-0 bg-background p-6 pb-12 font-mono text-sm any-pointer-coarse:text-base leading-6 text-foreground placeholder:text-muted-foreground"
      style={{ outline: 'none', boxShadow: 'none', borderRadius: 0 }}
    />
  )
}
