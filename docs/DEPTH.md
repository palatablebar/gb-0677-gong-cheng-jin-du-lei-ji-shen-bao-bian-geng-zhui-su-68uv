# 深度登记 工程进度累计申报与变更追溯

规模：中型。使用者：总包项目计量审核员。核心规则：累计申报必须减前期已审金额，变更按审批生效版本覆盖；负进度需关联冲回证据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 申报采信 | 查看并运行申报采信 | `/api/claims` | `app/modules/m1_claims/rules.py` | `tests/test_m1.py` |
| M2 清单映射 | 查看并运行清单映射 | `/api/billitems` | `app/modules/m2_billitems/rules.py` | `tests/test_m2.py` |
| M3 变更定版 | 查看并运行变更定版 | `/api/changes` | `app/modules/m3_changes/rules.py` | `tests/test_m3.py` |
| M4 累计还原 | 查看并运行累计还原 | `/api/increment` | `app/modules/m4_increment/rules.py` | `tests/test_m4.py` |
| M5 扣回核算 | 查看并运行扣回核算 | `/api/deductions` | `app/modules/m5_deductions/rules.py` | `tests/test_m5.py` |
| M6 进度对账 | 查看并运行进度对账 | `/api/valuation` | `app/modules/m6_valuation/rules.py` | `tests/test_m6.py` |
| M7 签证归档 | 查看并运行签证归档 | `/api/certificates` | `app/modules/m7_certificates/rules.py` | `tests/test_m7.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码 | 默认解码失败 | 回退GB18030 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_legacy_encoding_real_sample` |
| TRAP-02 | BOM | 首列名多字符 | utf-8-sig | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_bom_real_sample` |
| TRAP-03 | 带引号换行 | 拆成两条 | csv逻辑行 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_quoted_newline_is_one_record` |
| TRAP-05 | 列数不匹配 | 静默错列 | 隔离原行 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_bad_columns_real_sample` |
| TRAP-08 | 前导零ID | 编码变形 | 全程字符串 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_identifier_preserved_real_sample` |
| TRAP-14 | 重复键冲突 | 后写覆盖 | 规范行序裁决并隔离冲突行（与投递行序无关） | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_duplicate_conflict_real_sample` |
| TRAP-18 | 空态三态 | 一律成功 | 分别错误码 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_three_empty_states` |
| TRAP-26 | 旧版本提交 | 覆盖新结果 | 版本条件更新 | `app/store.py` | `tests/test_e2e.py::test_stale_review_409` |
| TRAP-06 | 全角累计量 | 转换失败 | Decimal归一 | `app/values.py` | `tests/test_m2.py::test_fullwidth_quantity` |
| TRAP-07 | 累计量空值 | 按零冲回 | 隔离缺失 | `app/values.py` | `tests/test_m2.py::test_real_null_not_zero` |
| TRAP-10 | 分币舍入 | 浮点误差 | 分项量化后净额守恒 | `app/modules/m5_deductions/rules.py` | `tests/test_m5.py::test_fraction_rounds_to_cent` |
| TRAP-23 | 变更生效边界 | 旧价覆盖 | 按周期选择已批版本 | `app/modules/m3_changes/rules.py` | `tests/test_m3.py::test_effective_boundary` |
| TRAP-27 | 换序重投 | 文件摘要不同即新增批次与签证 | 内容摘要与行序无关，同一封存编号 | `app/identity.py`、`app/modules/m1_claims/rules.py` | `tests/test_seal.py::test_reordered_delivery_shares_batch_and_seal_number` |
| TRAP-28 | 封存失败或中断 | 覆盖旧版证据、重试跳号或重建 | pending+定稿事务，恢复后续用已有版本 | `app/store.py` | `tests/test_seal.py::test_failed_seal_keeps_old_evidence_and_resumes` |
| TRAP-29 | 原始投递取证 | 合并后找不到后一次投递 | 投递表只追加并导出对应关系 | `app/store.py`、`app/archive.py` | `tests/test_seal.py::test_seal_map_lists_every_delivery_under_one_number` |

## 对账规则

输入原始逻辑行=最终采纳来源行+隔离来源行+重复行+空白行+分段表头。派生记录用_sources保存完整来源，因此分组不丢行。业务预期合计与上游申报合计之差等于逐行差异之和。结果按内容摘要（不含行号、与行序无关）、规则版本及参数产生唯一批次，事务写入；同内容重跑只复用已有批次并追加原始投递。

## 月末封存

同一组申报内容只有一个 `MS-<YYYYMM>-<摘要>` 编号。批次、原始投递、封存版本分表存放：投递按投递摘要幂等、只追加原件字节；封存版本按证据内容追加，旧版本永久可导出。换序重投只增加投递证据，不新增编号；复核变化在同一编号下产生新版本。失败或中断时 pending 快照被丢弃，已封存版本保持可读，重试从已有版本续号。

对应关系导出：`/api/seal-map.csv`，或按编号 `/api/seals/<编号>/deliveries`；原件下载 `/api/deliveries/<投递编号>/raw`。

## 已知不覆盖

4MB以上文件需预先按业务键分批；不模拟生产支付与外部回调。人工复核只变更批次状态，不改写原始记录。
