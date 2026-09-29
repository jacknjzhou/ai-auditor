# 模板 Schema 约定

批量提取模板统一使用 YAML 定义，存放于本目录，每个模板一个文件。

## 字段类型

| type | 说明 | 台账单元格 | 兜底校验 |
|---|---|---|---|
| `string` | 普通文本 | 文本 | `rule` 正则 |
| `money` | 金额，统一为**元**（两位小数） | 数值（千分位格式） | `sum_check`（与明细求和核对） |
| `date` | 日期，统一归一化为 `YYYY-MM-DD` | 日期单元格 | 合法日期检查 |
| `number` | 数量/年限等数值 | 数值 | — |
| `enum` | 枚举，取值限于 `enum_values` | 文本 | 枚举校验，越界值标黄 |
| `array` | 一对多明细/列表 | 主表汇总显示条数，明细展开到溯源 sheet 或独立明细 sheet | — |

## 通用字段属性

- `key`：列英文键（台账/数据库字段名）
- `label`：中文表头
- `required`：必填缺失 → 整行标红进复核
- `rule`：正则兜底校验（规则能查的不靠模型）
- `sum_check: true`：与 array 明细的指定金额列求和核对
- `hint`：给 LLM 的提取提示（写清常见形态、边界情况）
- `enum_values`：枚举可选值

## 匹配规则（match）

- `keywords`：文档内容关键词，命中个数计分
- `filename_patterns`：文件名通配（fnmatch），命中计 2 分
- 多模板命中取最高分；分数并列或低于 `min_score` → 进 `failed/` 待人工指定

## 选项（options）

- `confidence_threshold`：字段置信度低于该值 → 单元格标黄（默认 0.8）
- `cross_checks`：模板级交叉校验规则（如发票 金额×税率≈税额），校验失败标红

## 去重（dedup）

- `keys`：业务键字段（如 `invoice_number`），提取完成后跨批次撞库；重复 → 台账标记"🔁 重复"并进重复清单
- `compare_fields`：冲突判定字段——同业务键但这些字段不一致 → L3 内容冲突，整行标红 + urgent 推送
- 文件级重复（hash）由全局配置 `dedup.file_hash: skip|alert|reprocess` 控制，默认 `alert`（跳过提取 + 提示）
- 未配置 `dedup` 的模板仅做 L1 文件级检测，不做业务键撞库

## 台账输出

每个模板生成一份 `台账_<name>_<日期>.xlsx`：
1. **主表**：一行一文件，列 = fields + 固定 `文件名/状态/最低置信度`
2. **溯源 sheet**：`文件名 | 字段 | 提取值 | 原文片段 | 页码`
3. **明细 sheet**（仅含 array 字段的模板）：一对多展开
