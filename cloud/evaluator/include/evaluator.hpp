/*!
 * @file offline_evaluator.hpp
 * 离线评测器（替代旧的 EvaluationNode）
 *
 * 输入：
 *   - 预测目录 pred_data_root/<scene>/<frame_id>/{pred_road_<ts16>.npy, pred_traj_<ts16>.npy, traj_xy_<ts16>.npy, pred_img_<ts13>.json}
 *   - 真值目录 labeled_data_root/<scene>/<frame_id>/{occ_*_<ts16>_*.npy, dense_labeled_*_<ts16>_*.npy, img_label_*_<ts13>.json}
 *
 * 评测核心逻辑与原 EvaluationNode 完全一致；
 * 仅去掉 DAG/指针接收路径，改为按场景与帧顺序读盘驱动。
 */
#ifndef _EVALUATOR_HPP_
#define _EVALUATOR_HPP_

#include <chrono>
#include <fstream>
#include <iostream>
#include <memory>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <Eigen/Dense>
#include <Eigen/Geometry>
#include "compat/logging_compat.hpp"

#include "data/wheel_trajectory.hpp"
#include "data/road_surface.hpp"

#include "eval/iou.hpp"
#include "eval/map.hpp"
#include "postprocess/trajectory_surface_fitter.hpp"
#include "postprocess/traj_postprocess_manager.hpp"

using namespace uroad_m100;

// 辅助输出结构体（视觉分类任务，按 name 索引，与 dev-clean dump 协议一致）
struct AuxiliaryOutputs {
    std::map<std::string, std::vector<float>> heads;
};

class OfflineEvaluator {
public:
    OfflineEvaluator() = default;
    ~OfflineEvaluator() = default;

    // 加载配置 + 初始化（IoU/mAP 评测器、扫描场景）
    bool initialize(const std::string& yaml_path);

    // 主驱动：双层循环（场景 × 帧）逐帧评测
    bool runAll();

    // 输出最终统计信息（路面 IoU、轨迹 3+1、目标物、视觉 mAP）
    void writeFinalStats();

    // 真正的统计输出实现（由 writeFinalStats / runAll 末尾调用）
    void outputFinalStatistics();

    // ---------------- 通用数据结构 ----------------
    // OCC 点结构体（用于路面和轨迹评测）
    struct OccPoint {
        int x, y, z;
        int64_t label;
    };

    // 场景信息（真值/预测共享）
    struct SceneInfo {
        std::string scene_name;
        size_t total_samples;
        size_t completed_samples;
        std::map<uint64_t, size_t> timestamp_to_sample_idx;  // 真值时间戳(ms) -> 索引
        uint64_t min_timestamp_ms;
        uint64_t max_timestamp_ms;
        std::vector<std::string> sample_folder_paths;        // 排序后的真值样本路径
    };

    // 预测场景信息：扫描 pred_data_root 得到的逐帧 (时间戳 ms, 帧目录)
    struct PredSceneInfo {
        std::string scene_name;
        std::vector<std::pair<uint64_t, std::string>> frames;  // (ts_ms, frame_dir)
    };

    // 稠密点云（零拷贝访问：直接存储 5 维原始数据 [x,y,z,label,drivable]）
    struct DensePointCloud {
        std::vector<float> raw_data;
        size_t num_points;
        inline float   x(size_t i)     const { return raw_data[i * 5 + 0]; }
        inline float   y(size_t i)     const { return raw_data[i * 5 + 1]; }
        inline float   z(size_t i)     const { return raw_data[i * 5 + 2]; }
        inline int64_t label(size_t i) const { return static_cast<int64_t>(raw_data[i * 5 + 3]); }
    };

    // ---------------- 评测主流程 ----------------
    // 单帧评测：三个参数表示对应任务的预测是否已成功加载。
    // 同一帧按“路面 → 轨迹/目标物 → 视觉”顺序独立评测；任一任务失败不阻断其他任务。
    void performEvaluation(bool pred_road_ok, bool pred_traj_ok, bool pred_vision_ok);

    // 任务 1：路面评测
    std::vector<OccPoint> convertRoadSurfaceToOccIndices(const RoadSurface* road_surface_ptr);
    std::vector<OccPoint> extractSingleLayerFromGroundTruth(const std::vector<int64_t>& occ_raw_data);
    void evaluateRoadSurface(const std::vector<OccPoint>& predicted,
                             const std::vector<OccPoint>& ground_truth);

