import { useEffect, useState } from 'react'
import { listSkills, type SkillInfo } from '../settings/settingsApi'

/** 加载可用技能列表（项目级优先于用户级），供命令候选框展示。 */
export function useSkills(projectRoot: string | null): SkillInfo[] {
  const [skills, setSkills] = useState<SkillInfo[]>([])
  useEffect(() => {
    if (!projectRoot) {
      setSkills([])
      return
    }
    let alive = true
    void listSkills(projectRoot)
      .then((s) => {
        if (alive) setSkills(s)
      })
      .catch(() => {
        if (alive) setSkills([])
      })
    return () => {
      alive = false
    }
  }, [projectRoot])
  return skills
}
