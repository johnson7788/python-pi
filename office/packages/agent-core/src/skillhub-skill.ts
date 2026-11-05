import type { AgentSkill } from './skill'
import type { AgentToolDef } from './types'

/**
 * Bridge to the user's installed extension skills (Anthropic Agent Skills
 * format). Progressive disclosure with NO code execution — the agent only ever
 * reads text:
 *   L0  every enabled skill's name+description → buildContext (cheap manifest)
 *   L1  load_skill(name)            → the skill's SKILL.md body
 *   L2  read_skill_file(name, path) → a reference file's text
 * The host injects `api` (HTTP calls to the backend /skills endpoints), keeping
 * agent-core free of any app's web-adapter.
 */
export interface SkillhubApi {
  /** enabled skills only */
  list(): Promise<{ name: string; description: string }[]>
  /** SKILL.md body for a skill by name */
  loadBody(name: string): Promise<string>
  /** a reference file's text, by skill name + in-pack path */
  readFile(name: string, path: string): Promise<string>
}

const REFRESH_MS = 15_000

const tools: AgentToolDef[] = [
  {
    name: 'load_skill',
    description:
      '读取某个扩展技能的完整操作指令（SKILL.md 正文）。当「可用扩展技能」清单里有与当前任务相关的技能时，先调用它拿到详细步骤再执行。',
    inputSchema: {
      type: 'object',
      properties: { name: { type: 'string', description: '技能名称（取自可用扩展技能清单）' } },
      required: ['name'],
    },
  },
  {
    name: 'read_skill_file',
    description:
      '读取扩展技能包内的参考文件（references/ 下的文本），仅在 SKILL.md 指示需要某参考资料时调用。',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: '技能名称' },
        path: { type: 'string', description: '包内相对路径，如 references/style.md' },
      },
      required: ['name', 'path'],
    },
  },
]

export function createSkillhubSkill(api: SkillhubApi): AgentSkill {
  let manifest: { name: string; description: string }[] = []
  let lastFetch = 0

  const refresh = () => {
    lastFetch = Date.now()
    return api
      .list()
      .then((m) => {
        manifest = m
      })
      .catch(() => {
        /* offline / not logged in: no skills, silent */
      })
  }
  void refresh() // warm the manifest at construction (before the first turn)

  return {
    id: 'skillhub',
    systemPrompt:
      '你可以使用用户安装的「扩展技能」。相关技能会列在上下文的「可用扩展技能」中。当某技能与当前任务相关时，先用 load_skill 读取其完整指令并按步骤执行；需要参考资料时用 read_skill_file。没有相关技能就忽略。',
    tools,
    buildContext() {
      if (Date.now() - lastFetch > REFRESH_MS) void refresh() // keep it fresh for later turns
      if (!manifest.length) return ''
      const lines = manifest.map((s) => `- ${s.name}: ${s.description}`).join('\n')
      return `可用扩展技能（相关时先用 load_skill 读取正文）:\n${lines}`
    },
    async executeTool(call) {
      const name = String(call.input.name ?? '').trim()
      if (call.name === 'load_skill') {
        try {
          return { output: await api.loadBody(name), summary: `技能 ${name}` }
        } catch {
          return { output: `未找到扩展技能: ${name}`, isError: true, summary: `技能 ${name}` }
        }
      }
      if (call.name === 'read_skill_file') {
        const path = String(call.input.path ?? '').trim()
        try {
          return { output: await api.readFile(name, path), summary: `${name}/${path}` }
        } catch {
          return { output: `未找到文件: ${path}`, isError: true, summary: `${name}/${path}` }
        }
      }
      return { output: `Unknown tool: ${call.name}`, isError: true, summary: call.name }
    },
  }
}
