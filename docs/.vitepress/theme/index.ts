import DefaultTheme from 'vitepress/theme'
import type { Theme } from 'vitepress'
import { useRoute } from 'vitepress'
import { nextTick, onMounted, onUnmounted, watch } from 'vue'
import mediumZoom, { type Zoom } from 'medium-zoom'
import './style.css'

export default {
  extends: DefaultTheme,
  setup() {
    const route = useRoute()
    let zoom: Zoom | undefined
    const attachImages = () => {
      zoom ??= mediumZoom({ background: 'var(--vp-c-bg)', margin: 24 })
      zoom.detach()
      const images = document.querySelectorAll<HTMLImageElement>('.vp-doc img, .home-preview img')
      images.forEach(image => {
        image.tabIndex = 0
        image.setAttribute('role', 'button')
        image.setAttribute('aria-label', `放大图片：${image.alt}`)
      })
      zoom.attach(images)
    }
    const openFromKeyboard = (event: KeyboardEvent) => {
      const image = document.activeElement
      if ((event.key === 'Enter' || event.key === ' ') && image instanceof HTMLImageElement
        && image.classList.contains('medium-zoom-image')) {
        event.preventDefault()
        void zoom?.open({ target: image })
      }
    }
    onMounted(() => {
      attachImages()
      document.addEventListener('keydown', openFromKeyboard)
    })
    watch(() => route.path, async () => {
      await nextTick()
      attachImages()
    })
    onUnmounted(() => {
      zoom?.detach()
      document.removeEventListener('keydown', openFromKeyboard)
    })
  },
} satisfies Theme
