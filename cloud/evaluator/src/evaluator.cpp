#include "evaluator.hpp"
#include "postprocess/trajectory_surface_fitter.hpp"
#include "compat/logging_compat.hpp"
#include <filesystem>
#include <iomanip>
#include <thread>
#include <chrono>
#include <yaml-cpp/yaml.h>
#include <map>
#include <set>
#include <unordered_map>
#include <unordered_set>
#include <cmath>
#include <numeric>
#include <algorithm>
#include <cstdlib>
#include <cstring>

namespace fs = std::filesystem;

// 旧 DAG 构造/析构/init/deinit/updateInput/run 已删除；离线主入口为 initialize/runAll/writeFinalStats。
bool OfflineEvaluator::initialize(const std::string& yaml_path)
{
    config_file_path_ = yaml_path;
    target_points_buffer_.reserve(50);
    LOG(INFO) << " OfflineEvaluator: Initializing from " << yaml_path;

    try {
        YAML::Node config = YAML::LoadFile(config_file_path_);

        // 加载数据路径与场景筛选配置
        if (config["dataset"]) {
            auto dataset_cfg = config["dataset"];
            labeled_data_root_   = dataset_cfg["labeled_data_root"].as<std::string>("");
            pred_data_root_      = dataset_cfg["pred_data_root"].as<std::string>("");
            scene_folder_prefix_ = dataset_cfg["scene_folder_prefix"].as<std::string>("L7_SH");
            max_scenes_for_debug_ = dataset_cfg["max_scenes_for_debug"].as<size_t>(0);
            if (max_scenes_for_debug_ > 0) {
                LOG(INFO) << " OfflineEvaluator: 调试模式：仅评测前 " << max_scenes_for_debug_ << " 个场景";
            }
            if (labeled_data_root_.empty() || pred_data_root_.empty()) {
                LOG(ERROR) << " OfflineEvaluator: labeled_data_root / pred_data_root 必填";
                return false;
            }
        } else {
            LOG(ERROR) << " OfflineEvaluator: 未找到 dataset 配置";
            return false;
        }

        // 加载评测任务开关
        if (config["eval_tasks"]) {
            auto tasks_cfg = config["eval_tasks"];
            eval_road_   = (tasks_cfg["eval_road"].as<std::string>("ON")   == "ON");
            eval_traj_   = (tasks_cfg["eval_traj"].as<std::string>("ON")   == "ON");
            eval_vision_ = (tasks_cfg["eval_vision"].as<std::string>("ON") == "ON");
        }
        LOG(INFO) << " OfflineEvaluator: 评测开关 road=" << (eval_road_ ? "ON" : "OFF")
                  << " traj=" << (eval_traj_ ? "ON" : "OFF")
                  << " vision=" << (eval_vision_ ? "ON" : "OFF");

        // 加载视觉头配置（vision.heads 列表，决定本次评测关心哪些 head 及其类别数）
        if (config["vision"] && config["vision"]["heads"] && config["vision"]["heads"].IsSequence()) {
            for (const auto& head : config["vision"]["heads"]) {
                std::string name = head["name"].as<std::string>();
                int num          = head["num_classes"].as<int>();
                int offset       = head["label_offset"].as<int>(0);
                vision_class_counts_[name] = num;
                vision_head_order_.emplace_back(name, static_cast<std::size_t>(num));
                if (offset != 0) vision_label_offsets_[name] = offset;
            }
            std::string heads_info;
            for (const auto& [n, c] : vision_head_order_)
                heads_info += n + "(" + std::to_string(c) + ") ";
            LOG(INFO) << " OfflineEvaluator: 视觉头 " << vision_head_order_.size()
                      << " 个: " << heads_info;
        } else {
            LOG(ERROR) << " OfflineEvaluator: 未找到 vision.heads 配置（需为 YAML 列表）";
            return false;
        }

        // ===== 预测网格参数（用于路面 OCC 索引化）=====
        if (config["road_surface"] && config["road_surface"]["predictor"]) {
            auto pred_cfg = config["road_surface"]["predictor"];
            pred_NX_ = pred_cfg["NX"].as<int>();
            pred_NY_ = pred_cfg["NY"].as<int>();
            pred_NZ_ = pred_cfg["NZ"].as<int>();
            pred_CELL_X_ = pred_cfg["CELL_X"].as<double>();
            pred_CELL_Y_ = pred_cfg["CELL_Y"].as<double>();
            pred_CELL_Z_ = pred_cfg["CELL_Z"].as<double>();
            pred_X_MIN_ = pred_cfg["X_MIN"].as<double>();
            pred_Y_MIN_ = pred_cfg["Y_MIN"].as<double>();
            pred_Z_MIN_ = pred_cfg["Z_MIN"].as<double>();
            LOG(INFO) << " OfflineEvaluator: 预测网格: " << pred_NX_ << "x" << pred_NY_ << "x" << pred_NZ_;
        } else {
            pred_NX_ = 250; pred_NY_ = 80; pred_NZ_ = 200;
            pred_CELL_X_ = 0.1; pred_CELL_Y_ = 0.1; pred_CELL_Z_ = 0.01;
            pred_X_MIN_ = 0.0; pred_Y_MIN_ = -4.0; pred_Z_MIN_ = -1.0;
        }

        // ===== 加载真值网格参数（评测专用，与预测参数可能不同）=====
        if (config["road_surface"] && config["road_surface"]["ground_truth_grid"]) {
            auto gt_cfg = config["road_surface"]["ground_truth_grid"];
            gt_NX_ = gt_cfg["NX"].as<int>(250);
            gt_NY_ = gt_cfg["NY"].as<int>(80);
            gt_NZ_ = gt_cfg["NZ"].as<int>(100);
            gt_CELL_X_ = gt_cfg["CELL_X"].as<double>(0.1);
            gt_CELL_Y_ = gt_cfg["CELL_Y"].as<double>(0.1);
            gt_CELL_Z_ = gt_cfg["CELL_Z"].as<double>(0.02);
            gt_X_MIN_ = gt_cfg["X_MIN"].as<double>(0.0);
            gt_Y_MIN_ = gt_cfg["Y_MIN"].as<double>(-4.0);
            gt_Z_MIN_ = gt_cfg["Z_MIN"].as<double>(-1.0);

            LOG(INFO) << " OfflineEvaluator: 真值网格参数: " << gt_NX_ << "×" << gt_NY_ << "×" << gt_NZ_
                      << ",体素大小(" << gt_CELL_X_ << "," << gt_CELL_Y_ << "," << gt_CELL_Z_ << ")m";
        } else {
            gt_NX_ = 250; gt_NY_ = 80; gt_NZ_ = 100;
            gt_CELL_X_ = 0.1; gt_CELL_Y_ = 0.1; gt_CELL_Z_ = 0.02;
            gt_X_MIN_ = 0.0; gt_Y_MIN_ = -4.0; gt_Z_MIN_ = -1.0;
        }

        // ===== 加载轨迹评测参数 =====
        if (config["trajectory"]) {
            auto traj_cfg = config["trajectory"];

            // 稠密点云加载
            if (traj_cfg["dense_cloud"]) {
                auto dc = traj_cfg["dense_cloud"];
                dense_cloud_reserve_capacity_ = dc["reserve_capacity"].as<size_t>(480000);
                filtered_indices_reserve_capacity_ = dc["filtered_indices_reserve_capacity"].as<size_t>(400000);
                dense_cloud_search_radius_ = dc["search_radius"].as<double>(0.10);
                dense_cloud_search_radius_sq_ = dense_cloud_search_radius_ * dense_cloud_search_radius_;
                dense_cloud_min_points_ = dc["min_points"].as<size_t>(0);
                neighbor_reserve_capacity_ = dc["neighbor_reserve_capacity"].as<size_t>(100);
            } else {
                dense_cloud_reserve_capacity_ = 480000;
                filtered_indices_reserve_capacity_ = 400000;
                dense_cloud_search_radius_ = 0.10;
                dense_cloud_search_radius_sq_ = dense_cloud_search_radius_ * dense_cloud_search_radius_;
                dense_cloud_min_points_ = 0;
                neighbor_reserve_capacity_ = 100;
            }
            LOG(INFO) << " OfflineEvaluator: 轨迹真值提取: radius=" << dense_cloud_search_radius_
                      << "m, min_pts=" << dense_cloud_min_points_
                      << ", reserve=" << neighbor_reserve_capacity_;

            // ROI过滤参数
            if (traj_cfg["point_cloud_roi"]) {
                auto roi_cfg = traj_cfg["point_cloud_roi"];
                roi_x_min_ = roi_cfg["x_min"].as<double>(5.0);
                roi_x_max_ = roi_cfg["x_max"].as<double>(25.0);
                roi_y_min_ = roi_cfg["y_min"].as<double>(-3.6);
                roi_y_max_ = roi_cfg["y_max"].as<double>(3.6);
                roi_z_min_ = roi_cfg["z_min"].as<double>(-1.0);
                roi_z_max_ = roi_cfg["z_max"].as<double>(1.0);
                LOG(INFO) << " OfflineEvaluator: ROI过滤: x=[" << roi_x_min_ << "," << roi_x_max_
                          << "], y=[" << roi_y_min_ << "," << roi_y_max_
                          << "], z=[" << roi_z_min_ << "," << roi_z_max_ << "]";
            } else {
                LOG(WARNING) << " OfflineEvaluator: 未找到trajectory.point_cloud_roi配置，使用默认ROI";
                roi_x_min_ = 5.0; roi_x_max_ = 25.0;
                roi_y_min_ = -3.6; roi_y_max_ = 3.6;
                roi_z_min_ = -1.0; roi_z_max_ = 1.0;
            }

            // 评测阈值
            if (traj_cfg["eval_thresholds"]) {
                auto th = traj_cfg["eval_thresholds"];
                trajectory_z_tolerance_m_ = th["z_tolerance_m"].as<double>(0.01);
                trajectory_height_errors_reserve_capacity_ = th["height_errors_reserve_capacity"].as<size_t>(10000000);
            } else {
                trajectory_z_tolerance_m_ = 0.01;
                trajectory_height_errors_reserve_capacity_ = 10000000;
            }

            // 目标物评测参数（减速带/井盖/大桥接缝共用；成员默认值已在 .hpp 中声明，此处覆盖）
            if (traj_cfg["target_object"]) {
                auto sb = traj_cfg["target_object"];
                target_eval_x_min_ = sb["eval_x_min"].as<double>(target_eval_x_min_);
                target_eval_x_max_ = sb["eval_x_max"].as<double>(target_eval_x_max_);
                eval_x_ideal_          = sb["eval_x_ideal"].as<double>(eval_x_ideal_);
                same_target_dx_max_    = sb["same_target_dx_max"].as<double>(same_target_dx_max_);
                TARGET_MIN_POINTS      = sb["min_points"].as<size_t>(TARGET_MIN_POINTS);
                // focus_labels：从 YAML 列表加载，缺省 {16, 17, 23}
                if (sb["focus_labels"] && sb["focus_labels"].IsSequence()) {
                    focus_target_labels_.clear();
                    for (const auto& n : sb["focus_labels"]) focus_target_labels_.insert(n.as<int>());
                }
                TARGET_MATCH_THRESHOLD = sb["match_threshold"].as<double>(TARGET_MATCH_THRESHOLD);
                TARGET_ERRORS_RESERVE  = sb["errors_reserve"].as<size_t>(TARGET_ERRORS_RESERVE);
                TARGET_X_ERR_LARGE = sb["x_error_large"].as<double>(TARGET_X_ERR_LARGE);
                TARGET_Z_ERR_LARGE = sb["z_error_large"].as<double>(TARGET_Z_ERR_LARGE);
                TARGET_W_ERR_LARGE = sb["w_error_large"].as<double>(TARGET_W_ERR_LARGE);
                TARGET_X_ERR_MID   = sb["x_error_mid"].as<double>(TARGET_X_ERR_MID);
                TARGET_Z_ERR_MID   = sb["z_error_mid"].as<double>(TARGET_Z_ERR_MID);
            }

            LOG(INFO) << " OfflineEvaluator: 评测阈值: z_tol=" << trajectory_z_tolerance_m_ << "m";
            {
                std::string labels_info;
                for (int l : focus_target_labels_) labels_info += std::to_string(l) + " ";
                LOG(INFO) << " OfflineEvaluator: 目标物评测: 锁帧窗口=[" << target_eval_x_min_ << "m, " << target_eval_x_max_ << "m]"
                          << ", ideal=" << eval_x_ideal_ << "m"
                          << ", 跨帧关联Δx<" << same_target_dx_max_ << "m"
                          << ", 匹配=" << TARGET_MATCH_THRESHOLD << "m"
                          << ", focus_labels={" << labels_info << "}"
                          << ", 大误差(x/z/w)=" << TARGET_X_ERR_LARGE*100 << "/" << TARGET_Z_ERR_LARGE*100 << "/" << TARGET_W_ERR_LARGE*100 << "cm"
                          << ", 中误差(x/z)=" << TARGET_X_ERR_MID*100 << "/" << TARGET_Z_ERR_MID*100 << "cm";
            }
            LOG(INFO) << " OfflineEvaluator: 轨迹高度误差容器预分配: " << trajectory_height_errors_reserve_capacity_
                      << " 点 (约 " << (trajectory_height_errors_reserve_capacity_ * sizeof(double) / 1024.0 / 1024.0) << " MB)";
        } else {
            LOG(FATAL) << " OfflineEvaluator: 未找到trajectory配置，无法继续";
            return false;
        }

        // per-label 累加器预分配（focus_target_labels_ 每 label 建一个 stats 项 + 误差容器）
        for (int l : focus_target_labels_) {
            auto& s = focus_target_stats_[l];
            s.x_peak_errors.reserve(TARGET_ERRORS_RESERVE);
            s.x_leading_errors.reserve(TARGET_ERRORS_RESERVE);
            s.z_peak_errors.reserve(TARGET_ERRORS_RESERVE);
            s.z_mean_errors.reserve(TARGET_ERRORS_RESERVE);
            s.width_errors.reserve(TARGET_ERRORS_RESERVE);
        }

        // 初始化轨迹表面拟合器参数（从YAML加载，与后处理节点保持一致）
        YAML::Node tsf;
        if (config["trajectory"] && config["trajectory"]["surface_fitter"]) {
            tsf = config["trajectory"]["surface_fitter"];
        }
        if (tsf) {
            if (tsf["ref_min_x"])               surface_fitter_options_.min_x = tsf["ref_min_x"].as<double>();
            if (tsf["ref_max_x"])               surface_fitter_options_.max_x = tsf["ref_max_x"].as<double>();
            if (tsf["ref_angle_alpha"])         surface_fitter_options_.ref_angle_alpha = tsf["ref_angle_alpha"].as<double>();
            if (tsf["ref_z0_alpha"])            surface_fitter_options_.ref_z0_alpha = tsf["ref_z0_alpha"].as<double>();
            if (tsf["use_distance_weight"])     surface_fitter_options_.use_distance_weight = tsf["use_distance_weight"].as<bool>();
            if (tsf["weight_transition_start"]) surface_fitter_options_.weight_transition_start = tsf["weight_transition_start"].as<double>();
            if (tsf["weight_transition_end"])   surface_fitter_options_.weight_transition_end = tsf["weight_transition_end"].as<double>();
            if (tsf["weight_min"])              surface_fitter_options_.weight_min = tsf["weight_min"].as<double>();

            if (tsf["trunk_threshold"])         surface_fitter_options_.trunk_threshold = tsf["trunk_threshold"].as<double>();
            if (tsf["trunk_near_min"])          surface_fitter_options_.trunk_near_min = tsf["trunk_near_min"].as<double>();
            if (tsf["trunk_near_max"])          surface_fitter_options_.trunk_near_max = tsf["trunk_near_max"].as<double>();
            if (tsf["trunk_near_weight"])       surface_fitter_options_.trunk_near_weight = tsf["trunk_near_weight"].as<double>();
            if (tsf["trunk_near_constraint"])   surface_fitter_options_.trunk_near_constraint = tsf["trunk_near_constraint"].as<double>();
            if (tsf["trunk_max_iter"])          surface_fitter_options_.trunk_max_iter = tsf["trunk_max_iter"].as<size_t>();
            if (tsf["trunk_min_points"])        surface_fitter_options_.trunk_min_points = tsf["trunk_min_points"].as<size_t>();
            if (tsf["trunk_converge_eps"])      surface_fitter_options_.trunk_converge_eps = tsf["trunk_converge_eps"].as<double>();

            LOG(INFO) << " OfflineEvaluator: TrajectorySurfaceFitter configured"
                      << " (ref_range=[" << surface_fitter_options_.min_x << "," << surface_fitter_options_.max_x << "]m"
                      << ", trunk_threshold=" << surface_fitter_options_.trunk_threshold << ")";
        } else {
            LOG(INFO) << " OfflineEvaluator: trajectory.surface_fitter配置未找到，使用默认值";
        }

    } catch (const YAML::Exception& e) {
        LOG(ERROR) << " OfflineEvaluator: 配置文件加载失败: " << e.what();
        return false;
    }

    // 初始化路面类别IoU评测器（24类）
    std::vector<int> class_indices = {1,2,3,4,5,6,7,8,9,10,11,12,13,14,16,17,18,19,20,21,22,23,24,25};
    std::vector<int> drivable_classes = {1,2,3,4,5,6,7,8,9,10,11,12,13,14,16,17,18,19,20,21,22,23};
    std::vector<std::string> class_names = {"asphalt", "cement", "epoxy_floor", "brick_road","gravel", "cobbles", "grass",
        "dirt", "hard_sand","soft_sand", "water", "ice", "snow", "other_surface","bumper", "manhole_cover",
        "steel_plate", "drain","road_beam", "railway", "step", "other_bump", "moving_obstacle", "fixed_obstacle"};

    road_surface_iou_evaluator_ = std::make_unique<Iou>(
        /*num_classes=*/ int(class_names.size()),
        /*class_indices=*/ class_indices,
        /*empty_label=*/ 26,
        /*label_str=*/      class_names,
        /*name=*/ "Road Surface Labels",
        /*drivable_surface_labels=*/ drivable_classes,
        /*H=*/ gt_NX_, /*W=*/ gt_NY_, /*D=*/ gt_NZ_);

    // 初始化轨迹类别IoU评测器（复用相同的类别配置）
    trajectory_iou_evaluator_ = std::make_unique<Iou>(
        /*num_classes=*/ int(class_names.size()),
        /*class_indices=*/ class_indices,
        /*empty_label=*/ 26,
        /*label_str=*/      class_names,
        /*name=*/ "Trajectory Labels",
        /*drivable_surface_labels=*/ drivable_classes,
        /*H=*/ gt_NX_, /*W=*/ gt_NY_, /*D=*/ gt_NZ_);

    // 初始化视觉分类mAP计算器（动态扩展模式，16个任务）
    LOG(INFO) << " OfflineEvaluator: 初始化视觉分类mAP计算器（动态扩展模式）";
    LOG(INFO) << "   传入类别数量配置: " << vision_class_counts_.size() << " 个视觉头";
    vision_map_calculator_ = std::make_unique<GlobalMAPCalculator>(1000, vision_class_counts_, vision_label_offsets_);
    LOG(INFO) << " ✓ GlobalMAPCalculator 初始化完成";

    received_samples_ = 0;
    evaluation_samples_ = 0;
    completed_samples_ = 0;
    skipped_no_gt_samples_ = 0;
    skipped_all_tasks_samples_ = 0;
    road_task_stats_ = {};
    traj_task_stats_ = {};
    vision_task_stats_ = {};
    max_dense_cloud_points_ = 0;
    max_filtered_indices_size_ = 0;
    max_neighbor_z_size_ = 0;

    // 预分配固定内存空间
    filtered_indices_.reserve(filtered_indices_reserve_capacity_);
    trajectory_height_errors_.reserve(trajectory_height_errors_reserve_capacity_);

    // 初始化评测临时容器
    const int grid_size = gt_NX_ * gt_NY_;
    pred_labels_.resize(grid_size, 26);  // 26=empty
    gt_labels_.resize(grid_size, 26);
    pred_z_.resize(grid_size, 0);
    gt_z_.resize(grid_size, 0);

    // 初始化column_data_
    column_data_.resize(gt_NX_);
    for (int x = 0; x < gt_NX_; ++x) {
        column_data_[x].resize(gt_NY_);
    }

    // 初始化真值场景列表（扫描 labeled_data_root_）
    if (!initializeScenes()) {
        LOG(ERROR) << " OfflineEvaluator: Failed to initialize scenes";
        return false;
    }
    // 初始化预测场景列表（扫描 pred_data_root_）
    if (!initializePredScenes()) {
        LOG(ERROR) << " OfflineEvaluator: Failed to initialize pred scenes";
        return false;
    }

    // 配置真值轨迹后处理器参数（与预测保持一致）
    traj_postprocess_manager_.setOptions(surface_fitter_options_);


    LOG(INFO) << " OfflineEvaluator: 初始化完成";
    return true;
}

