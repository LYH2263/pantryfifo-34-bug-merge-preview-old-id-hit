<template>
  <div>
    <h1>冰箱分层</h1>
    <p class="muted">竖列分层 · FEFO 消费走「消费」页 · 勾两条同品同到期日同隔离资格的批即可在此合并</p>
    <div class="fridge">
      <section v-for="L in layers" :key="L" class="shelf">
        <h3>{{ label[L] }}</h3>
        <label v-for="x in by(L)" :key="x.id" class="lot pick" :class="{ off: !(x.qty_remain > 0) }">
          <input type="checkbox" :value="x.id" v-model="picked" :disabled="!(x.qty_remain > 0)" />
          {{ x.name }} #{{ x.id }} ×{{ x.qty_remain }} · {{ x.expiry }}
          <span v-if="x.data_quality === 'dirty'" class="dq">脏</span>
        </label>
      </section>
    </div>

    <div style="margin-top:12px">
      <button type="button" :disabled="picked.length < 2" @click="doPreview">预览合并（{{ picked.length }} 条）</button>
      <button type="button" v-if="preview" @click="doConfirm">确认合并</button>
      <button type="button" v-if="picked.length" @click="clearPick" class="ghost">清空选择</button>
    </div>

    <div v-if="preview" class="shelf" style="margin-top:12px">
      <h3>预览（库存未改动）</h3>
      <p>留存 lot #{{ preview.plan.survivor_id }}，吞掉 lot #{{ preview.plan.absorbed_ids.join('、#') }}；
         合并后余量 <b>{{ preview.plan.total_qty }}</b>。</p>
      <p>当前全层条数 <b>{{ preview.shelf_before.lot_count }}</b>、余量合计 <b>{{ preview.shelf_before.total_qty }}</b> —— 预览时均不变。</p>
    </div>
    <div v-if="result" class="shelf" style="margin-top:12px">
      <h3>合并结果</h3>
      <p v-if="result.idempotent" class="muted">该组已合并过：重复确认未再加量。</p>
      <p>确认后全层条数 {{ result.shelf_before.lot_count }} → <b>{{ result.shelf_after.lot_count }}</b>，
         余量合计 {{ result.shelf_before.total_qty }} = <b>{{ result.shelf_after.total_qty }}</b>（不变）。</p>
      <p>按临期消费此后只能打到 lot #{{ result.survivor_id }}（×{{ result.total_qty }}）。</p>
    </div>
    <p v-if="error" class="muted" style="color:var(--alert)">失败：{{ error }}（全部回到合并前）</p>

    <button style="margin-top:12px" @click="sweep">过期下架</button>
  </div>
</template>
<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
const rows = ref([])
const layers = ['upper','mid','lower']
const label = { upper: '上层', mid: '中层', lower: '下层' }
const picked = ref([])
const preview = ref(null)
const result = ref(null)
const error = ref('')
function by(L) { return rows.value.filter(r => r.layer === L) }
async function load() {
  rows.value = await api('/fridge')
  // 收走的行从选择/预览里剔除，避免总表留着已吞批号。
  const live = new Set(rows.value.map(r => r.id))
  picked.value = picked.value.filter(id => live.has(id))
  if (preview.value && preview.value.plan.absorbed_ids.some(id => !live.has(id))) preview.value = null
}
function clearPick() { picked.value = []; preview.value = null; result.value = null; error.value = '' }
async function doPreview() {
  result.value = null; error.value = ''
  try {
    preview.value = await api('/merge/preview', { method: 'POST', body: JSON.stringify({ lot_ids: picked.value }) })
  } catch (e) { preview.value = null; error.value = e.message }
}
async function doConfirm() {
  error.value = ''
  try {
    result.value = await api('/merge/confirm', { method: 'POST', body: JSON.stringify({ lot_ids: picked.value }) })
    preview.value = null
    clearPick()
    await load()
    window.dispatchEvent(new Event('pantry:changed'))  // 分层页与紧急条一并收口
  } catch (e) {
    result.value = null
    preview.value = null
    error.value = e.message
    picked.value = []
    await load()                                       // 失败也回到合并前视图
    window.dispatchEvent(new Event('pantry:changed'))
  }
}
async function sweep() { await api('/expire-sweep', { method: 'POST', body: '{}' }); await load(); window.dispatchEvent(new Event('pantry:changed')) }
onMounted(() => { load(); window.addEventListener('pantry:changed', load) })
onBeforeUnmount(() => window.removeEventListener('pantry:changed', load))
</script>
<style scoped>
.pick input { width: auto; margin: 0 6px 0 0; }
.pick.off { opacity: .55; }
.dq { margin-left: 4px; padding: 0 5px; border-radius: 6px; background: #ffe8d8; color: var(--alert); font-size: 12px; }
.ghost { background: #fff; color: var(--teal); border: 1px solid var(--line); margin-left: 8px; }
</style>
