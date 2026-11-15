# evaluator.hpp 解读

`include/evaluator.hpp` 是整个离线评测工具的总控头文件：它声明了 `OfflineEvaluator` 这个核心类，把配置加载、场景扫描、逐帧评测、结果汇总这些主流程都串起来了。

如果只用一句话概括：

> `evaluator.hpp` 定义了评测器的总状态机和核心数据结构；真正的实现分散在 `src/evaluator.cpp`、`src/data_loader.cpp`、`src/eval_road_surface.cpp`、`src/eval_trajectory.cpp`、`src/eval_vision.cpp`、`src/eval_statistics.cpp`。

---

## 1. 文件定位

从类入口看，`OfflineEvaluator` 对外接口很少，核心是四个主方法：

- `initialize(...)`
- `runAll()`
- `writeFinalStats()`
- `outputFinalStatistics()`

这说明它不是一个单一算法类，而是一个总控调度器。

可以把它理解成三阶段：

1. **初始化阶段**：读 YAML、初始化 IoU/mAP 评测器、扫描数据目录
2. **执行阶段**：按场景和帧循环，加载 pred / GT，分别调用路面、轨迹、视觉、目标物评测
3. **收尾阶段**：输出最终统计报表

---

## 2. 文件开头注释传达的设计意图

头部注释说明：

- 它是“离线评测器”，替代旧的 `EvaluationNode`
- 输入来自硬盘目录，而不是 DAG/消息流
- 评测逻辑尽量保持和旧实现一致
- 只是把“在线节点式输入”改成了“离线按场景、按帧读盘”

也就是：**核心算法不变，调度方式改成 PC 侧离线回放。**

---

## 3. 对外主流程接口

### 3.1 `initialize`

作用大致是：

1. 读取配置文件
2. 初始化各种评测器
3. 扫描真值场景和预测场景
4. 建立场景和时间戳映射

它属于“准备阶段”。

### 3.2 `runAll`

这是主循环，负责：

- 外层按 `pred_scenes_` 遍历场景
- 内层按每个场景的 `frames` 遍历帧
- 每帧先加载预测
- 再按时间戳匹配真值
- 再调各任务评测函数

可以把它看成整个程序的发动机。

### 3.3 `writeFinalStats` / `outputFinalStatistics`

这两个名字接近，但职责略有区分：

- `writeFinalStats()`：更像对外入口
- `outputFinalStatistics()`：真正负责报表输出的实现

---

## 4. 核心数据结构

### 4.1 `AuxiliaryOutputs`

这是视觉分类预测结果容器：

- key：head 名字，比如 `basic_road_surface`
- value：该 head 的 logits 向量

因此视觉任务不是固定字段，而是按 head 名称动态索引。

### 4.2 `OccPoint`

这是路面 / OCC 的基础点结构，本质上是“栅格坐标 + 类别”。

主要用于：

- 预测路面结果转成 OCC 索引
- GT OCC 提取单层表示
- 后续送入 IoU 评测器

### 4.3 `SceneInfo`

这个结构描述真值场景，包含：

- `scene_name`
- `total_samples`
- `completed_samples`
- `timestamp_to_sample_idx`
- `min_timestamp_ms` / `max_timestamp_ms`
- `sample_folder_paths`

其中最关键的是 `timestamp_to_sample_idx`，它把真值时间戳映射到场景内样本索引，是后续按预测时间戳找 GT 的基础。

### 4.4 `PredSceneInfo`

这是预测场景信息，核心字段是：

- `frames = [(ts_ms, frame_dir), ...]`

相比真值场景，预测侧结构更简单，因为预测数据已经按帧目录组织好了。

### 4.5 `DensePointCloud`

这是轨迹 GT 提取用的稠密点云包装，特点是：

- `raw_data` 直接存原始 5 维 float 数组
- `num_points` 记录点数
- 通过 `x(i) / y(i) / z(i) / label(i)` 做零拷贝访问

这体现出对性能的考虑：避免把原始数据再展开成 struct 数组。

---

## 5. 四类评测任务接口

### 5.1 任务 1：路面评测

相关接口：

- `convertRoadSurfaceToOccIndices(...)`
- `extractSingleLayerFromGroundTruth(...)`
- `evaluateRoadSurface(...)`

逻辑分层是：

1. 先把预测转成 GT 网格体系下的 OCC 点
2. 再把 GT 的 3D OCC 压成单层表示
3. 最后评 IoU

### 5.2 任务 2：轨迹评测

相关接口：

- `extractTrajectoryGroundTruthPhysical(...)`
- `evaluateTrajectory(...)`

这部分不是直接“读 GT”，而是以预测轨迹点为查询点，到稠密点云里做邻域搜索，反推出 GT 的 `z/label`。

