import { describe, expect, it } from 'vitest'
import { createSkillhubSkill, type SkillhubApi } from '../src/skillhub-skill'

function fakeApi(over: Partial<SkillhubApi> = {}): SkillhubApi {
  return {
    list: async () => [{ name: 'apa', description: 'APA references' }],
    loadBody: async (name) => `body of ${name}`,
    readFile: async (name, path) => `file ${path} of ${name}`,
    ...over,
  }
}

const tick = () => new Promise((r) => setTimeout(r, 0))

describe('skillhub skill', () => {
  it('advertises enabled skills in buildContext after the manifest loads', async () => {
    const skill = createSkillhubSkill(fakeApi())
    await tick() // let the construction-time refresh resolve
    const ctx = skill.buildContext?.() ?? ''
    expect(ctx).toContain('apa')
    expect(ctx).toContain('APA references')
  })

  it('load_skill returns the SKILL.md body', async () => {
    const skill = createSkillhubSkill(fakeApi())
    const r = await skill.executeTool({ id: '1', name: 'load_skill', input: { name: 'apa' } })
    expect(r.output).toBe('body of apa')
    expect(r.isError).toBeFalsy()
  })

  it('read_skill_file returns a reference file', async () => {
    const skill = createSkillhubSkill(fakeApi())
    const r = await skill.executeTool({
      id: '2',
      name: 'read_skill_file',
      input: { name: 'apa', path: 'references/x.md' },
    })
    expect(r.output).toBe('file references/x.md of apa')
  })

  it('errors gracefully when a skill is missing', async () => {
    const skill = createSkillhubSkill(
      fakeApi({ loadBody: async () => { throw new Error('nope') } }),
    )
    const r = await skill.executeTool({ id: '3', name: 'load_skill', input: { name: 'ghost' } })
    expect(r.isError).toBe(true)
    expect(r.output).toContain('ghost')
  })

  it('empty manifest → no context, no noise', async () => {
    const skill = createSkillhubSkill(fakeApi({ list: async () => [] }))
    await tick()
    expect(skill.buildContext?.()).toBe('')
  })
})
