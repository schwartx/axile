import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { InitWizard } from '../src/features/init/InitWizard'
import '../src/styles/theme.css'

createRoot(document.getElementById('root')!).render(<MemoryRouter>
  <InitWizard mode="edit" editSection="advanced" initial={{
    sqlalchemy_database_uri: '', exe_err_feishu_key: '', system_execution_notification_mode: 'default',
    system_execution_notification_code: '', environment: 'local', app_log_dir: './logs',
    axile_log_rotation: '1 day', algorithm_modules: [], algorithm_directories: [],
  }} />
</MemoryRouter>)
