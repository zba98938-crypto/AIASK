# 孵化固定间隔模式（2026-11-04）

已完成代码与隔离回归，可用于持续四工厂运行。**没有启动生产进程，没有访问或修改生产数据库。**

## 启动方式

- 单独孵化：`/opt/miniconda3/envs/aiask/bin/python scripts/factories/run_incubation_factory.py --daemon --interval 900`
- 四工厂：在 `run_three_factories.py` 命令中加入 `--incubation-interval 900`。
- 未指定或设置 0 时保留原每日 `--run-time` 调度。

## 行为

1. 正间隔模式立即执行第一轮，然后在上一轮完成后等待指定秒数。
2. 循环直接 await 每轮，串行、不重入；整个 daemon 期间只启动一次并最终关闭 paper engines。
3. 返回型 partial/failed 和异常继续计失败并执行原有 error backoff，然后按配置间隔进入下一轮。
4. 取消时仍执行 stop 清理；负间隔在运行前明确拒绝。
5. supervisor 正间隔模式不启动额外一次性 incubation catch-up，避免启动时重复运行；每日模式的 catch-up 保留。

## 修改文件

- `packages/strategy-factory/src/strategy_factory/runtime/incubation.py`：`run_daemon(..., interval_sec=None)`。
- `scripts/factories/run_incubation_factory.py`：`--interval` 解析及透传。
- `scripts/factories/run_three_factories.py`：`--incubation-interval` 透传和 catch-up 去重。
- `packages/strategy-factory/tests/test_incubation_runtime_daemon.py`：立即启动、真实异步首轮阻塞期间不重入、按完成间隔等待、失败退避、取消清理、负数拒绝。
- 新增 `packages/strategy-factory/tests/test_incubation_interval_runner.py`：真实 CLI 解析、runtime 参数传递、supervisor spec/catch-up 合同。

## 验证

使用仓库根 conftest 的 hermetic 隔离；**52 passed in 6.10s**。

```sh
/opt/miniconda3/envs/aiask/bin/python -m pytest -q \
  packages/strategy-factory/tests/test_incubation_runtime_daemon.py \
  packages/strategy-factory/tests/test_incubation_interval_runner.py \
  packages/strategy-factory/tests/test_supervisor_diagnostic_audit_regressions.py \
  packages/akshare-mcp/tests/test_market_event_ingest_runner.py \
  packages/strategy-factory/tests/test_runtime_provider_boundary.py
```

日志：`/tmp/aiask_incubation_interval_tests_20261104.log`。

修改文件 `compileall` 与 `git diff --check` 均通过。没有改动策略候选准入、风控或生产调度参数值；实际运行周期由 root 的启动参数决定。
