import { useEffect, useRef, useState } from 'react'
import './App.css'

type Message = { id: string; role: 'user' | 'model'; text: string; ts: string }
type ToolEvent = { id: string; callId: string; toolName: string; serverName?: string; args?: Record<string, unknown>; data?: unknown; error?: { message: string; structured_content?: unknown }; ts: string; phase: 'call' | 'result' | 'error' }

function App() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [toolEvents, setToolEvents] = useState<ToolEvent[]>([])
  const [input, setInput] = useState('')
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    // Create session on mount
    fetch('/api/session', { method: 'POST' })
      .then((r) => r.json())
      .then((j) => {
        setSessionId(j.sessionId)
        const es = new EventSource(`/api/session/${j.sessionId}/events`)
        es.onmessage = (ev) => {
          const evt = JSON.parse(ev.data)
          if (evt.type === 'message.user') {
            setMessages((m) => m.concat({ id: crypto.randomUUID(), role: 'user', text: evt.payload.text, ts: evt.ts }))
          } else if (evt.type === 'message.model.final') {
            setMessages((m) => m.concat({ id: crypto.randomUUID(), role: 'model', text: evt.payload.text, ts: evt.ts }))
          } else if (evt.type === 'tool_call.started') {
            setToolEvents((t) => t.concat({
              id: crypto.randomUUID(),
              callId: evt.payload.callId,
              toolName: evt.payload.toolName ?? evt.payload.toolFqn,
              serverName: evt.payload.serverName ?? undefined,
              args: evt.payload.args,
              phase: 'call',
              ts: evt.ts,
            }))
          } else if (evt.type === 'tool_call.result') {
            setToolEvents((t) => t.map((item) => item.callId === evt.payload.callId ? { ...item, phase: 'result', data: evt.payload.data, ts: evt.ts } : item))
          } else if (evt.type === 'tool_call.error') {
            setToolEvents((t) => t.map((item) => item.callId === evt.payload.callId ? { ...item, phase: 'error', error: { message: evt.payload.message, structured_content: evt.payload.structured_content }, ts: evt.ts } : item))
          }
        }
        esRef.current = es
      })
  }, [])

  const send = async () => {
    if (!sessionId || !input.trim()) return
    await fetch(`/api/session/${sessionId}/message`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: input }) })
    setInput('')
  }

  const reset = async () => {
    if (!sessionId) return
    await fetch(`/api/session/${sessionId}/reset`, { method: 'POST' })
    setMessages([])
    setToolEvents([])
  }

  return (
    <div className="app">
      <header className="header">
        <h1>MCP BOT</h1>
        <button className="reset" onClick={reset}>Reset</button>
      </header>
      <main className="chat">
        <section className="messages">
          {messages.map((m) => (
            <div key={m.id} className={`msg ${m.role === 'user' ? 'right' : 'left'}`}>
              <div className="bubble">{m.text}</div>
            </div>
          ))}
        </section>
        <aside className="tools">
          <h3>Tool Calls</h3>
          <div className="tool-list">
            {toolEvents.map((e) => (
              <div key={e.callId} className={`tool-card ${e.phase}`}>
                <div className="tool-header">
                  <div className="tool-title">{e.toolName}</div>
                  {e.serverName && <div className="tool-server">from {e.serverName}</div>}
                </div>
                <div className="tool-body">
                  <div className="tool-section">
                    <div className="tool-label">Input:</div>
                    <pre className="tool-json">{formatArgsBlock(e.args)}</pre>
                  </div>
                  {e.phase === 'result' && (
                    <div className="tool-section">
                      <div className="tool-label">Output:</div>
                      <pre className="tool-json">{formatOutputBlock(e.data)}</pre>
                    </div>
                  )}
                  {e.phase === 'error' && e.error && (
                    <div className="tool-section">
                      <div className="tool-label error">Error:</div>
                      <pre className="tool-json error">{e.error.message}</pre>
                    </div>
                  )}
                  {e.phase === 'call' && (
                    <div className="tool-status">Running…</div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </aside>
      </main>
      <footer className="inputbar">
        <input
          type="text"
          placeholder="Type your prompt..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
        />
        <button onClick={send}>Send</button>
      </footer>
    </div>
  )
}

function formatArgsBlock(args?: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) {
    return '{\n}'
  }
  return formatBlock(args)
}

function formatOutputBlock(data: unknown): string {
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    return formatBlock(data as Record<string, unknown>)
  }
  return formatBlock({ result: data })
}

function formatBlock(obj: Record<string, unknown>): string {
  const entries = Object.entries(obj)
  if (entries.length === 0) {
    return '{\n}'
  }
  const maxKeyLength = entries.reduce((max, [key]) => Math.max(max, key.length), 0)
  const lines = entries.map(([key, value]) => {
    const paddedKey = key.padEnd(maxKeyLength, ' ')
    const formattedValue = formatValue(value)
    return `  ${paddedKey}: ${formattedValue}`
  })
  return `{
${lines.join('\n')}
}`
}

function formatValue(value: unknown): string {
  if (value === null) return 'null'
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]'
    const content = value
      .map((item) => `    - ${formatValue(item)}`)
      .join('\n')
    return `\n${content}`
  }
  if (typeof value === 'object') {
    const block = formatBlock(value as Record<string, unknown>)
      .split('\n')
      .map((line) => `  ${line}`)
      .join('\n')
    return `\n${block}`
  }
  if (typeof value === 'string') {
    return value
  }
  return String(value)
}

export default App