    // 任务 2：轨迹评测
    WheelTrajectory extractTrajectoryGroundTruthPhysical(const WheelTrajectory& traj_pred_physical,
                                                         const DensePointCloud& dense_cloud);
    void evaluateTrajectory(const WheelTrajectory& traj_pred_physical,
                            const WheelTrajectory& traj_gt_physical);

    // 任务 3：视觉分类评测（GT 按 name 索引，与 dump 协议一致）
    void evaluateVisionClassification(const AuxiliaryOutputs& predicted,
                                      const std::map<std::string, int>& ground_truth);

    // 任务 4：目标物评测（减速带 label=16 / 井盖 17 / 大桥接缝 23，跨帧锁帧代表帧化）
    struct TargetObject {
        float x_peak = 0.0f, z_peak = 0.0f;  // 极值点（段内 max z 的坐标）
        float x_leading = 0.0f;              // 前缘点 x（段内 x_min，离车最近）
        float mean_z = 0.0f;                 // 段内均值 z（17/23 高度指标）
        float width  = 0.0f;                 // 宽度（相邻点欧氏距离累加）
        int wheel_idx = -1;
        int label = 0;
        size_t frame_idx = static_cast<size_t>(-1);
        size_t scene_idx = static_cast<size_t>(-1);
    };
    struct TargetAnomalyRecord {
        size_t scene_idx;
        size_t frame_idx;
    };
    struct FalsePositiveRecord {
        size_t scene_idx;
        size_t frame_idx;
        int wheel_idx;
        int label;
        double reference_x;              // label=16 用 x_peak，label=17/23 用 x_leading
        bool has_nearest_gt;
        double nearest_gt_delta_x;       // has_nearest_gt=true 时有效
    };
    struct TargetStats {
        size_t total_pred_bumps = 0;
        size_t total_gt_bumps   = 0;
        size_t matched_bumps    = 0;
        size_t false_positive   = 0;
        size_t false_negative   = 0;
        std::array<size_t, 4> matched_per_wheel = {0};
        std::vector<double> width_errors;         // 三类均写入

        // x/z 误差按 label 分派
        std::vector<double> x_peak_errors;        // 仅 label=16（极值点 x）
        std::vector<double> x_leading_errors;     // 仅 label=17/23（前缘点 x）
        std::vector<double> z_peak_errors;        // 仅 label=16（极值点 z）
        std::vector<double> z_mean_errors;        // 仅 label=17/23（段均值 z）

        std::vector<FalsePositiveRecord> fp_records;
        std::vector<TargetAnomalyRecord> fn_records;
        std::vector<TargetAnomalyRecord> large_x_err_records;
        std::vector<TargetAnomalyRecord> large_z_err_records;  // 仅 label=16
        std::vector<TargetAnomalyRecord> large_w_err_records;
        size_t mid_x_error_count = 0;
        size_t mid_z_error_count = 0;  // 仅 label=16
    };
    std::map<int, TargetStats> focus_target_stats_;

    // 跨帧追踪：同一物理目标跨帧关联，reference_x < ideal 时持续更新代表帧
    struct TargetTrack {
        TargetObject rep;               // 代表帧观测（reference_x 最接近 ideal 的帧）
        double prev_ref_x = 0.0;        // 上一帧 reference_x（用于单调递减关联）
        double min_dist   = 1e9;        // ideal - reference_x 最小值
        int    label      = 0;          // 目标 label（16/17/23）
        bool   locked     = false;      // 是否已锁定代表帧
        bool   emitted    = false;      // 是否已 emit 到 scene_pred_reps_
    };

    std::vector<TargetObject> extractTargets(const WheelTrajectory& trajectory, int wheel_idx);
    TargetObject createTarget(int wheel_idx, int label);
    void evaluateTargets(const WheelTrajectory& pred_trajectory,
                            const WheelTrajectory& gt_trajectory);
    // 跨帧追踪：关联同物理目标；首次 reference_x < eval_x_ideal 时锁定代表帧。
    // reference_x: label=16 用 peak_x，label=17/23 用 x_leading。
    void trackAndEmitTargets(std::vector<TargetTrack>& tracks,
                           std::vector<TargetObject>& scene_reps,
                           const std::vector<TargetObject>& frame_bumps,
                           size_t frame_idx);
    void flushTargetStatsForScene();

private:
    // ---------------- 辅助函数 ----------------
    inline uint64_t getCurrentTimestampMs() const {
        return frame_start_timestamp_sec_ * 1000ULL + frame_start_timestamp_nsec_ / 1000000ULL;
    }