// ============================================================================
// 离线评测主驱动：双层循环（场景 × 帧），从预测目录读取数据后调用 performEvaluation
// ============================================================================
bool OfflineEvaluator::runAll()
{
    if (pred_scenes_.empty()) {
        LOG(ERROR) << " OfflineEvaluator: 预测场景为空，无法评测";
        return false;
    }

    for (size_t s = 0; s < pred_scenes_.size(); ++s) {
        if (max_scenes_for_debug_ > 0 && s >= max_scenes_for_debug_) break;
        // 场景切换前 flush 上一场景的目标物累积（首场景 s=0 时 flush 无副作用：状态为空）
        if (eval_traj_) flushTargetStatsForScene();
        current_scene_idx_ = s;
        traj_postprocess_manager_.resetSceneState();

        const auto& pred_scene = pred_scenes_[s];

        // 校验场景名一致性：预测场景与真值场景需同名对齐
        if (s >= scenes_.size() || pred_scene.scene_name != scenes_[s].scene_name) {
            LOG(WARNING) << " 场景名不一致，跳过预测场景: " << pred_scene.scene_name
                         << " (对应真值场景索引 " << s << " 不匹配"
                         << (s < scenes_.size() ? (": 真值=" + scenes_[s].scene_name) : ": 真值场景不足") << ")";
            continue;
        }

        LOG(INFO) << "\n========== 场景 " << s << "/" << pred_scenes_.size()
                  << ": " << pred_scene.scene_name
                  << " (" << pred_scene.frames.size() << " 帧) ==========";

        for (const auto& [pred_ts_ms, pred_frame_dir] : pred_scene.frames) {
            cur_pred_road_ = RoadSurface();
            cur_pred_trajectory_ = WheelTrajectory();
            cur_pred_vision_ = AuxiliaryOutputs();
            trajectory_total_pts_ = 0;

            bool pred_road_ok = false;
            bool pred_traj_ok = false;
            bool pred_vision_ok = false;

            // 按固定顺序独立加载预测。单个任务的预测缺失不阻断其他任务。
            if (eval_road_) {
                pred_road_ok = loadPredRoadSurface(pred_frame_dir, pred_ts_ms, cur_pred_road_);
                auto& stats = road_task_stats_;
                if (pred_road_ok) {
                    stats.prediction_available++;
                } else {
                    stats.skipped_prediction_missing++;
                    LOG(WARNING) << "  路面预测不可用: " << pred_ts_ms << "ms";
                }
            }
            if (eval_traj_) {
                pred_traj_ok = loadPredTrajectory(pred_frame_dir, pred_ts_ms, cur_pred_trajectory_,
                                                   trajectory_total_pts_);
                auto& stats = traj_task_stats_;
                if (pred_traj_ok) {
                    stats.prediction_available++;
                } else {
                    stats.skipped_prediction_missing++;
                    LOG(WARNING) << "  轨迹预测不可用: " << pred_ts_ms << "ms";
                }
            }
            if (eval_vision_) {
                pred_vision_ok = loadPredVision(pred_frame_dir, pred_ts_ms, cur_pred_vision_);
                auto& stats = vision_task_stats_;
                if (pred_vision_ok) {
                    stats.prediction_available++;
                } else {
                    stats.skipped_prediction_missing++;
                    LOG(WARNING) << "  视觉预测不可用: " << pred_ts_ms << "ms";
                }
            }

            if (!pred_road_ok && !pred_traj_ok && !pred_vision_ok) {
                LOG(WARNING) << "  跳过帧 " << pred_ts_ms << "ms：所有启用任务的预测均不可用";
                continue;
            }

            // 将 ms 时间戳拆为 sec/nsec（兼容旧代码 getCurrentTimestampMs）
            frame_start_timestamp_sec_  = pred_ts_ms / 1000ULL;
            frame_start_timestamp_nsec_ = (pred_ts_ms % 1000ULL) * 1'000'000ULL;

            received_samples_++;
            performEvaluation(pred_road_ok, pred_traj_ok, pred_vision_ok);
        }
    }
    // 收尾：flush 最后一个场景的目标物累积
    if (eval_traj_) flushTargetStatsForScene();
    all_scenes_completed_ = true;
    return true;
}

