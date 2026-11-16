async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  if (!res.ok) {
    let detail = await res.text()
    try {
      const j = JSON.parse(detail)
      detail = j.detail || detail
    } catch (_) {}
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  health: () => request('/api/health'),
  dashboard: () => request('/api/dashboard'),
  skus: () => request('/api/skus'),
  tickets: () => request('/api/tickets'),
  ticket: (id) => request(`/api/tickets/${id}`),
  createTicket: (body) => request('/api/tickets', { method: 'POST', body: JSON.stringify(body) }),
  clock: () => request('/api/apply/clock'),
  setClock: (business_clock) =>
    request('/api/apply/clock', { method: 'PUT', body: JSON.stringify({ business_clock }) }),
  applyDue: (hold_ms) =>
    request(`/api/apply/due${hold_ms ? `?hold_ms=${encodeURIComponent(hold_ms)}` : ''}`, { method: 'POST' }),
  batches: () => request('/api/apply/batches'),
  applyLock: () => request('/api/apply/lock'),
  recoverLock: () => request('/api/apply/lock/recover', { method: 'POST' }),
  policy: () => request('/api/apply/policy'),
  setPolicy: (policy, lock_timeout_seconds) =>
    request('/api/apply/policy', {
      method: 'PUT',
      body: JSON.stringify({ policy, lock_timeout_seconds }),
    }),
  conflicts: () => request('/api/apply/conflicts'),
  aborts: () => request('/api/apply/aborts'),
  priceBook: () => request('/api/prices/book'),
  history: (sku) => request(sku ? `/api/prices/history?sku_code=${encodeURIComponent(sku)}` : '/api/prices/history'),
  reconcile: () => request('/api/reconcile/sales'),
}
