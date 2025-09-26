import * as React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './index.css'

// Ensure React app can run under base '/static/' without router

const container = document.getElementById('root')!
const root = createRoot(container)
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
