// React 19 callback refs dispose these listeners when the textarea unmounts.
export function keepEditorVisible(textarea: HTMLTextAreaElement | null) {
  const viewport = window.visualViewport
  const scroller = textarea?.closest('main')
  if (!textarea || !viewport || !scroller) return

  const touchScreen = window.matchMedia('(any-pointer: coarse)')
  const originalPadding = textarea.style.paddingBottom
  let bottomPadding: number | undefined
  let frame = 0

  // A textarea exposes selection offsets, but no cursor rectangle. Mirror its
  // typography and wrapping to locate the selected line, including soft wraps.
  const mirror = document.createElement('div')
  mirror.setAttribute('aria-hidden', 'true')
  Object.assign(mirror.style, {
    position: 'fixed',
    left: '-10000px',
    top: '0',
    visibility: 'hidden',
    pointerEvents: 'none',
  })

  const restorePadding = () => {
    scroller.removeAttribute('data-editor-keyboard-open')
    textarea.style.paddingBottom = originalPadding
    bottomPadding = undefined
    mirror.remove()
  }

  const revealCursor = () => {
    frame = 0
    if (document.activeElement !== textarea || !touchScreen.matches) {
      restorePadding()
      return
    }

    // Ignore browser toolbar changes and normalize pinch zoom. A substantial
    // loss of visible height while editing indicates the software keyboard.
    scroller.toggleAttribute('data-editor-keyboard-open',
      window.innerHeight - viewport.height * viewport.scale > 100)

    const scrollBounds = scroller.getBoundingClientRect()
    const viewportBottom = viewport.offsetTop + viewport.height
    const style = getComputedStyle(textarea)
    bottomPadding ??= parseFloat(style.paddingBottom)
    // The layout viewport can stay full height behind the keyboard. Extra
    // trailing space lets the outer scroller bring even the final line above it.
    textarea.style.paddingBottom = `${bottomPadding + Math.max(0, scrollBounds.bottom - viewportBottom)}px`

    for (const property of [
      'box-sizing', 'font-family', 'font-size', 'font-weight', 'font-style',
      'font-variant', 'line-height', 'letter-spacing', 'word-spacing',
      'text-indent', 'text-transform', 'text-align', 'direction', 'tab-size',
      'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
      'border-top-width', 'border-right-width', 'border-bottom-width', 'border-left-width',
      'border-style', 'white-space', 'overflow-wrap', 'word-break',
    ]) {
      mirror.style.setProperty(property, style.getPropertyValue(property))
    }
    mirror.style.width = `${textarea.getBoundingClientRect().width}px`
    const cursor = textarea.selectionDirection === 'backward'
      ? textarea.selectionStart : textarea.selectionEnd
    const marker = document.createElement('span')
    // Keep the remaining text so a word crossing the cursor wraps identically.
    marker.textContent = textarea.value.slice(cursor) || '\u200b'
    mirror.replaceChildren(document.createTextNode(textarea.value.slice(0, cursor)), marker)
    if (!mirror.isConnected) document.body.append(mirror)

    const markerBounds = marker.getClientRects()[0]
    if (!markerBounds) return
    const cursorTop = textarea.getBoundingClientRect().top - textarea.scrollTop
      + markerBounds.top - mirror.getBoundingClientRect().top
    const cursorBottom = cursorTop + markerBounds.height
    const visibleTop = Math.max(scrollBounds.top, viewport.offsetTop) + 16
    // Leave a little context below the line, above the keyboard.
    const visibleBottom = Math.min(scrollBounds.bottom, viewportBottom) - 48
    if (cursorBottom > visibleBottom) {
      scroller.scrollTop += cursorBottom - visibleBottom
    } else if (cursorTop < visibleTop) {
      scroller.scrollTop -= visibleTop - cursorTop
    }
  }

  const schedule = () => {
    cancelAnimationFrame(frame)
    frame = requestAnimationFrame(revealCursor)
  }
  const events = ['focus', 'blur', 'input', 'select', 'click', 'keyup'] as const
  for (const event of events) textarea.addEventListener(event, schedule)
  viewport.addEventListener('resize', schedule)
  viewport.addEventListener('scroll', schedule)

  return () => {
    cancelAnimationFrame(frame)
    for (const event of events) textarea.removeEventListener(event, schedule)
    viewport.removeEventListener('resize', schedule)
    viewport.removeEventListener('scroll', schedule)
    restorePadding()
  }
}
