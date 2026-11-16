<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'

const clock = ref('')
const savedClock = ref('')
const batches = ref([])
const lock = ref(null)
const conflicts = ref([])
const aborts = ref([])
const msg = ref('')
const err = ref('')
const result = ref(null)
const busy = ref(false)
const holdMs = ref(0)
let timer = null

const policy = computed(() => lock.value?.policy || 'apply_first')
const policyLabel = computed(() => lock.value?.policy_label || '应用优先')
const isHeld = computed(() => !!lock.value?.held)
const holder = computed(() => lock.value?.lock || null)

async function refresh() {
  const [c, b, l, cf, af] = await Promise.all([
    api.clock(),
    api.batches(),
    api.applyLock(),
    api.conflicts(),
    api.aborts(),
  ])
  clock.value = c.business_clock || ''
  savedClock.value = c.business_clock || ''
  batches.value = b
  lock.value = l
  conflicts.value = cf
  aborts.value = af
}

async function saveClock() {
  msg.value = ''
  err.value = ''
  try {
    const c = await api.setClock(clock.value)
    msg.value = `业务钟已更新为 ${c.business_clock}`
    await refresh()
  } catch (e) {
    err.value = e.message
    clock.value = savedClock.value
    await refresh().catch(() => {})
  }
}

async function applyDue() {
  msg.value = ''
  err.value = ''
  result.value = null
  busy.value = true
  try {
    result.value = await api.applyDue(holdMs.value || undefined)
    msg.value = result.value.applied
      ? `已应用 ${result.value.ticket_count} 张改价单（批次 ${result.value.batch_no}）`
      : result.value.message
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = false
    await refresh().catch(() => {})
  }
}

async function switchPolicy(next) {
  msg.value = ''
  err.value = ''
  try {
    const l = await api.setPolicy(next)
    lock.value = l
    msg.value = `让路策略已切换为「${l.policy_label}」（全局仅此一种生效）`
  } catch (e) {
    err.value = e.message
  }
}

