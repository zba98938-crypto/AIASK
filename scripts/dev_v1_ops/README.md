# DEV-V1 运维脚本归档

DEV-V1 落地、灰度、产出密度提升过程中产生的辅助脚本。

> **平时运维请用根目录的 [`run_dev_v1_ops.py`](../../run_dev_v1_ops.py) 一站式脚本**,
> 详见 [`docs/ops/DEV-V1-运维手册.md`](../../docs/ops/DEV-V1-运维手册.md)。
>
> 本目录下的脚本是历史调研/验证留痕,作为审计参考。

---

## 目录结构

```
scripts/dev_v1_ops/
├── README.md                    # 本文件
├── verify/                      # 端到端验证脚本(无副作用)
├── query/                       # DB 查询/探针(无副作用)
├── gray/                        # 灰度运行器(写 DB)
└── archived/                    # 历史/归档(已完成使命)
```

---

## verify/ — 端到端验证脚本

不修改 DB,纯逻辑/接口验证。

| 脚本 | 作用 | 验证维度 |
|---|---|---|
| `_verify_v5_pr1_isolated.py` | V5-PR-1 隔离验证 | 直接调 `_inject_run_correction_metrics`,看 DSR/proxy 字段是否产出 |
| `_e2e_v5_pr1.py` | V5-PR-1 端到端验证 | 走完整 `run_submission_quality_gate` 流程,验证 V5-PR-1 注入字段最终落入返回结果 |
| `_e2e_p0_p1_force.py` | P0+P1 端到端 5 步验证 | toggle 解析 + P0 路径 ON/OFF 对照 + P1 intake/runner 路径 |

**这些脚本的功能已被 `run_dev_v1_ops.py verify` 子命令封装。**

### 用法示例

```powershell
# 跑 V5-PR-1 隔离验证(看 DSR 是否产出非空值)
python scripts/dev_v1_ops/verify/_verify_v5_pr1_isolated.py

# 跑 V5-PR-1 端到端(走 submission_gate 完整流程)
python scripts/dev_v1_ops/verify/_e2e_v5_pr1.py

# 跑 P0+P1 端到端 5 步(已被 run_dev_v1_ops.py verify 封装)
python scripts/dev_v1_ops/verify/_e2e_p0_p1_force.py
```

---

## query/ — DB 查询/探针

读 DB,不写。用于状态监测、基线分析。

| 脚本 | 作用 |
|---|---|
| `_query_incubation_status.py` | 全方位查孵化工厂状态(strategies/accounts/quality_reports/events/24h活动)|
| `_query_p0_baseline.py` | 历史 quality_reports P0 解封基线扫描(D 级 + Gate-B passed 的候选数) |
| `_check_v5_pr1_in_db.py` | 抽查最新 quality_reports 中 V5-PR-1 注入字段是否出现 |

**这些脚本的功能已被 `run_dev_v1_ops.py status` 子命令封装。**

### 用法示例

```powershell
# 查孵化工厂全景(已被 run_dev_v1_ops.py status 封装)
python scripts/dev_v1_ops/query/_query_incubation_status.py

# 跑历史 reports 的 P0 解封基线分析
python scripts/dev_v1_ops/query/_query_p0_baseline.py

# 抽查最近 5 条 reports 是否有 V5-PR-1 字段
python scripts/dev_v1_ops/query/_check_v5_pr1_in_db.py
```

---

## gray/ — 灰度运行器

会写 DB(跑 cycle 或 intake)。这些脚本是研发调研期间的调度器原型,
现已被 `run_dev_v1_ops.py` 各子命令(`cycle` / `intake` / `full`)替代。

| 脚本 | 作用 | 现在用什么替代 |
|---|---|---|
| `_gray_phase1_runner.py` | 灰度阶段 1 单 cycle 执行器(P0 toggle ON 验证) | `run_dev_v1_ops.py cycle` |
| `_gray_phase_full.py` | 多 cycle 灰度执行器(最多 5 cycle 红线监控) | `run_dev_v1_ops.py full`(单 cycle) + 循环 |
| `_test_density_cycle.py` | 产出密度提升测试 cycle | `run_dev_v1_ops.py cycle` |
| `_run_incubation_factory.py` | 直接调 IncubationFactoryRunner.run_once() | `run_dev_v1_ops.py intake` |

**优先用 `run_dev_v1_ops.py` 一站式脚本**,这些脚本只在需要特殊逻辑(比如多 cycle 红线监控)时再用。

### 用法示例

```powershell
# 跑灰度阶段 1(单 cycle + 红线检查)
python scripts/dev_v1_ops/gray/_gray_phase1_runner.py

# 跑多 cycle(最多 5 cycle,产 paper 账户后退出)
python scripts/dev_v1_ops/gray/_gray_phase_full.py

# 跑产出密度测试 cycle(同 _gray_phase1 但用新参数)
python scripts/dev_v1_ops/gray/_test_density_cycle.py

# 直接跑 IncubationFactoryRunner(已被 run_dev_v1_ops.py intake 封装)
python scripts/dev_v1_ops/gray/_run_incubation_factory.py
```

---

## archived/ — 历史归档

已完成使命的脚本和日志,保留作审计参考。

| 文件 | 用途 | 状态 |
|---|---|---|
| `_tmp_md_audit.py` | 早期 markdown 文档审计工具 | 已完成 |
| `_tmp_md_relocate.py` | 早期 markdown 文档重定位工具 | 已完成 |
| `_tmp_md_verify.py` | 早期 markdown 文档验证工具 | 已完成 |
| `_gray_phase1.log` | 灰度阶段 1 cycle stdout 日志(0 字节) | 历史日志 |
| `_gray_phase1.err.log` | 灰度阶段 1 cycle 完整日志(13.5 KB) | 历史日志 |

这些文件**不要删除** — 是 DEV-V1 灰度阶段的真实运行证据,
对应决策日志 `data/reports/sf_dev_v1_decision_log.md` 中的多个段落。

---

## 决策日志位置

所有 DEV-V1 操作的完整决策记录在:

`data/reports/sf_dev_v1_decision_log.md`

包含:
- 每次 toggle 启动的时间和原因
- 红线检查结果
- 每次 cycle/intake 的关键数据
- LLM 路径调研发现
- 产出密度提升前后对比

---

## 关联文档

- [`docs/ops/DEV-V1-运维手册.md`](../../docs/ops/DEV-V1-运维手册.md) — 平时运维参考(主文档)
- [`策略工厂到孵化工厂过渡架构方案-2026-05-26.md`](../../策略工厂到孵化工厂过渡架构方案-2026-05-26.md) — V4-network-p3 架构方案
- [`策略工厂到孵化工厂过渡-开发方案-2026-05-26.md`](../../策略工厂到孵化工厂过渡-开发方案-2026-05-26.md) — DEV-V1 + V5-PR-1 实施方案
- [`run_dev_v1_ops.py`](../../run_dev_v1_ops.py) — 一站式运维脚本
- [`data/reports/sf_dev_v1_decision_log.md`](../../data/reports/sf_dev_v1_decision_log.md) — 决策日志

---

**优先用 `run_dev_v1_ops.py`,本目录脚本仅作为审计/调试备选。**
