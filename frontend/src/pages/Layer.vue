<template>
  <div>
    <h1>{{ props.layer }} 层</h1>
    <span v-for="x in rows" :key="x.id" class="lot">{{ x.name }} #{{ x.id }} ×{{ x.qty_remain }} · {{ x.expiry }}</span>
    <p v-if="!rows.length" class="muted">该层暂无在架批次。</p>
  </div>
</template>
<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
const props = defineProps({ layer: String })
const rows = ref([])
async function load() { rows.value = await api('/fridge?layer=' + props.layer) }
function onChange() { load() }
watch(() => props.layer, load)
onMounted(() => { load(); window.addEventListener('pantry:changed', onChange) })
onBeforeUnmount(() => window.removeEventListener('pantry:changed', onChange))
</script>
