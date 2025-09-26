import { marked } from 'marked'
import React from 'react'

// Configure marked for safe, minimal rendering: no raw HTML, safe links, GFM
const renderer = new marked.Renderer()
// Drop raw HTML entirely
renderer.html = () => ''

marked.setOptions({
  renderer,
  gfm: true,
  breaks: true,
  smartypants: true,
})

marked.use({
  walkTokens: (token) => {
    if (token.type === 'link') {
      const href = (token as any).href as string
      const safe = href && /^(https?:|mailto:)/i.test(href)
      if (!safe) {
        ;(token as any).href = '#'
      }
    }
    if ((token as any).type === 'html') {
      ;(token as any).text = ''
    }
  },
})

function escapeHtml(input: string): string {
  return input
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

export function MarkdownMessage({ text }: { text: string }) {
  // Always escape first, then let marked interpret Markdown (no HTML allowed)
  const escaped = escapeHtml(text || '')
  const html = marked.parse(escaped)
  return <div className="md" dangerouslySetInnerHTML={{ __html: html as string }} />
}

export default MarkdownMessage


