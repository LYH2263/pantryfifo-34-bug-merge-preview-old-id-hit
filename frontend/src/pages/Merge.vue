<template>
  <div>
    <h1>同品同到期日合并</h1>
    <p class="muted">仅同品 · 同到期日 · 同隔离资格（clean/dirty 一致）的在架批可合并。预览不改动库存；确认后只留一条，余量为合并前之和。</p>

    <section v-for="g in groups" :key="g.key" class="shelf merge-group">
      <h3>{{ g.name }} · {{ g.unit }} · 到期 {{ g.expiry }} · {{ g.quality === 'dirty' ? '脏批' : 'clean' }}</h3>
      <label v-for="x in g.lots" :key="x.id" class="lot pick">
        <input type="checkbox" :value="x.id" v-model="picked" />
        lot #{{ x.id }} ×{{ x.qty_remain }}
      </label>
      <div><button type="button" @click="pickGroup(g)">选择本组</button></div>
    </section>
    <p v-if="!groups.length" class="muted">当前没有可合并的批次分组。</p>

    <div style="margin-top:12px">
      <button type="button" :disabled="picked.length < 2" @click="doPreview">预览合并（{{ picked.length }} 条）</button>
      <button type="button" v-if="preview" @click="doConfirm">确认合并</button>
    </div>

    <div v-if="preview" class="shelf" style="margin-top:12px">
      <h3>预览（库存未改动）</h3>
      <p>留存 lot #{{ preview.plan.survivor_id }}，吞掉 lot #{{ preview.plan.absorbed_ids.join('、#') }}；
         合并后余量 <b>{{ preview.plan.total_qty }}</b>。</p>
      <p>全层条数 <b>{{ preview.shelf_before.lot_count }}</b>、余量合计 <b>{{ preview.shelf_before.total_qty }}</b>
         —— 预览时均不变，确认后条数 -{{ preview.plan.absorbed_ids.length }}。</p>
      <p>确认后按临期消费打到留下的 lot #{{ preview.hit_ids[0] }}。</p>
    </div>
    <div v-if="result" class="shelf" style="margin-top:12px">
      <h3>合并结果</h3>
      <p v-if="result.idempotent" class="muted">该组已合并过：重复确认未再加量。</p>
      <p>确认后全层条数 {{ result.shelf_before.lot_count }} → <b>{{ result.shelf_after.lot_count }}</b>，
         余量合计 {{ result.shelf_before.total_qty }} = <b>{{ result.shelf_after.total_qty }}</b>（不变）。</p>
      <p>按临期消费此后只能打到 lot #{{ result.survivor_id }}（×{{ result.total_qty }}）。</p>
    </div>
    <p v-if="error" class="muted" style="color:var(--alert)">失败：{{ error }}（全部回到合并前）</p>
  </div>
</template>
<script setup>
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
const route = useRoute()
const rows = ref([])
const picked = ref([])
const preview = ref(null)
const result = ref(null)
const error = ref('')

const groups = computed(() => {
  const m = new Map()
  for (const r of rows.value) {
    if (!(r.qty_remain > 0)) continue
    const key = [r.item_id, r.expiry, r.data_quality || 'clean'].join('|')
    if (!m.has(key)) m.set(key, { key, name: r.name, unit: r.unit, expiry: r.expiry,
                                  quality: r.data_quality || 'clean', lots: [] })
    m.get(key).lots.push(r)
  }
  return [...m.values()].filter(g => g.lots.length >= 2)
    .map(g => ({ ...g, lots: g.lots.sort((a, b) => a.id - b.id) }))
})

async function load() {
  rows.value = await api('/fridge')
  const valid = new Set(rows.value.map(r => r.id))
  // 已被吞掉/下架的 id 不再保留在选中里，避免拿旧身份再发起。
  picked.value = picked.value.filter(id => valid.has(id))
  applyQueryPicks(valid)
}
function applyQueryPicks(valid) {
  // 从总表带参发起：/merge?lots=1,2 —— 只预选仍在架的批。
  const q = route.query.lots
  if (!q) return
  const ids = String(q).split(',').map(Number).filter(Boolean).filter(id => valid.has(id))
  if (ids.length >= 2) {
    picked.value = ids
    preview.value = null; result.value = null; error.value = ''
  }
}
function pickGroup(g) {
  picked.value = g.lots.map(l => l.id)
  preview.value = null; result.value = null; error.value = ''
}
async function doPreview() {
  result.value = null; error.value = ''
  try {
    preview.value = await api('/merge/preview', { method: 'POST', body: JSON.stringify({ lot_ids: picked.value }) })
  } catch (e) {
    preview.value = null; error.value = e.message
  }
}
async function doConfirm() {
  error.value = ''
  try {
    result.value = await api('/merge/confirm', { method: 'POST', body: JSON.stringify({ lot_ids: picked.value }) })
    preview.value = null
    await load()
  } catch (e) {
    result.value = null; error.value = e.message
    await load()
  }
}
load()
</script>
<style scoped>
.merge-group { margin-bottom: 12px; }
.pick { cursor: pointer; }
.pick input { width: auto; margin: 0 6px 0 0; }
button { margin: 4px 8px 0 0; }
button:disabled { opacity: 0.5; cursor: default; }
</style>
