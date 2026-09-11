import { createRouter, createWebHistory } from 'vue-router'
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('./views/WorkbenchView.vue') },
    { path: '/tasks/:id', component: () => import('./views/WorkbenchView.vue') },
    { path: '/history', component: () => import('./views/HistoryView.vue') },
    { path: '/datasets', component: () => import('./views/DatasetLibraryView.vue') },
    { path: '/knowledge', component: () => import('./views/KnowledgeLibraryView.vue') },
    { path: '/reports', component: () => import('./views/ReportLibraryView.vue') },
  ],
})
