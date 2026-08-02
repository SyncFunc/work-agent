---
name: skillcreator
description: 创建或编辑一个新的技能（Skill）——生成规范的 SKILL.md 目录，含 frontmatter 与正文模板变量
when_to_use: 当用户要求"新建一个技能/skill"、"写一个 SKILL.md"、"把某段流程固化成技能"时触发
arguments: [name]
argument_hint: <skill-name>
user_invocable: true
---

# Skill Creator：创建规范的技能

本技能指导你（Agent）为一个新技能生成完整的 `SKILL.md` 目录结构，遵循 work-agent 的技能规范。

## 步骤

1. **确定技能名**：由 `$ARGUMENTS` 提供（如 `$ARGUMENTS` 为空则先向用户确认）。技能名用小写短横线命名，如 `code-review`。

2. **选择存放位置**（按作用域）：
   - 项目级：`<project>/.agent/skills/<name>/SKILL.md`（仅当前项目）
   - 用户级：`~/.agent/skills/<name>/SKILL.md`（所有项目可用）
   - 内建：`agent/skills/builtin/<name>/SKILL.md`（随代码分发，慎用）
   默认写入项目级；如需跨项目用用户级。

3. **生成 SKILL.md**：frontmatter 至少包含以下字段：

   ```yaml
   ---
   name: <skill-name>
   description: <一句话，说明该技能做什么，供模型决定何时触发>
   when_to_use: <补充触发场景，可选>
   arguments: [<命名位置参数>]     # 可选，逗号分隔
   argument_hint: <参数提示，如 <file>>
   allowed_tools: [<允许的工具>]    # 可选；默认继承父会话全部
   disallowed_tools: [<禁用的工具>] # 可选
   disable_model_invocation: false  # true=仅 /name 手动调用
   user_invocable: true             # 是否用户可调用
   ---
   ```

   正文用 Markdown，遵循结构：

   - `# 技能名`
   - `## 步骤`：明确列出执行步骤，写清楚用什么工具、产出什么
   - `## 参数`：说明 `$ARGUMENTS` 及每个命名参数的用法

4. **模板变量规范**：
   - `$ARGUMENTS` — 全部位置参数拼接的字符串
   - `$1` `$2` … 或 `$ARGUMENTS[N]` — 第 N 个参数
   - `$name` — 命名参数（若有）
   - `${SKILL_DIR}` — 技能所在目录路径
   - 反引号包裹的字面 `$` 用 `\$` 转义

5. **验证**：写完用 `read` 重新读一遍 SKILL.md，确认 frontmatter 语法正确（YAML）、正文步骤无歧义。

## 参数

- `$ARGUMENTS` — 新技能的名称；未提供则询问用户。