### 5.3 任务 3：视觉分类评测

相关接口：

- `evaluateVisionClassification(...)`

它比较简单，因为数据准备主要在加载阶段完成，评测时只是把预测 logits 和 GT label 喂给 `GlobalMAPCalculator`。

### 5.4 任务 4：目标物评测

相关接口：

- `extractTargets(...)`
- `createTarget(...)`
- `evaluateTargets(...)`
- `trackAndEmitTargets(...)`
- `flushTargetStatsForScene()`

它不是简单逐点评测，而是跨帧目标级评测，针对：

- 减速带 `16`
- 井盖 `17`
- 大桥接缝 `23`

主要步骤是：

1. 从单帧轨迹切出候选目标段
2. 把目标段转成 `TargetObject`
3. 在跨帧维度做 tracking / representative frame 锁定
4. 在场景收尾时统一做统计 flush

---

## 6. 目标物相关结构体

### 6.1 `TargetObject`

表示一个物理候选目标，关键字段包括：

- `x_peak`, `z_peak`：极值点
- `x_leading`：前缘点
- `mean_z`：平均高度
- `width`：宽度
- `wheel_idx`
- `label`
- `frame_idx`
- `scene_idx`

也就是说，一个目标不是单个点，而是一个“段”的摘要特征。

### 6.2 `TargetStats`

这是按目标类别累计统计的容器，里面既有计数：

- `total_pred_bumps`
- `total_gt_bumps`
- `matched_bumps`
- `false_positive`
- `false_negative`

也有误差分布：

- `width_errors`
- `x_peak_errors`
- `x_leading_errors`
- `z_peak_errors`
- `z_mean_errors`

还保存异常样本记录：

- `fp_records`
- `fn_records`
- `large_x_err_records`
- `large_z_err_records`
- `large_w_err_records`

说明这套设计既关心 KPI，也关心问题样本回溯。

### 6.3 `TargetTrack`

这是跨帧关联状态对象，关键字段包括：

- `rep`：当前代表帧观测
- `prev_ref_x`：上一帧参考 x
- `min_dist`：离 ideal x 最近的距离
- `label`
- `locked`
- `emitted`

设计思想是：一个物理目标在多帧中会被多次看到，但最终只取一个代表帧统计。

---

## 7. private 区域维护的主要状态

### 7.1 场景管理

相关函数：

- `buildTimestampMapForScene`
- `initializeScenes`
- `initializePredScenes`
- `getSampleCountInScene`

负责把磁盘目录组织成内存中的 `scenes_` / `pred_scenes_`。

### 7.2 真值加载 / 预测加载

真值加载接口：

- `loadRoadSurfaceGroundTruth`
- `loadDensePointCloud`
- `loadVisionClassificationGroundTruth`
- `loadGroundTruthByTimestamp`

预测加载接口：

- `loadPredRoadSurface`
- `loadPredTrajectory`
- `loadPredVision`

这说明 `OfflineEvaluator` 既是总控，也承担了较多 IO 编排工作。

### 7.3 配置 / 运行开关

主要字段：

- `config_file_path_`
- `pred_data_root_`
- `labeled_data_root_`
- `scene_folder_prefix_`
- `max_scenes_for_debug_`
- `eval_road_`
- `eval_traj_`
- `eval_vision_`
- `vision_head_order_`
- `vision_label_offsets_`
- `vision_class_counts_`

这组就是 YAML 配置在类内的落地状态。

其中视觉部分体现了配置驱动的设计：

- `vision_head_order_`：有序，用于展示和缺失校验
- `vision_class_counts_`：按 name 过滤评测 head
- `vision_label_offsets_`：处理 GT label 偏移

### 7.4 评测器对象

主要成员：

- `road_surface_iou_evaluator_`
- `trajectory_iou_evaluator_`
- `vision_map_calculator_`
- `traj_postprocess_manager_`

这里说明 `OfflineEvaluator` 本身不是算法本体，而是总调度器；真正做指标计算的是这些独立组件。

### 7.5 当前帧上下文缓存

主要成员：

- `cur_pred_road_`
- `cur_pred_trajectory_`
- `cur_pred_vision_`
- `trajectory_total_pts_`
- `frame_start_timestamp_sec_`
- `frame_start_timestamp_nsec_`

这是一个关键设计点：用当前帧缓存替代了旧在线系统里的指针消息缓存。

### 7.6 样本计数器

主要成员：

- `received_samples_`
- `evaluation_samples_`
- `completed_samples_`
- `skipped_no_gt_samples_`
- `has_output_final_stats_`
- `all_scenes_completed_`

这些是最终报表中完成度、跳过样本等统计的来源。

### 7.7 轨迹逐点统计

包括：