async function recoverLock() {
  msg.value = ''
  err.value = ''
  try {
    lock.value = await api.recoverLock()
    msg.value = '已回收超时的临界区锁'
    await refresh()
  } catch (e) {
    err.value = e.message
  }
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    err.value = e.message
  }
  timer = setInterval(() => {
    api.applyLock().then((l) => (lock.value = l)).catch(() => {})
  }, 1500)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <section class="hero">
    <h1>生效应用</h1>
    <p>
      拨钟与应用到期共享同一生效临界区：同一时刻只允许一方持锁推进。冲突时按全局唯一的让路策略处理。
    </p>
  </section>

  <div class="panel">
    <h2>冲突让路策略</h2>
    <div class="row" style="align-items:center;flex-wrap:wrap">
      <span class="tag" :class="policy === 'apply_first' ? 'ok' : 'warn'">{{ policyLabel }}</span>
      <button class="btn ghost" type="button" :disabled="policy === 'apply_first'" @click="switchPolicy('apply_first')">
        切换为应用优先
      </button>
      <button class="btn ghost" type="button" :disabled="policy === 'clock_first'" @click="switchPolicy('clock_first')">
        切换为拨钟优先
      </button>
    </div>
    <ul class="policy-notes">
      <li><strong>应用优先</strong>：应用持锁期间一切拨钟立即失败；拨钟不改钟，也不迫使应用回滚。</li>
      <li><strong>拨钟优先</strong>：应用持锁期间收到拨钟，应用中止并整批回滚，随后拨钟生效；应用回执记冲突中止，不留半截 applied。</li>
    </ul>
    <p class="muted-note">两种策略不会并存：任一时刻只有当前选中这一种生效。</p>
  </div>

  <div class="panel">
    <h2>临界区锁</h2>
    <div class="row" style="flex-wrap:wrap">
      <span class="tag" :class="isHeld ? 'warn' : 'ok'">{{ isHeld ? '持有中' : '空闲' }}</span>
      <span v-if="lock" class="kv">超时阈值 {{ lock.lock_timeout_seconds }}s</span>
      <span v-if="holder" class="kv">持有者 {{ holder.holder }}</span>
      <span v-if="holder" class="kv">{{ holder.holder_kind === 'apply' ? '应用批次' : '拨钟' }}</span>
      <span v-if="holder" class="kv">获取于 {{ holder.acquired_at }}</span>
      <span v-if="holder" class="kv">到期 {{ holder.expires_at }}</span>
      <span v-if="holder" class="kv">栅栏 #{{ holder.fence }}</span>
      <button v-if="lock?.stale" class="btn ghost" type="button" @click="recoverLock">回收超时锁</button>
    </div>
  </div>

  <div class="panel">
    <h2>业务钟</h2>
    <div class="row">
      <div class="field" style="flex:1">
        <label>业务钟 (UTC)</label>
        <input v-model="clock" />
      </div>
      <div class="field" style="width:150px">
        <label>应用模拟耗时 (ms)</label>
        <input v-model.number="holdMs" type="number" min="0" step="100" />
      </div>
      <button class="btn ghost" type="button" @click="saveClock">保存业务钟</button>
      <button class="btn warn" type="button" :disabled="busy" @click="applyDue">
        {{ busy ? '应用中…' : '应用到期' }}
      </button>
    </div>
    <p class="muted-note">
      应用会读取到期集合、写价签/历史、更新单状态、落批次，四步同成同败。模拟耗时超过锁超时会触发整批回滚。
    </p>
    <p v-if="msg" class="msg ok">{{ msg }}</p>
    <p v-if="err" class="msg err">{{ err }}</p>
    <pre v-if="result" style="margin-top:12px;font-size:0.82rem;color:var(--muted);white-space:pre-wrap">{{ JSON.stringify(result, null, 2) }}</pre>
  </div>

  <div class="panel">
    <h2>应用批次</h2>
    <table>
      <thead>
        <tr><th>批次</th><th>业务钟</th><th>改价单数</th><th>明细行</th><th>状态</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="b in batches" :key="b.id">
          <td>{{ b.batch_no }}</td>
          <td>{{ b.clock_at }}</td>
          <td>{{ b.ticket_count }}</td>
          <td>{{ b.line_count }}</td>
          <td><span class="tag ok">{{ b.status }}</span></td>
          <td>{{ b.created_at }}</td>
        </tr>
        <tr v-if="!batches.length"><td colspan="6" class="muted-note">暂无批次</td></tr>
      </tbody>
    </table>
  </div>

  <div class="panel">
    <h2>冲突回执</h2>
    <table>
      <thead>
        <tr><th>类型</th><th>胜方</th><th>让路方</th><th>策略</th><th>说明</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="c in conflicts" :key="c.id">
          <td><span class="tag" :class="c.kind === 'apply_aborted' || c.kind === 'clock_rejected' ? 'bad' : 'muted'">{{ c.kind }}</span></td>
          <td>{{ c.winner }}</td>
          <td>{{ c.loser }}</td>
          <td>{{ c.policy }}</td>
          <td>{{ c.detail }}</td>
          <td>{{ c.created_at }}</td>
        </tr>
        <tr v-if="!conflicts.length"><td colspan="6" class="muted-note">暂无冲突</td></tr>
      </tbody>
    </table>
  </div>

  <div class="panel" v-if="aborts.length">
    <h2>应用中止（已整批回滚）</h2>
    <table>
      <thead>
        <tr><th>持有者</th><th>栅栏</th><th>原因</th><th>已写行</th><th>已回滚</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="a in aborts" :key="a.id">
          <td>{{ a.holder }}</td>
          <td>{{ a.fence }}</td>
          <td>{{ a.reason }}</td>
          <td>{{ a.written_lines }}</td>
          <td><span class="tag ok">{{ a.rolled_back ? '是' : '否' }}</span></td>
          <td>{{ a.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.policy-notes {
  margin: 12px 0 0;
  padding-left: 18px;
  color: var(--muted);
  font-size: 0.9rem;
  line-height: 1.7;
}
.policy-notes strong { color: var(--ink); }
.kv { font-size: 0.85rem; color: var(--muted); }
.muted-note { color: var(--muted); font-size: 0.85rem; margin-top: 8px; }
</style>
