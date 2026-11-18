---
name: 技能编写
description: 把一套重复做法写成 Skill（SKILL.md）：名字、何时用、步骤、自检、输出格式
when_to_use: 同一类任务做过三次以上；或用户说"把这个流程记下来/做成技能"
allowed-tools: read_file, write_file, edit_file, list_files, glob
---

# 写一个 Skill

## 放哪

- 项目级：`.blank/skills/<name>/SKILL.md`（跟仓库走，团队共享）
- 个人级：`~/.blank/skills/<name>/SKILL.md`（只对自己生效）
- 目录名就是调用名，用小写与连字符：`code-review`、`db-migrate`。

## frontmatter（只写用得上的）

```yaml
---
name: 显示名
description: 一句话说清"做什么"，会被放进系统提示的清单，决定模型能不能匹配上
when_to_use: 什么时候该用（写触发场景与关键词，不要写"需要时使用"）
allowed-tools: read_file, search_code, run_shell   # 不写=不限制
paths: src/**/*.tsx                                 # 可选：只有碰到匹配文件才出现
---
```

`description` 与 `when_to_use` 是**唯一会被每轮都读到的部分**，必须写清"何时命中"。
其余正文只在真正调用时才加载，可以写细。

## 正文结构

1. **步骤**：编号，每步一个动作，能照着做。
2. **判据**：什么算完成、什么算失败。
3. **输出格式**：给出模板，模型照着填。
4. **禁止**：这类任务里最容易犯的错（比"注意事项"有效十倍）。

## 自检

- [ ] 换个不懂背景的人，只读这份能不能做对？
- [ ] 有没有写"看情况""酌情处理"这种没法执行的词？
- [ ] 步骤里有没有可以验证的产出（命令、文件、检查项）？
- [ ] description 能不能让人一眼判断"这事该不该用它"？

## 反例

写"要仔细、要严谨、要考虑周全"等于没写——正确做法是给出**可检查的清单**。