    // ---------------- 场景管理 ----------------
    bool buildTimestampMapForScene(SceneInfo& scene);
    bool initializeScenes();              // 扫描 labeled_data_root_
    bool initializePredScenes();          // 扫描 pred_data_root_
    size_t getSampleCountInScene(const std::string& scene_name);

    // ---------------- 真值加载（保留原有 NPY/JSON 解析逻辑）----------------
    /*
    loadRoadSurfaceGroundTruth() 是“加载某一个已知真值样本目录里的 occ 文件”；
    loadGroundTruthByTimestamp() 是“根据预测时间戳先找到该用哪一帧真值，再按任务分别调用多个底层加载函数”。
    */
    bool loadRoadSurfaceGroundTruth(const SceneInfo& current_scene, size_t sample_idx_in_scene,
                                    std::vector<int64_t>& occ_raw_data);
    bool loadDensePointCloud(const SceneInfo& current_scene, size_t sample_idx_in_scene,
                             DensePointCloud& dense_cloud);
    bool loadVisionClassificationGroundTruth(const SceneInfo& current_scene, size_t sample_idx_in_scene,
                                             std::map<std::string, int>& vision_labels);
    struct GroundTruthLoadStatus {
        bool timestamp_matched = false;
        bool road_ready = false;
        bool traj_ready = false;
        bool vision_ready = false;
        size_t sample_idx = static_cast<size_t>(-1);  // timestamp_matched=true 时对应的 GT 样本索引
    };

    struct TaskEvaluationStats {
        size_t prediction_available = 0;
        size_t ground_truth_available = 0;
        size_t evaluated = 0;
        size_t skipped_prediction_missing = 0;
        size_t skipped_ground_truth_missing = 0;
    };

    // 真值按时间戳定位一次；各任务文件的可用性独立返回。
    GroundTruthLoadStatus loadGroundTruthByTimestamp(uint64_t pred_timestamp_ms,
                                                      std::vector<int64_t>& occ_labels,
                                                      DensePointCloud& dense_cloud,
                                                      std::map<std::string, int>& vision_labels);

    // ---------------- 预测加载（NPY 解析）----------------
    // pred_road_<ts13>.npy：(H*W, 4) float32，每行 (x,y,z,label)
    bool loadPredRoadSurface(const std::string& frame_dir, uint64_t ts_ms, RoadSurface& out);
    // pred_traj_<ts13>.npy：|u1 字节流 = 16B [N_FL,N_FR,N_RL,N_RR] int32×4 + 各轮变长数据
    //   total_pts 输出各轮最小值（供 pts_num() 使用）
    bool loadPredTrajectory(const std::string& frame_dir, uint64_t ts_ms,
                            WheelTrajectory& out, int32_t& total_pts);
    // pred_img_<ts13>.json：{"head_name": [logits...]} KV 形式；按 vision_head_order_ 取需要的 head
    bool loadPredVision(const std::string& frame_dir, uint64_t ts_ms, AuxiliaryOutputs& out);

    // ---------------- 配置/状态 ----------------
    std::string config_file_path_;
    std::string pred_data_root_;        // 预测根目录（eval_dump 落盘根目录）
    std::string labeled_data_root_;     // 真值目录
    std::string scene_folder_prefix_;
    size_t max_scenes_for_debug_ = 0;
    bool eval_road_    = true;   // 路面评测（任务1）
    bool eval_traj_    = true;   // 轨迹逐点 + 目标物评测（任务2+4）
    bool eval_vision_  = true;   // 视觉分类评测（任务3）
    // 视觉头有序列表（由 vision.heads YAML 驱动；用于报表展示及缺失校验）
    using HeadSpec = std::pair<std::string, std::size_t>;  // (name, num_classes)
    std::vector<HeadSpec>      vision_head_order_;
    std::map<std::string, int> vision_label_offsets_;  // name -> label_offset (default 0)

    std::vector<SceneInfo> scenes_;
    std::vector<PredSceneInfo> pred_scenes_;  // 与 scenes_ 一一对齐
    size_t current_scene_idx_ = 0;
    std::map<std::string, int> vision_class_counts_;

    // 评测器
    std::unique_ptr<Iou> road_surface_iou_evaluator_;
    std::unique_ptr<Iou> trajectory_iou_evaluator_;
    std::unique_ptr<GlobalMAPCalculator> vision_map_calculator_;

    // 真值轨迹后处理器（替代旧 PostprocessNode 接口）
    EvalTrajPostprocessManager traj_postprocess_manager_;

