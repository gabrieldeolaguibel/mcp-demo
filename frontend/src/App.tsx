import { useEffect, useRef, useState } from 'react'
import './App.css'

type Message = { id: string; role: 'user' | 'model'; text: string; ts: string }
type ToolEvent = { id: string; toolFqn: string; args?: any; data?: any; error?: { message: string; structured_content?: any }; ts: string }

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
            setToolEvents((t) => t.concat({ id: crypto.randomUUID(), toolFqn: evt.payload.toolFqn, args: evt.payload.args, ts: evt.ts }))
          } else if (evt.type === 'tool_call.result') {
            setToolEvents((t) => t.concat({ id: crypto.randomUUID(), toolFqn: evt.payload.toolFqn, data: evt.payload.data, ts: evt.ts }))
          } else if (evt.type === 'tool_call.error') {
            setToolEvents((t) => t.concat({ id: crypto.randomUUID(), toolFqn: evt.payload.toolFqn, error: { message: evt.payload.message, structured_content: evt.payload.structured_content }, ts: evt.ts }))
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
              <div key={e.id} className="tool-card">
                <div className="tool-fqn">{e.toolFqn}</div>
                {e.args && <pre className="tool-json">[Call] {JSON.stringify(e.args, null, 2)}</pre>}
                {e.data && <pre className="tool-json">[Result] {JSON.stringify(e.data, null, 2)}</pre>}
                {e.error && <pre className="tool-json error">[Error] {e.error.message}</pre>}
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

export default App