- 全局统计：`trajectory_height_total_count_`、`trajectory_height_correct_count_`、`trajectory_pass_combined_count_`、`trajectory_height_errors_`
- 关注类别统计：`FocusClassMetrics`、`focus_class_metrics_`

`FocusClassMetrics` 不仅记录总量、通过量，还记录最大误差及其场景/帧/轮位/点索引，用于问题回溯。

### 7.8 性能统计

- `evaluation_times_`

用于统计每帧或整体评测耗时。

### 7.9 网格参数

分成两套：

- 预测网格参数 `pred_*`
- GT 网格参数 `gt_*`

这是因为预测和 GT 不一定共用同一套网格精度。

### 7.10 稠密点云搜索相关缓存

例如：

- `filtered_indices_`
- `grid_hash_`
- `neighbor_z_`
- `neighbor_labels_`
- `label_count_`

这说明轨迹 GT 提取时并不是暴力搜索，而是使用 ROI + 哈希网格 + 邻域缓存复用来提速。

### 7.11 评测阈值与关注类别

关键成员：

- `trajectory_z_tolerance_m_`
- `getFocusClasses()`
- `getFocusClassNames()`

这里体现了一个重要区分：

- 点级关注类：`{1, 2}`，即沥青/水泥
- 目标级关注类：`{16, 17, 23}`，即减速带/井盖/大桥接缝

也就是普通路面类走点级统计，特殊目标类走目标级统计。

### 7.12 路面评测临时容器

- `pred_labels_`
- `gt_labels_`
- `pred_z_`
- `gt_z_`
- `column_data_`

这部分是为了路面 IoU 评测复用的中间缓冲区，避免每帧频繁分配。

### 7.13 目标物评测运行态

包括：

- 窗口参数 `target_eval_x_min_`, `target_eval_x_max_`, `eval_x_ideal_`
- 关联阈值 `same_target_dx_max_`
- 匹配阈值 `TARGET_MATCH_THRESHOLD`
- 大/中误差阈值
- 缓冲区 `target_points_buffer_`
- 跨帧状态 `active_pred_tracks_`
- 已产出代表帧 `scene_pred_reps_`
- GT 帧缓存 `scene_gt_by_frame_`

这一块构成了目标物评测的全部运行态。

---

## 8. 这个头文件体现出的设计特点

### 8.1 它是“大总管”类

优点：

- 单点入口明确
- 状态集中，调试方便

代价：

- 头文件较大
- 成员很多，理解成本高
- 类职责偏重

### 8.2 强调缓存复用，避免频繁分配

可以看到大量 `reserve_capacity`、`max_*_size_` 以及复用向量 / map / hash 的设计，说明作者明确考虑过稠密点云数据量大、逐帧处理频繁带来的性能问题。

### 8.3 既做调度，也做 IO 编排，也做状态累计

`OfflineEvaluator` 同时管理：

- 配置
- 场景扫描
- 文件读取调度
- 当前帧缓存
- 指标累计
- 报表输出入口

因此理解它时，最好把它看成离线评测流程的控制中心，而不是某个单一算法。

---

## 9. 阅读这个文件时最重要的主线

建议抓住下面这条调用链：

1. **初始化**：`initialize()`，扫场景、读 YAML、建评测器
2. **主循环**：`runAll()`，遍历 `pred_scenes_`
3. **每帧加载预测**：`loadPredRoadSurface / loadPredTrajectory / loadPredVision`
4. **按时间戳找 GT**：`loadGroundTruthByTimestamp()`
5. **分任务评测**：
   - `evaluateRoadSurface()`
   - `evaluateTrajectory()`
   - `evaluateVisionClassification()`
   - `evaluateTargets()`
6. **收尾**：`flushTargetStatsForScene()`、`writeFinalStats()`

只要这条主线理顺，剩下成员变量基本都能找到归属。

---

## 10. 推荐的后续阅读顺序

如果想继续深挖实现，推荐按这个顺序读：

1. `include/evaluator.hpp`
2. `src/evaluator.cpp` —— 看总流程
3. `src/data_loader.cpp` —— 看场景扫描、时间戳匹配、文件解析
4. `src/eval_trajectory.cpp` —— 最复杂，含轨迹 + 目标物
5. `src/eval_road_surface.cpp`
6. `src/eval_vision.cpp`
7. `src/eval_statistics.cpp`

---

## 总结

`evaluator.hpp` 的本质不是“实现某个算法”，而是声明了整个离线评测器的控制中心：

- 统一管理配置和场景
- 统一加载预测与真值
- 统一调度四类评测任务
- 统一累积统计并输出报表

因此，理解这个文件的关键不是逐成员死记，而是先抓住“初始化 → 主循环 → 加载 → 评测 → 汇总”这条主线。