# 控制面修复 C1-C4

- C1：公开与持久运行状态机允许queued→failed；启动失败保留failure_kind、stderr、error、finished_at。只吞已确认终态竞态，其他ValueError上抛。dispatcher/worker在初始化前通过公开API注册工厂schema，修复单独进程在新库缺表的问题。
- C2：四个真实runner通过AIASK_FACTORY_RESULT_PATH原子写aiask.factory_result.v1结果信封；worker验证版本/工厂身份/完整对象，不截断业务结果再解析。旧runner的stdout兼容路径仅解析独立顶层完整对象，空/未知状态fail-closed。真实partial/blocked不被product_success=False覆盖；非零进程退出仍失败。心跳复用shielded communicate task，避免每10秒取消读取丢字节。
- C3：生产supervisor与catchup按64K块读取并增量UTF-8解码，无单行长度限制；异常终止子进程并向supervisor传播，监督任务异常触发整组停止和非零返回，不能继续伪健康心跳。
- C4：诊断器严格按db_path使用mode=ro/query_only连接，不调用get_db.initialize；不存在的库不创建。

新增行为回归：packages/agent/tests/test_factory_control_audit_regressions.py、packages/strategy-factory/tests/test_supervisor_diagnostic_audit_regressions.py。

控制面相关7文件最新合计52 passed (10.53s)；四runner结果传递/CLI/MCP端到端/边界与事件runner组70 passed (18.24s)，均为hermetic临时库与合成子进程，不代表生产运行。

真实runner路径测试覆盖strategy/factor/incubation/event各自一次业务分支，仅stub业务runtime，实际调用write_runner_result→worker→真实ledger。
