"""Skill 体系（M5.1）：按需加载的可复用提示词 + 脚本 + 参考文档包。"""

from agent.skills.loader import SkillLoader
from agent.skills.spec import SkillSpec

__all__ = ["SkillSpec", "SkillLoader"]
