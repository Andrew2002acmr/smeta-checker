<template>
  <div class="knowledge-layout">
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
.knowledge-layout {
  display: flex;
  height: 100dvh;
  min-height: 0;
  flex-direction: column;
}
.knowledge-page {
  width: 100%;
  flex: 1;
  min-height: 0;
  padding-bottom: 24px;
}
</style>
