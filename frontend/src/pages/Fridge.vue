<template>
  <div>
    <h1>冰箱分层</h1>
    <p class="muted">竖列分层 · FEFO 消费走「消费」页 · 同品同到期日的在架批可从总表发起合并</p>
    <div class="fridge">
      <section v-for="L in layers" :key="L" class="shelf">
        <h3>{{ label[L] }}</h3>
        <span v-for="x in by(L)" :key="x.id" class="lot">{{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}</span>
      </section>
    </div>
    <div v-if="mergeGroups.length" style="margin-top:12px">
      <h3>可合并（同品 · 同到期日 · 同隔离资格）</h3>
      <div v-for="g in mergeGroups" :key="g.key" class="shelf merge-group">
        <span v-for="x in g.lots" :key="x.id" class="lot">lot #{{ x.id }} ×{{ x.qty_remain }}</span>
        <button type="button" @click="goMerge(g)">去合并（{{ g.lots.length }} 条 → 1 条）</button>
      </div>
    </div>
    <button style="margin-top:12px" @click="sweep">过期下架</button>
  </div>
</template>
<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
const router = useRouter()
const rows = ref([])
const layers = ['upper','mid','lower']
const label = { upper: '上层', mid: '中层', lower: '下层' }
function by(L) { return rows.value.filter(r => r.layer === L) }
// 与合并页、扣减候选同一套门：在架且正余量；同品 · 同到期日 · 同 data_quality 才成组
const mergeGroups = computed(() => {
  const m = new Map()
  for (const r of rows.value) {
    if (!(r.qty_remain > 0)) continue
    const key = [r.item_id, r.expiry, r.data_quality || 'clean'].join('|')
    if (!m.has(key)) m.set(key, { key, lots: [] })
    m.get(key).lots.push(r)
  }
  return [...m.values()].filter(g => g.lots.length >= 2)
})
function goMerge(g) { router.push({ path: '/merge', query: { lots: g.lots.map(l => l.id).join(',') } }) }
async function load() { rows.value = await api('/fridge') }
async function sweep() { await api('/expire-sweep', { method: 'POST', body: '{}' }); await load() }
onMounted(load)
</script>
<style scoped>
.merge-group { margin-bottom: 8px; }
.merge-group button { margin-left: 8px; }
</style>