    // ---------------- 当前帧上下文（替代旧的指针缓存）----------------
    RoadSurface     cur_pred_road_;
    WheelTrajectory cur_pred_trajectory_;
    AuxiliaryOutputs cur_pred_vision_;
    int32_t  trajectory_total_pts_ = 0;
    uint64_t frame_start_timestamp_sec_  = 0;
    uint64_t frame_start_timestamp_nsec_ = 0;
    size_t current_gt_sample_idx_ = static_cast<size_t>(-1);  // 当前时间戳匹配到的 GT 样本索引

    // ---------------- 计数器 ----------------
    size_t received_samples_  = 0;   // 至少有一个启用任务的预测加载成功的候选帧
    size_t evaluation_samples_ = 0;  // 进入真值时间戳匹配流程的候选帧
    size_t completed_samples_  = 0;  // 至少成功执行一个任务的帧
    size_t skipped_no_gt_samples_ = 0;       // 未匹配到真值时间戳的帧
    size_t skipped_all_tasks_samples_ = 0;   // 匹配真值后无任务可执行的帧
    TaskEvaluationStats road_task_stats_;
    TaskEvaluationStats traj_task_stats_;    // 含目标物评测
    TaskEvaluationStats vision_task_stats_;
    bool   has_output_final_stats_ = false;
    bool   all_scenes_completed_   = false;

    // ---------------- 轨迹统计 ----------------
    size_t trajectory_height_total_count_ = 0;
    size_t trajectory_height_correct_count_ = 0;
    size_t trajectory_pass_combined_count_ = 0;
    size_t trajectory_height_errors_reserve_capacity_ = 10000000;
    std::vector<double> trajectory_height_errors_;

    // 逐点关注类别（沥青/水泥）的点级统计
    // 减速带/井盖/大桥接缝 {16,17,23} 的点级/目标级已迁移到 focus_target_stats_（见 flush）
    struct FocusClassMetrics {
        size_t total = 0;                        // 该类真值总点数（gt_label==focus）
        size_t height_pass = 0;                  // z_diff <= tol 的点数（gt 单边）
        size_t combined_pass = 0;                // z_pass && label_match 的点数
        double z_error_sum = 0.0;                // 仅 pred_label==gt_label==focus 时累加（IoU 交集）
        size_t z_error_count = 0;                // 上面 z_error_sum 的样本数（作为 z_MAE 分母）
        double max_z_error = 0.0;                // 同上，只在 IoU 交集内更新
        std::string max_error_scene;
        size_t max_error_scene_idx = 0;
        size_t max_error_frame_idx = SIZE_MAX;
        int max_error_wheel = -1;
        size_t max_error_point_idx = 0;
    };
    std::map<int, FocusClassMetrics> focus_class_metrics_;

    // ---------------- 性能统计 ----------------
    std::vector<double> evaluation_times_;

    // ---------------- 预测网格参数 ----------------
    int    pred_NX_, pred_NY_, pred_NZ_;
    double pred_CELL_X_, pred_CELL_Y_, pred_CELL_Z_;
    double pred_X_MIN_, pred_Y_MIN_, pred_Z_MIN_;

    // ---------------- 真值网格参数 ----------------
    int    gt_NX_, gt_NY_, gt_NZ_;
    double gt_CELL_X_, gt_CELL_Y_, gt_CELL_Z_;
    double gt_X_MIN_, gt_Y_MIN_, gt_Z_MIN_;

    // ---------------- 稠密点云 / ROI ----------------
    size_t dense_cloud_reserve_capacity_ = 480000;
    size_t max_dense_cloud_points_ = 0;
    double roi_x_min_, roi_x_max_;
    double roi_y_min_, roi_y_max_;
    double roi_z_min_, roi_z_max_;
    size_t filtered_indices_reserve_capacity_ = 400000;
    std::vector<size_t> filtered_indices_;
    size_t max_filtered_indices_size_ = 0;

    // ---------------- 轨迹真值提取 ----------------
    double dense_cloud_search_radius_ = 0.10;
    double dense_cloud_search_radius_sq_ = 0.01;
    size_t dense_cloud_min_points_ = 0;
    size_t neighbor_reserve_capacity_ = 100;
    size_t max_neighbor_z_size_ = 0;
    TrajectorySurfaceFitter::Options surface_fitter_options_;
    std::unordered_map<int64_t, std::vector<size_t>> grid_hash_;
    std::vector<float> neighbor_z_;
    std::vector<int64_t> neighbor_labels_;
    std::unordered_map<int64_t, int> label_count_;

    // ---------------- 评测阈值 ----------------
    double trajectory_z_tolerance_m_ = 0.01;

