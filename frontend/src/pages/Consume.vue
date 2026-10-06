<template>
  <div>
    <h1>按临期消费</h1>
    <select v-model.number="item_id"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" />
    <button @click="go">FEFO 扣减</button>
    <div v-if="result" class="shelf" style="margin-top:10px">
      <h3>扣分明细（仅引用当前在架 lot）</h3>
      <p v-for="d in result.deductions" :key="d.lot_id">
        lot #{{ d.lot_id }} · 到期 {{ d.expiry || '—' }} · 扣 <b>{{ d.take }}</b>
      </p>
      <p v-if="!result.deductions.length" class="muted">无扣减。</p>
    </div>
    <p v-if="error" class="muted" style="color:var(--alert)">失败：{{ error }}（余量未改动）</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const result = ref(null)
const error = ref('')
onMounted(async () => { items.value = await api('/items'); if (items.value[0]) item_id.value = items.value[0].id })
async function go() {
  result.value = null; error.value = ''
  try {
    result.value = await api('/consume', { method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value }) })
    window.dispatchEvent(new Event('pantry:changed'))
  } catch (e) { error.value = e.message }
}
</script>
