# 到期预警 + 台账回写 — 细化设计

> 日期：2026-09-24 ｜ 状态：设计评审中  
> 关联：`2026-09-24-batch-extract-ledger-design.md`（总体）、`2026-09-24-accuracy-enhancement-design.md`（精确度）  
> 定位：B（预警监控）与 D（台账回写）两个发散方向的落地设计，共同前提是**提取结果入库**（P3 台账入库后生效）

## 0. 前置：台账入库（两项的共同地基）

Excel 台账之上增加数据库层（复用办公平台 PostgreSQL，或独立 SQLite 起步）：

```
extraction_records        # 提取主表
  id, file_hash(唯一索引), file_name, template_id, status,  # status 含 duplicate
  dup_type, dup_of_record_id,                                 # L1/L2/L3 重复标记
  extracted_at, reviewed_by
extraction_fields         # 字段表（每字段一行，含溯源）
  record_id, field_key, value, evidence_text, page_no, confidence, flags
alerts                    # 预警表（B 用）
  id, alert_type, record_id, field_key, due_date, level, status, pushed_at
  # alert_type 增枚举：duplicate_file(L1) / duplicate_bizkey(L2) / duplicate_conflict(L3)
writeback_jobs            # 回写任务表（D 用）
  id, record_id, target_module, biz_key, status, request_id, response_digest
```

台账 Excel 保留为导出视图，数据库成为唯一事实来源。

---

## 1. B：到期预警 + 异常监控

### 1.1 预警类型

| 类型         | 触发逻辑                                                      | 数据来源                                              |
| ---------- | --------------------------------------------------------- | ------------------------------------------------- |
| **到期预警**   | 日期字段距今天数 ≤ N，分级提醒（30/7/1 天）                               | 合同 `end_date`、质保期、报价 `valid_until`、证照年检日期         |
| **重复文档预警** | L1 文件 hash 重复 / L2 业务键重复（内容一致）/ L3 业务键重复且**内容冲突**（urgent） | 批次提取时实时检测（总体设计 §3.1），每日任务兜底复扫                     |
| **异常预警**   | 提取/校验阶段产生的 flags 非空                                       | 勾稽失败、税号格式错                                        |
| **定期简报**   | 每周定时汇总                                                    | 新增合同 X 份 / 报销 Y 元 / 待复核 Z 项 / 本周到期清单 / 本周重复文档 N 份 |

### 1.2 规则配置（延续模板 DNA，YAML 定义）

```yaml
alert_rules:
  - id: contract_expiry
    template: purchase_sales_contract_v1
    source_field: end_date
    type: due_date
    levels: [{days: 30, level: info}, {days: 7, level: warning}, {days: 1, level: urgent}]
    message: "合同 {contract_no}（{party_b}）将于 {due_date} 到期"
    escalate_to: 合同经办人
  - id: duplicate_bizkey                # 业务键重复（泛化：发票号/合同号/资产编号）
    scope: global                       # 跨批次撞库
    match: {template: vat_invoice_v1}
    dedup_keys: [invoice_number]        # 与模板 dedup.keys 一致
    conflict_fields: [amount, invoice_date, seller_tax_id]  # 不一致→升级 L3 urgent
    on_conflict_push: true
  - id: duplicate_file                  # 文件级重复（hash）
    scope: global
    type: duplicate_file
    action: alert                       # skip | alert | reprocess
```

- 规则引用模板字段 key，模板更新规则自动跟随
- 业务人员只需在 YAML 里加一段，无需改代码

### 1.3 推送与去重

- **渠道**：钉钉/企微群机器人 webhook（起步）→ 后续接办公平台站内信
- **去重**：同一 `(alert_type, record_id)` 一档级别只推一次，级别升级（7天→1天）才重推
- **静默**：节假日顺延；已回写平台的到期合同自动关闭预警

### 1.4 实现

- **实时检测**（随批次，不等待每日任务）：L1 在扫描阶段、L2/L3 在字段提取完成后即时撞库；L3 批次结束立即推送
- **兜底复扫**：`office_alert check` 每日任务复扫 L2/L3 与到期项，纯规则无 LLM 调用，成本为零

---

## 2. D：台账回写办公平台

### 2.1 核心原则

1. **复核是闸门**：只有复核通过（人工确认或高置信自动通过）的记录才允许回写
2. **幂等回写**：按**业务键**（合同编号/发票号码/资产编号）upsert，而非文件 hash——同一份合同重扫不重复建卡
3. **全链审计**：每次回写的请求/响应落 `writeback_jobs`，可追溯"谁在何时同步了什么"

### 2.2 模板侧扩展：writeback 节点

```yaml
# 追加到 purchase_sales_contract.yaml
writeback:
  target_module: contracts            # 办公平台模块 
  biz_key: contract_no                # 幂等键
  create_api: POST /api/v1/contracts
  update_api: PUT  /api/v1/contracts/{id}
  field_mapping:                      # 模板字段 → 平台字段
    contract_no: contract_no
    party_a: party_a_name
    total_amount: amount
    sign_date: signed_at
    payment_schedule: payment_plan    # array → 平台子表
  lookup:                             # 回写前先用 biz_key 查平台是否已存在
    api: GET /api/v1/contracts?no={biz_key}
```

### 2.3 回写流程

```
复核通过 → 生成回写任务(writeback_jobs, status=pending)
        → 按 biz_key 查平台：不存在→create / 已存在→diff后update
        → 成功：record 标记 synced + 台账"同步状态"列更新
        → 失败：退避重试3次 → 仍失败标红，进人工处理清单
```

- **diff 更新**：平台已有记录时逐字段对比，仅更新有差异的字段，diff 记录进审计
- **人工确认可选**：`writeback.require_confirm: true` 的模板，回写前需在复核界面点确认

### 2.4 安全

- 平台 API 走专用 service token，仅授予目标模块的读写最小权限
- 回写请求附带 `source: office_extract` + record_id，平台侧可识别来源

---

## 3. 路线图更新

| 阶段     | 内容                                    | 依赖          |
| ------ | ------------------------------------- | ----------- |
| P1–P4  | 原有：CLI 跑通 → 多模板分流 → 校验体系 → 平台 Web     | —           |
| **P5** | **台账入库（extraction_records/fields 表）** | P3          |
| **P6** | **到期预警 + 异常推送 + 周报**（纯规则，零 LLM 成本）    | P5          |
| **P7** | **台账回写平台**（先合同模块试点，扩展资产/报销）           | P5 + 平台 API |

P6 不依赖平台、见效最快；P7 依赖办公平台各模块 API 就绪程度，可与 P4 并行推进。