    // 逐点评测的关注类别：仅沥青/水泥（无目标物概念的路面类）；
    // 减速带/井盖/大桥接缝 {16, 17, 23} 走目标级+代表帧点级评测，见 focus_target_stats_
    static const std::unordered_set<int>& getFocusClasses() {
        static const std::unordered_set<int> focus_classes = {1, 2};
        return focus_classes;
    }
    // 中文名映射保持完整（其他模块也用来查 label 名字）
    static const std::map<int, std::string>& getFocusClassNames() {
        static const std::map<int, std::string> class_names = {
            {1, "沥青"}, {2, "水泥"}, {16, "减速带"}, {17, "井盖"}, {23, "大桥接缝"}
        };
        return class_names;
    }

    // ---------------- 调试统计 ----------------
    double min_pred_z_ = 1e9, max_pred_z_ = -1e9;
    double min_gt_z_   = 1e9, max_gt_z_   = -1e9;
    double max_z_diff_ = 0;

    // ---------------- 路面评测临时容器 ----------------
    std::vector<int64_t> pred_labels_;
    std::vector<int64_t> gt_labels_;
    std::vector<int> pred_z_;
    std::vector<int> gt_z_;
    std::vector<std::vector<std::vector<std::pair<int, int64_t>>>> column_data_;

    // ---------------- 目标物评测（减速带 16 / 井盖 17 / 大桥接缝 23） ----------------
    double target_eval_x_min_ = 9.0;
    double target_eval_x_max_ = 13.0;
    double eval_x_ideal_ = 10.8;         // 代表帧择优目标 x（|peak_x - ideal| 最小者胜出）
    double same_target_dx_max_ = 2.5;   // 跨帧关联：reference_x 单调递减且 Δx < 此值 → 同一物理目标
    size_t TARGET_MIN_POINTS = 3;
    std::set<int> focus_target_labels_{16, 17, 23};   // 从 YAML 加载：{减速带, 井盖, 大桥接缝}
    double TARGET_MATCH_THRESHOLD = 0.5;
    size_t TARGET_ERRORS_RESERVE = 3000;
    double TARGET_X_ERR_LARGE = 0.20;
    double TARGET_Z_ERR_LARGE = 0.05;
    double TARGET_W_ERR_LARGE = 0.20;
    double TARGET_X_ERR_MID   = 0.10;
    double TARGET_Z_ERR_MID   = 0.02;
    std::vector<const WheelPoint*> target_points_buffer_;   // 段构建复用缓冲（避免频繁 alloc）
    size_t max_target_points_size_ = 0;
    size_t max_target_errors_size_  = 0;

    // 跨帧追踪状态（每场景内清空）
    std::array<std::vector<TargetTrack>, 4> active_pred_tracks_{}; // 仅 pred 跨帧锁帧
    std::array<std::vector<TargetObject>, 4> scene_pred_reps_{};    // 已 emit 的 pred 代表帧
    // GT 按帧缓存：pred 锁定后按 frame_idx 直接查找同帧 GT 目标（不做 GT 跨帧锁帧）
    std::array<std::map<size_t, std::vector<TargetObject>>, 4> scene_gt_by_frame_{};

    // ---------------- PCD 保存（已移除）----------------
};

#endif // _EVALUATOR_HPP_


