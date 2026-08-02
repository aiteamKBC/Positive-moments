import { createRouter, createWebHistory } from 'vue-router'

import { hasToken } from './services/api'
import LectureDetailView from './views/LectureDetailView.vue'
import LecturesView from './views/LecturesView.vue'
import LoginView from './views/LoginView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/lectures' },
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    { path: '/lectures', name: 'lectures', component: LecturesView },
    { path: '/lectures/:sessionId', name: 'lecture-detail', component: LectureDetailView },
    { path: '/:pathMatch(.*)*', redirect: '/lectures' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach((to) => {
  if (!to.meta.public && !hasToken()) {
    return { name: 'login', query: { next: to.fullPath } }
  }
  if (to.name === 'login' && hasToken()) {
    return { name: 'lectures' }
  }
})

export default router