// 输出最终统计 + 退出
void OfflineEvaluator::writeFinalStats()
{
    outputFinalStatistics();
}

// ===========================================================================================
// 主评测流程
// ===========================================================================================
void OfflineEvaluator::performEvaluation(bool pred_road_ok, bool pred_traj_ok, bool pred_vision_ok)
{
    try {
        const uint64_t pred_timestamp_ms = getCurrentTimestampMs();
        evaluation_samples_++;

        LOG(INFO) << "\n========== [OfflineEvaluator] 开始评测 ==========";
        LOG(INFO) << " 评测样本索引: #" << evaluation_samples_
                  << " 时间戳: " << pred_timestamp_ms << " ms";

        const auto eval_start = std::chrono::high_resolution_clock::now();

        // 先定位一次真值时间戳，再让各任务独立决定是否加载成功、是否参与评测。
        std::vector<int64_t> gt_occ_data;
        DensePointCloud dense_cloud;
        std::map<std::string, int> gt_vision_data;
        const GroundTruthLoadStatus gt_status = loadGroundTruthByTimestamp(
            pred_timestamp_ms, gt_occ_data, dense_cloud, gt_vision_data);
        current_gt_sample_idx_ = gt_status.timestamp_matched ? gt_status.sample_idx : SIZE_MAX;

        if (!gt_status.timestamp_matched) {
            skipped_no_gt_samples_++;
            if (eval_road_ && pred_road_ok) road_task_stats_.skipped_ground_truth_missing++;
            if (eval_traj_ && pred_traj_ok) traj_task_stats_.skipped_ground_truth_missing++;
            if (eval_vision_ && pred_vision_ok) vision_task_stats_.skipped_ground_truth_missing++;
            LOG(WARNING) << " 帧任务结果: 路面=" << (eval_road_ && pred_road_ok ? "GT未匹配" : "未参与")
                         << ", 轨迹=" << (eval_traj_ && pred_traj_ok ? "GT未匹配" : "未参与")
                         << ", 视觉=" << (eval_vision_ && pred_vision_ok ? "GT未匹配" : "未参与");
            return;
        }

        if (eval_road_ && gt_status.road_ready) road_task_stats_.ground_truth_available++;
        if (eval_traj_ && gt_status.traj_ready) traj_task_stats_.ground_truth_available++;
        if (eval_vision_ && gt_status.vision_ready) vision_task_stats_.ground_truth_available++;

        bool road_evaluated = false;
        bool traj_evaluated = false;
        bool vision_evaluated = false;

        // 任务1：路面
        if (eval_road_) {
            if (pred_road_ok && gt_status.road_ready) {
                LOG(INFO) << "\n----- 任务1：路面评测 -----";
                const std::vector<OccPoint> pred_road_surface = convertRoadSurfaceToOccIndices(&cur_pred_road_);
                const std::vector<OccPoint> gt_road_surface = extractSingleLayerFromGroundTruth(gt_occ_data);
                evaluateRoadSurface(pred_road_surface, gt_road_surface);
                road_task_stats_.evaluated++;
                road_evaluated = true;
            } else if (pred_road_ok) {
                road_task_stats_.skipped_ground_truth_missing++;
            }
        }

        // 任务2 + 任务4：轨迹与目标物
        if (eval_traj_) {
            if (pred_traj_ok && gt_status.traj_ready) {
                LOG(INFO) << "\n----- 任务2：轨迹评测 -----";
                cur_pred_trajectory_.pts_num = trajectory_total_pts_;
                WheelTrajectory gt_trajectory = extractTrajectoryGroundTruthPhysical(cur_pred_trajectory_, dense_cloud);
                traj_postprocess_manager_.applyPca(gt_trajectory);
                evaluateTrajectory(cur_pred_trajectory_, gt_trajectory);
                evaluateTargets(cur_pred_trajectory_, gt_trajectory);
                traj_task_stats_.evaluated++;
                traj_evaluated = true;
            } else if (pred_traj_ok) {
                traj_task_stats_.skipped_ground_truth_missing++;
            }
        }

        // 任务3：视觉分类
        if (eval_vision_) {
            if (pred_vision_ok && gt_status.vision_ready) {
                LOG(INFO) << "\n----- 任务3：视觉分类评测 -----";
                evaluateVisionClassification(cur_pred_vision_, gt_vision_data);
                vision_task_stats_.evaluated++;
                vision_evaluated = true;
            } else if (pred_vision_ok) {
                vision_task_stats_.skipped_ground_truth_missing++;
            }
        }

        if (!road_evaluated && !traj_evaluated && !vision_evaluated) {
            skipped_all_tasks_samples_++;
            LOG(WARNING) << " 帧任务结果: 路面=" << (eval_road_ ? "跳过" : "未启用")
                         << ", 轨迹=" << (eval_traj_ ? "跳过" : "未启用")
                         << ", 视觉=" << (eval_vision_ ? "跳过" : "未启用");
            return;
        }

        completed_samples_++;
        if (current_scene_idx_ < scenes_.size()) {
            scenes_[current_scene_idx_].completed_samples++;
        }

        const auto eval_end = std::chrono::high_resolution_clock::now();
        const double eval_duration = std::chrono::duration<double, std::milli>(eval_end - eval_start).count();
        evaluation_times_.push_back(eval_duration);

        LOG(INFO) << " 帧任务结果: 路面=" << (road_evaluated ? "完成" : (eval_road_ ? "跳过" : "未启用"))
                  << ", 轨迹=" << (traj_evaluated ? "完成" : (eval_traj_ ? "跳过" : "未启用"))
                  << ", 视觉=" << (vision_evaluated ? "完成" : (eval_vision_ ? "跳过" : "未启用"));
        LOG(INFO) << "========== 评测完成，耗时: " << std::fixed << std::setprecision(2)
                  << eval_duration << " ms ==========\n";

        if (evaluation_samples_ % 100 == 0) {
            std::cout << "OfflineEvaluator: 已处理 " << evaluation_samples_ << " 个候选样本 "
                      << "(至少完成一项: " << completed_samples_
                      << ", 未匹配真值: " << skipped_no_gt_samples_
                      << ", 无可评任务: " << skipped_all_tasks_samples_ << ")" << std::endl;
        }

    } catch (const std::exception& e) {
        LOG(ERROR) << " OfflineEvaluator: Exception in performEvaluation: " << e.what();
    }
}