/*
这个 include/evaluator.hpp 基本就是整个离线评测工具的总控头文件：它声明了 OfflineEvaluator 这个核心类，把“配置加载、场景扫描、逐帧评测、结果汇总”全串起来了。

如果只看一句话：

evaluator.hpp 定义了评测器的“总状态机”和所有核心数据结构；真正实现分散在 src/evaluator.cpp、src/data_loader.cpp、src/eval_road_surface.cpp、src/eval_trajectory.cpp、src/eval_vision.cpp、src/eval_statistics.cpp。

1. 这个文件的定位
从类入口看，OfflineEvaluator 提供的对外接口很少，只有 4 个主方法，见 evaluator.hpp:44-59：

initialize(...)
runAll()
writeFinalStats()
outputFinalStatistics()
这说明它不是某个单一算法类，而是一个总控 orchestrator。

你可以把它理解成：

初始化阶段：读 YAML、初始化 IoU/mAP 评测器、扫描数据目录
执行阶段：按场景和帧循环，加载 pred / GT，分别调用路面、轨迹、视觉、目标物评测
收尾阶段：输出最终统计报表
2. 头文件顶部的信息在说什么
文件开头注释 evaluator.hpp:1-11 已经点明了设计意图：

它是“离线评测器”，替代旧的 EvaluationNode
输入来自硬盘目录，而不是 DAG/消息流
评测逻辑尽量保持和旧实现一致
只是把“在线节点式输入”改成了“离线按场景、按帧读盘”
这和项目文档是一致的：核心算法不变，调度方式换成 PC 上离线回放。

3. 这个类的对外主流程
3.1 initialize
声明在 evaluator.hpp:49-50

作用大致是：

读取配置文件
初始化各种评测器
扫描真值场景和预测场景
建立场景和时间戳映射
这是“准备阶段”。

3.2 runAll
声明在 evaluator.hpp:52-53

这是主循环。

它负责：

外层按 pred_scenes_ 遍历场景
内层按每个场景的 frames 遍历帧
每帧先加载预测
再按时间戳匹配真值
再调各任务评测函数
可以认为它是整个程序的“发动机”。

3.3 writeFinalStats / outputFinalStatistics
声明在 evaluator.hpp:55-59

这两个名字看起来接近，但职责略有区分：

writeFinalStats()：更像对外入口
outputFinalStatistics()：真正干报表输出的实现
从命名上看，这是一个“包装 + 实现”的关系。

4. 这个头文件里定义了哪些核心数据结构
4.1 AuxiliaryOutputs
见 evaluator.hpp:39-42


struct AuxiliaryOutputs {
    std::map<std::string, std::vector<float>> heads;
};
这是视觉分类预测结果的容器。

含义：

key：head 名字，比如 basic_road_surface
value：这个 head 的 logits 向量
也就是说，视觉任务不是固定写死字段，而是按 head 名称动态索引。

4.2 OccPoint
见 evaluator.hpp:61-66


struct OccPoint {
    int x, y, z;
    int64_t label;
};
这是路面 / OCC 相关的基础点结构。

本质上是一个栅格坐标 + 类别。

主要用于：

预测路面结果转成 OCC 索引
GT OCC 提取单层表示
之后送入 IoU 评测器
4.3 SceneInfo
见 evaluator.hpp:68-77

这个结构体描述真值场景：

scene_name
total_samples
completed_samples
timestamp_to_sample_idx
min_timestamp_ms / max_timestamp_ms
sample_folder_paths
其中最关键的是：

timestamp_to_sample_idx
它把：

真值时间戳 ms
映射到该场景里的 sample 索引
这是后续“按预测时间戳找 GT”的基础。

4.4 PredSceneInfo
见 evaluator.hpp:79-83

这是预测场景的信息：


std::vector<std::pair<uint64_t, std::string>> frames;
也就是每帧只有两件关键事：

时间戳
帧目录路径
相比真值场景，预测侧结构更简单，因为预测数据是按目录组织好的帧文件集合。

4.5 DensePointCloud
见 evaluator.hpp:85-93

这是轨迹 GT 提取时使用的稠密点云包装。

特点：

raw_data 直接存原始 float 数组
num_points 记录点数
通过 x(i) / y(i) / z(i) / label(i) 做零拷贝访问
这说明作者很在意性能：

不是把每个点展开成 struct 数组，而是保留原始连续内存，按步长访问。

5. 四类评测任务在头文件里的对应接口
5.1 任务1：路面评测
见 evaluator.hpp:99-103

主要接口：

convertRoadSurfaceToOccIndices(...)
extractSingleLayerFromGroundTruth(...)
evaluateRoadSurface(...)
逻辑分层很清晰：

先把预测转成 GT 栅格体系下的 OCC 点
再把 GT 3D OCC 压成单层表示
最后评 IoU
这和 src/eval_road_surface.cpp 对应。

5.2 任务2：轨迹评测
见 evaluator.hpp:105-109

主要接口：

extractTrajectoryGroundTruthPhysical(...)
evaluateTrajectory(...)
这部分的关键不是简单“读 GT”，而是：

以预测轨迹点为查询点
去稠密点云里做邻域搜索
反推出对应的 GT z/label
所以它本质上是“轨迹 GT 提取 + 指标评测”两步。

5.3 任务3：视觉分类评测
见 evaluator.hpp:111-113

接口很简单：

evaluateVisionClassification(...)
因为视觉任务的数据准备主要在加载阶段做完了，评测阶段只需要把：

predicted 的各 head logits
ground_truth 的各 head label
喂给 GlobalMAPCalculator 即可。

5.4 任务4：目标物评测
见 evaluator.hpp:115-174

这是头文件里最复杂的一块之一。

对象是：

减速带 16
井盖 17
大桥接缝 23
它不是简单逐点，而是跨帧目标级评测。

核心接口：

extractTargets(...)
createTarget(...)
evaluateTargets(...)
trackAndEmitTargets(...)
flushTargetStatsForScene()
这说明作者把目标物评测拆成了几层：

从单帧轨迹里切出候选目标段
把目标段转成一个结构化 TargetObject
在跨帧维度做 tracking / representative frame 锁定
在场景收尾时统一做统计 flush
6. 目标物相关几个结构体怎么理解
6.1 TargetObject
见 evaluator.hpp:116-125

这个结构表示一个物理候选目标。

关键字段：

x_peak, z_peak：极值点
x_leading：前缘点
mean_z：平均高度
width：宽度
wheel_idx
label
frame_idx
scene_idx
也就是说，一个目标不是单个点，而是一个“段”的摘要特征。

6.2 TargetStats
见 evaluator.hpp:130-151

这是按目标类别累计统计的容器。

里面既有计数：

total_pred_bumps
total_gt_bumps
matched_bumps
false_positive
false_negative
也有误差分布：

width_errors
x_peak_errors
x_leading_errors
z_peak_errors
z_mean_errors
也有异常样本记录：

fp_records
fn_records
large_x_err_records
large_z_err_records
large_w_err_records
说明这套评测不仅关心 KPI，还很关心问题样本回溯能力。

6.3 TargetTrack
见 evaluator.hpp:154-162

这是跨帧关联的状态对象。

关键字段：

rep：当前代表帧观测
prev_ref_x：上一帧参考 x
min_dist：离 ideal x 最近的距离
label
locked
emitted
这说明目标物评测的设计思想是：

一个物理目标在多帧里会被多次看到，但最终只取一个“代表帧”来统计。

7. private 区域：这个类真正维护了什么状态
这部分是理解这个类最重要的地方。

它基本把状态分成多个块，每块前面都有注释。

7.1 场景管理
见 evaluator.hpp:182-186

主要函数：

buildTimestampMapForScene
initializeScenes
initializePredScenes
getSampleCountInScene
这部分负责把磁盘目录组织成内存里的 scenes_ / pred_scenes_。

7.2 真值加载 / 预测加载
见 evaluator.hpp:188-208

真值加载接口
loadRoadSurfaceGroundTruth
loadDensePointCloud
loadVisionClassificationGroundTruth
loadGroundTruthByTimestamp
预测加载接口
loadPredRoadSurface
loadPredTrajectory
loadPredVision
这说明 OfflineEvaluator 虽然是总控类，但它也承担了相当一部分 IO 调度工作，而不是把 IO 单独拆成独立 manager。

7.3 配置 / 运行开关
见 evaluator.hpp:210-227

重要字段：

config_file_path_
pred_data_root_
labeled_data_root_
scene_folder_prefix_
max_scenes_for_debug_
eval_road_
eval_traj_
eval_vision_
vision_head_order_
vision_label_offsets_
vision_class_counts_
这一组就是 YAML 配置在类内的落地状态。

特别值得注意的是视觉部分：

vision_head_order_：有序，用于展示和缺失校验
vision_class_counts_：按 name 过滤评测 head
vision_label_offsets_：处理 GT label 偏移
说明视觉任务的设计是配置驱动、按 name 绑定，而不是写死 enum。

7.4 评测器对象
见 evaluator.hpp:229-235

road_surface_iou_evaluator_
trajectory_iou_evaluator_
vision_map_calculator_
traj_postprocess_manager_
这里体现出一个很重要的设计：

OfflineEvaluator 不是算法本体
它更像一个总调度器，真正算指标的是：

Iou
GlobalMAPCalculator
EvalTrajPostprocessManager
7.5 当前帧上下文缓存
见 evaluator.hpp:237-244

cur_pred_road_
cur_pred_trajectory_
cur_pred_vision_
trajectory_total_pts_
frame_start_timestamp_sec_
frame_start_timestamp_nsec_
这是一个非常关键的设计点：

这个类用“当前帧缓存”替代了旧在线系统里的“指针消息缓存”。

也就是说，当前正在评测的这一帧的数据，会被先存成类成员，再由 performEvaluation() 消费。

这也是注释里说的“替代旧的指针缓存”。

7.6 样本计数器
见 evaluator.hpp:245-251

received_samples_
evaluation_samples_
completed_samples_
skipped_no_gt_samples_
has_output_final_stats_
all_scenes_completed_
这些就是最终报表里“完成度”“跳过样本”等统计的来源。

7.7 轨迹逐点统计
见 evaluator.hpp:253-275

全局统计
trajectory_height_total_count_
trajectory_height_correct_count_
trajectory_pass_combined_count_
trajectory_height_errors_
关注类别统计
FocusClassMetrics
focus_class_metrics_
FocusClassMetrics 里不仅有总量、通过量，还有：

z_error_sum
max_z_error
max_error_scene
max_error_frame_idx
max_error_wheel
max_error_point_idx
这说明它专门支持“最大误差点回溯”。

7.8 性能统计
见 evaluator.hpp:277-278

evaluation_times_
这部分用于统计每帧/整体评测耗时。

7.9 网格参数
见 evaluator.hpp:280-288

分成两套：

预测网格参数 pred_*
GT 网格参数 gt_*
这是路面评测很重要的一点：

预测和 GT 不一定共用同一套网格精度，所以需要两套参数。

7.10 稠密点云搜索相关缓存
见 evaluator.hpp:290-310

这里有很多性能优化成员：

filtered_indices_
grid_hash_
neighbor_z_
neighbor_labels_
label_count_
说明轨迹 GT 提取时，作者并不是暴力搜点，而是：

先 ROI 筛
再哈希网格
再邻域缓存复用
这是整个程序的性能热点之一。

7.11 评测阈值与关注类别
见 evaluator.hpp:312-327

trajectory_z_tolerance_m_
getFocusClasses()
getFocusClassNames()
这里一个值得注意的设计是：

点级关注类和目标级关注类被分开了
点级 focus class：{1, 2}，即沥青/水泥
目标级 focus label：{16, 17, 23}，即减速带/井盖/大桥接缝
这说明作者已经把“普通路面类的点级统计”和“特殊目标类的目标级统计”分开建模了，避免混淆。

7.12 路面评测临时容器
见 evaluator.hpp:334-339

pred_labels_
gt_labels_
pred_z_
gt_z_
column_data_
这些是为了路面 IoU 评测复用的中间缓冲区，避免每帧重复分配。

7.13 目标物评测运行态
见 evaluator.hpp:341-363

包括：

窗口参数 target_eval_x_min_, target_eval_x_max_, eval_x_ideal_
关联阈值 same_target_dx_max_
匹配阈值 TARGET_MATCH_THRESHOLD
大/中误差阈值
缓冲区 target_points_buffer_
跨帧状态 active_pred_tracks_
已产出代表帧 scene_pred_reps_
GT 帧缓存 scene_gt_by_frame_
这一块就是目标物评测的全部状态基础。

8. 这个头文件体现出的几个设计特点
8.1 它是“大总管”类
优点：

单点入口明确
状态都集中，调试方便
代价：

头文件比较大
成员很多，理解成本高
类职责偏重
所以它是一个很典型的“工程总控类”，而不是纯粹优雅的小类设计。

8.2 很强调“复用缓存，避免频繁分配”
你会看到大量：

reserve_capacity
max_*_size_
复用向量 / map / hash
这说明程序设计时明确考虑过：

稠密点云数据量大
单帧处理频繁
如果每帧都重新分配容器，性能会很差
8.3 既做调度，也做 IO 编排，也做状态累计
OfflineEvaluator 同时管：

配置
场景扫描
文件读取调度
当前帧缓存
指标累计
报表输出入口
因此理解它时，最好的方式不是把它看成“某个算法”，而是看成：

离线评测流程的控制中心

9. 你读这个文件时最应该抓住的主线
如果你想快速建立脑图，建议只抓这条链：

初始化

initialize()
扫场景、读 YAML、建评测器
主循环

runAll()
遍历 pred_scenes_
每帧加载

loadPredRoadSurface / loadPredTrajectory / loadPredVision
按时间戳找 GT

loadGroundTruthByTimestamp()
分任务评测

evaluateRoadSurface()
evaluateTrajectory()
evaluateVisionClassification()
evaluateTargets()
场景收尾 / 全局收尾

flushTargetStatsForScene()
writeFinalStats()
只要这条线通了，剩下成员变量基本都能找到归属。

10. 如果要继续深挖，推荐阅读顺序
建议从声明跳到实现，按这个顺序看最顺：

include/evaluator.hpp
src/evaluator.cpp
看总流程
src/data_loader.cpp
看场景扫描、时间戳匹配、文件解析
src/eval_trajectory.cpp
最复杂，含轨迹 + 目标物
src/eval_road_surface.cpp
src/eval_vision.cpp
src/eval_statistics.cpp
*/