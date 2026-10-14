import { createContext } from 'react'

/** 已有弹窗中的错误就地展开，避免叠加第二个焦点陷阱。 */
export const ModalContentContext = createContext(false)
