import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@/styles/theme.css'
import '@/stores/accessibility'
import '@/components/ui/pythonQuickFixMenu.css'
import { commitFonts, prepareCachedFonts, warmFonts } from '@/fonts'

void prepareCachedFonts()
void import('@/App').then(({ default: App }) => {
  commitFonts()
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
  warmFonts()
})
