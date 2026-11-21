import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import SemanticVersioning from '@/views/SemanticVersioning.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/semantic-versioning', name: 'semantic-versioning', component: SemanticVersioning },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
