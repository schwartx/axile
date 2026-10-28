import { browserAnalysis } from '../src/features/history/browserAnalysis'
Object.assign(window, { browserAnalysis })
document.querySelector('#result')!.textContent = '分析引擎已加载'
