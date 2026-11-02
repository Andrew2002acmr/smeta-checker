<template>
  <div>
    <WorkspaceHeader active="knowledge" />
    <main
      id="main-content"
      tabindex="-1"
      class="shell knowledge-page"
      data-ui="normative-workbench"
    >
      <NormativeWorkbench />
    </main>
  </div>
</template>

<style scoped>
.knowledge-page {
  padding-bottom: 40px;
}
</style>
