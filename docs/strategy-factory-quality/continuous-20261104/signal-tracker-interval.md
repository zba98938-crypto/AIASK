# SignalTracker 持续间隔运行交接

日期：2026-11-04。仅修改源码及隔离测试，未启动生产进程、未访问生产数据库。

## 启动

```sh
/opt/miniconda3/envs/aiask/bin/python -u scripts/factories/run_signal_tracker.py --daemon --interval 1800
```

由 root 负责启动并确保只有一个所有者。正间隔立即第一轮，每轮完成后才等待间隔再进入下一轮，默认0仍按每日 `--run-time` 调度。

## 行为

- 执行业务期间每60秒更新心跳，等待及错误退避期间也每60秒更新。
- 心跳读取本进程刚写的业务结果并保留 `last_run_at`、`last_run_status`、`summary`、错误等字段；`status=running` 和 `phase=executing/waiting/retry_backoff` 表示进程活动，不冒充本轮业务完成。
- 异常仍按原300秒退避后重试一次，随后按间隔进入下轮；错误等待时保留 `last_run_status=failed` 和实际错误。
- 取消会取消并等待正在运行的业务 task，最终写 `status=stopped`，保留最近结果。KeyboardInterrupt也写停止状态。
- 负间隔、`--once`与正间隔组合在 runtime bootstrap 前拒绝。

## 修改文件

- `scripts/factories/run_signal_tracker.py`
- 新增 `packages/strategy-factory/tests/test_signal_tracker_interval_runner.py`

## 验证

统一解释器和仓库hermetic隔离，最终定向 **38 passed**。

```sh
/opt/miniconda3/envs/aiask/bin/python -m pytest -q \
  packages/strategy-factory/tests/test_signal_tracker_interval_runner.py \
  packages/strategy-factory/tests/test_runtime_sidecar_status.py \
  packages/strategy-factory/tests/test_runtime_bootstrap_independence.py \
  packages/strategy-factory/tests/test_runner_provider_neutrality.py
```

日志：`/tmp/aiask_signal_tracker_interval_tests_20261104.log`。`git diff --check` 与定向编译检查通过。

新增测试覆盖首轮无等待、首轮阻塞时不重入、真实完成后等待、心跳保留回填42条结果、实际服务异常的退避重试及失败证据、长业务周期心跳、取消业务任务与停止状态、CLI参数及提前拒绝。

间隔运行只会补录已成熟的数据，不能使5/20/40日证据提前成熟。此改动不增加任何实盘或券商写入权限。
