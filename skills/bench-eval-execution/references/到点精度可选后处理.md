# 可选到点精度后处理

默认关闭。新回灌在预测交付和正式服务恢复后启用；已有工作站任务可独立补算。依赖距离 CSV、车速、双轮 StartDist、同场景 XCP jerk 和原产品收据，不依赖轨迹/视觉评测报告。

```text
已验证预测产品 + 同场景 XCP
  → 输入解析/来源与代码身份检查
  → 已审核的独立触地计算核心
  → 事件匹配、误差与分母统计
  → analysis_plan.json + XLSX + 独立尝试收据
```

## 公开源码与运行依赖

提供 CSV/XCP 解析、重复时间戳取样、产品来源核对、工作站/SSH 编排及 openpyxl 报表源码。内部固定核心、原始数据、真实哈希和完整 MDF/MF4 流程不分发；公开示例不证明生产兼容。先提供经审核的 `speedbump_accuracy.py` 及来源清单，并在独立维护版本中配置真实核心哈希和设备路径。数组接口需要 `find_jerk_impacts`、`find_model_detections`、`assign_detections`、`calc_error` 及配套常量；还需核对适配器所用选项和阈值。核心调用依赖 NumPy/SciPy；XLSX 需要 openpyxl 和 LibreOffice。扩展构建器另需显式依赖目录中的 openpyxl/et_xmlfile。

输入需与当前 Job/Profile、产品收据和同场景真值一致。缺输入、来源变化或未知执行结果时保留证据，不重新回灌。公开测试使用合成输入和核心替身，只检查编排契约。

## 统计与状态

保留预期事件、门禁失败、未匹配和超范围明细。检出率与精度分母分开；仅门禁通过、有限误差且绝对值不大于 500 mm 的轮次进入精度统计。P90/P95 使用绝对误差，均值和样本离散程度使用带符号误差；缺样本显示不可用。

不启用预测引导的触地修正，不猜测时钟物理同步或缺失补偿。默认适配模式是 `bench_csv_trial_v1`，不宣称与原生 MDF/MF4 全流程等价。失败只重试本阶段，不能用回灌成功代替到点精度成功；无明确验收阈值不作达标结论。

## 调用形式

适配并验证运行环境后，统一入口增加 `-ArrivalAccuracy -ArrivalTruthRoot <当前工作站真值根>`；已有任务使用 `scripts/run_arrival_accuracy.ps1 -Enabled -TaskId <任务ID> -TruthRoot <真值根>` 规划，显式增加 `-Execute` 才计算。不传开关时不连接 SSH；每次计算使用新尝试 ID。
