import type { AgentDefinition } from './types/agent-definition'

const definition: AgentDefinition = {
  id: 'context-window-manager',
  version: '0.1.0',
  displayName: 'Context Window Manager',
  model: 'openai/gpt-5-mini',
  reasoningOptions: { effort: 'minimal' },
  mcpServers: {
    'context-window-manager': {
      type: 'stdio',
      command: 'cwm-mcp',
      args: [],
    },
  },
  instructionsPrompt: `At the start of every user turn, compare the request with the saved project checklist. Call context-window-manager/sync_requirements with new, changed, or cancelled requirements; preserve unrelated unfinished tasks. Call recover_context at session start or after compaction, and search_context for relevant history. Keep task status current and only complete tasks with concrete verification evidence. The automatic text capture is only a candidate checklist, so interpret the user's intent before syncing. Save a checkpoint before ending substantial work.`,
}

export default definition
