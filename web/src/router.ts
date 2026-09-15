import { createRouter, createWebHistory } from 'vue-router'
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('./views/WorkbenchView.vue') },
    { path: '/tasks/:id', component: () => import('./views/WorkbenchView.vue') },
    { path: '/history', redirect: '/' },
    { path: '/datasets', component: () => import('./views/DatasetLibraryView.vue') },
    { path: '/reports', component: () => import('./views/ReportLibraryView.vue') },
    { path: '/reports/:id/versions/:versionId', component: () => import('./views/PublishedReportView.vue') },
    { path: '/settings', component: () => import('./views/SettingsView.vue') },
  ],
})
