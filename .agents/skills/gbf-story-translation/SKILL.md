---
name: gbf-story-translation
description: Use when translating and reviewing Japanese Granblue Fantasy story CSV files into Simplified Chinese in this AI-Translation project, including files supplied one at a time, character fate episodes, q/s chapters, event stories, terminology and character-voice consistency, mistranslation fixes, UI line breaks, metadata trans fields, and final cross-file review. 默认用于《碧蓝幻想》日文剧情 CSV 的简体中文翻译并审校及连续供稿处理。
---

# GBF 剧情翻译

## 任务边界

* 默认单遍翻译并审校所有有效正文：空白 `trans` 直接翻译，已有译文直接审校并作出最终判断，不再安排第二次全面通读。
* 有效正文包括可见文本、有意义纯标点台词和标签内可见文本；默认排除空 `text`、纯控制和无可见内容的纯标签记录。
* 用户指定只补空白、只审校、文件或 ID 范围、保持不变或全部重译时，优先遵循用户要求。
* 只通过本 Skill 的 JSON 与脚本写入目标 CSV 的 `trans`。允许填写 `译者` 的 `trans`，但不得编造贡献信息。
* 不运行游戏或构建，不直接编辑 CSV，也不临时编写替换脚本；运行 Python 时使用 `-B`，不得留下 `__pycache__`。

## 必读规则

* 任务开始时完整读取 [工作流](references/workflow.md)。
* 连续任务的第一个文件开始前，完整读取 [翻译与审校规则](references/translation-rules.md)。
* 上下文经过压缩、摘要化或重建后，在继续任务前重新完整读取“翻译与审校规则”。
* 进入最终复查前重新读取“工作流”的“验证”和“完成条件”。

## 会话任务

* 每个新会话任务开始时按当前本地时间生成一次 `YYYYMMDD-HHMMSS` 格式的 `task_id`，同一会话内的连续供稿始终沿用该值，不维护 current-task 或任务复用状态。
* 任务文件统一位于 `.codex-tmp/gbf-translation/<task_id>/`；脚本只接收 `--task-id` 并按固定目录约定定位更新 JSON、state、报告和原始副本。
* 最终完成时只清理当前 `task_id` 的目录，不删除整个 `.codex-tmp` 或其他任务目录。

## 每文件入口检查

处理每个文件前，先完整读取非空的 `.codex-tmp/gbf-translation/<task_id>/terms.md`，确认顶部的固定校准提醒并取得当前最小前向约束，不得先读取正文。

## 剧情顺序与分批

角色剧情：文件名含 `fate` 的文件视为第一篇；其余按数字 `q`、再按数字 `s` 排序。按该顺序维持术语、人物塑造、关系和指代一致性。

活动剧情：以 `scene_evt` 开头的 CSV 属于活动剧情。类似 `scene_evt260529_cp0_q1_s10.csv` 的文件视为序章，并入第一节；类似 `scene_evt260529_cp0_q2_s10.csv` 的文件视为终章，并入最后一节。示例活动 ID 只是占位。叙事顺序为序章、其余剧情、终章；其余文件按数字 `cp`、`q`、`s` 排序。

用户依次提供文件时不读取尚未提供的文件；顺序异常时报告风险，但按用户实际提供范围处理。一次提供大型活动目录时，先只查看文件名、数量和大小：`scene_evt*.csv` 超过 8 个或约 150 KB 时按 `cp` 分批；单个 `cp` 超过 6 个文件或约 90 KB 时再按数字 `q` 分批，批内按数字 `s`。不将 `cp0` 单独成批，序章并入第一批，终章并入最后一批；当前批次未完成时不开始下一批。

## 执行流程

1. 新会话任务生成并记录 `task_id`；连续供稿沿用已有值，再按剧情顺序确定当前范围。
2. 执行每文件入口检查，然后用 [read_translation_csv.py](scripts/read_translation_csv.py) 一次读取当前文件。
3. 按顺序单遍翻译并审校，必要时定点补读上下文；同步整理会跨文件漂移的术语和角色口吻。
4. 生成 `.codex-tmp/gbf-translation/<task_id>/updates.json`，运行 `python -B <Skill目录>/scripts/apply_translation_updates.py --task-id <task_id>`。
5. 人工处理脚本警告，更新术语和 backlog。当前文件完成后等待下一文件，不提前清理任务状态。
6. 用户明确表示全部文件已提供后，处理 backlog，运行 task 最终验证，报告结果并只清理当前任务目录。
