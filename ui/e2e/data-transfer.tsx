import { createRoot } from 'react-dom/client'
import { DataTransferPage } from '../src/pages/DataTransferPage'
import '../src/styles/theme.css'
createRoot(document.getElementById('root')!).render(<main className="min-h-dvh bg-bg text-ink-1"><DataTransferPage /></main>)
