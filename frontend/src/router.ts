import { createRouter, createWebHistory } from 'vue-router'
import { hasToken } from './services/api'
import LectureDetailView from './views/LectureDetailView.vue'
import LecturesView from './views/LecturesView.vue'
import LoginView from './views/LoginView.vue'
import OperationsDashboardView from './views/OperationsDashboardView.vue'
import OperationsQueueView from './views/OperationsQueueView.vue'
import OperationsBackfillView from './views/OperationsBackfillView.vue'
import OperationsRunsView from './views/OperationsRunsView.vue'
import PositiveMomentDetailView from './views/PositiveMomentDetailView.vue'
import PositiveMomentsView from './views/PositiveMomentsView.vue'

/**
 * Three areas, one product.
 *
 *   /operations        what needs attention today
 *   /lectures          the lecture workspace, and one lecture in full
 *   /positive-moments  the learner-moments feature
 *
 * The canonical lecture detail is keyed by `lecture_id`, which is always a
 * UUID. The constraint on the parameter is what lets the old bookmark shape
 * `/lectures/<legacy session key>` keep working: anything that is not a UUID
 * falls through to the redirect below and lands in the Positive Moments
 * feature, where that identifier actually belongs.
 */
const UUID = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/operations' },
    { path: '/login', name: 'login', component: LoginView, meta: { public: true, title: 'Sign in' } },

    // --- Operations ------------------------------------------------------
    { path: '/operations', name: 'operations', component: OperationsDashboardView, meta: { title: 'Operations' } },
    {
      path: '/operations/pending-attendance', name: 'pending-attendance',
      component: OperationsQueueView, meta: { title: 'Pending attendance', queue: 'attendance' },
    },
    {
      path: '/operations/manual-review', name: 'manual-review',
      component: OperationsQueueView, meta: { title: 'Manual review', queue: 'review' },
    },
    {
      path: '/operations/errors', name: 'operations-errors',
      component: OperationsQueueView, meta: { title: 'Errors', queue: 'errors' },
    },
    { path: '/operations/runs', name: 'operations-runs', component: OperationsRunsView, meta: { title: 'Pipeline runs' } },
    {
      // QA Core RC2. Historical recovery: pick a range, preview it, run it.
      path: '/operations/backfill', name: 'operations-backfill',
      component: OperationsBackfillView, meta: { title: 'Historical backfill' },
    },

    // --- Lectures --------------------------------------------------------
    { path: '/lectures', name: 'lectures', component: LecturesView, meta: { title: 'Lectures' } },
    {
      path: `/lectures/:lectureId(${UUID})`, name: 'lecture',
      component: LectureDetailView, meta: { title: 'Lecture' },
    },

    // --- Positive Moments ------------------------------------------------
    { path: '/positive-moments', name: 'positive-moments', component: PositiveMomentsView, meta: { title: 'Positive Moments' } },
    {
      path: '/positive-moments/:sessionKey', name: 'positive-moment-detail',
      component: PositiveMomentDetailView, meta: { title: 'Positive Moments' },
    },

    // --- kept so existing bookmarks do not break -------------------------
    {
      path: `/operations/lectures/:lectureId(${UUID})`,
      redirect: (to) => ({ name: 'lecture', params: to.params }),
    },
    { path: '/lectures/:sessionKey', redirect: (to) => ({ name: 'positive-moment-detail', params: to.params }) },

    { path: '/:pathMatch(.*)*', redirect: '/operations' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach((to) => {
  if (!to.meta.public && !hasToken()) return { name: 'login', query: { next: to.fullPath } }
  if (to.name === 'login' && hasToken()) return { name: 'operations' }
})

router.afterEach((to) => {
  document.title = typeof to.meta.title === 'string'
    ? `${to.meta.title} | KBC Lecture Intelligence`
    : 'KBC Lecture Intelligence'
})

export default router
