#include "evaluator.hpp"
#include "compat/logging_compat.hpp"
#include <iomanip>
#include <cmath>
#include <numeric>
#include <algorithm>
#include <set>

// ===========================================================================================
// 任务2：轨迹评测（3+1指标，直接基于物理坐标）
// ===========================================================================================

// 从稠密点云提取轨迹真值（基于邻域统计：众数label、平均值z）
// 输入：预测轨迹物理坐标（提供xy查询位置）、稠密点云（5维原始数据）
// 输出：真值轨迹物理坐标（z和label已统计）
WheelTrajectory OfflineEvaluator::extractTrajectoryGroundTruthPhysical(const WheelTrajectory& traj_pred_physical, const DensePointCloud& dense_cloud)
{
    WheelTrajectory traj_gt_physical;

    try {
        // 注意：输入traj_pred_physical已是后段轨迹（索引从0开始）
        const size_t num_back_pts = static_cast<size_t>(traj_pred_physical.pts_num);

        if (num_back_pts == 0) {
            LOG(WARNING) << "  后段轨迹点数为0，返回默认真值";
            return traj_gt_physical;
        }

        // 设置pts_num为后段点数
        traj_gt_physical.pts_num = static_cast<int32_t>(num_back_pts);
        // ===== 1. ROI预过滤（减少无关点）=====
        filtered_indices_.clear();

        if (filtered_indices_.capacity() < dense_cloud.num_points * 0.9) {
            filtered_indices_.reserve(std::max(dense_cloud.num_points, filtered_indices_reserve_capacity_));
        }

        // 直接访问原始数据
        const float* raw = dense_cloud.raw_data.data();
        for (size_t i = 0; i < dense_cloud.num_points; ++i) {
            const float x = raw[i * 5 + 0];
            const float y = raw[i * 5 + 1];
            const float z = raw[i * 5 + 2];

            if (x >= roi_x_min_ && x <= roi_x_max_ &&
                y >= roi_y_min_ && y <= roi_y_max_ &&
                z >= roi_z_min_ && z <= roi_z_max_) {
                filtered_indices_.push_back(i);
            }
        }

        // 统计过滤后的最大点数
        if (filtered_indices_.size() > max_filtered_indices_size_) {
            max_filtered_indices_size_ = filtered_indices_.size();
        }

        if (filtered_indices_.empty()) {
            LOG(WARNING) << "  稠密点云ROI过滤后为空，返回默认真值";
            return traj_gt_physical;
        }

        LOG(INFO) << "  稠密点云ROI过滤: " << dense_cloud.num_points << " → " << filtered_indices_.size()
                  << " (" << std::fixed << std::setprecision(1)
                  << (100.0 * filtered_indices_.size() / dense_cloud.num_points) << "%)";

        // ===== 2. 构建哈希网格索引（加速邻域搜索）=====
        grid_hash_.clear();

        // 哈希函数：hash = gy*1000 + gx（ROI内gx<250, gy<72）
        auto hash_xy = [this](float x, float y) -> int64_t {
            int gx = static_cast<int>(std::floor(x / dense_cloud_search_radius_));
            int gy = static_cast<int>(std::floor(y / dense_cloud_search_radius_));
            return static_cast<int64_t>(gy) * 1000LL + static_cast<int64_t>(gx);
        };

        for (size_t idx : filtered_indices_) {
            grid_hash_[hash_xy(dense_cloud.x(idx), dense_cloud.y(idx))].push_back(idx);
        }

        // ===== 3. 遍历四轮轨迹提取真值 =====
        std::vector<WheelPoint>* gt_tracks[4] = {
            &traj_gt_physical.frontLeftTrajectory,
            &traj_gt_physical.frontRightTrajectory,
            &traj_gt_physical.rearLeftTrajectory,
            &traj_gt_physical.rearRightTrajectory
        };

        const std::vector<WheelPoint>* pred_tracks[4] = {
            &traj_pred_physical.frontLeftTrajectory,
            &traj_pred_physical.frontRightTrajectory,
            &traj_pred_physical.rearLeftTrajectory,
            &traj_pred_physical.rearRightTrajectory
        };

        // 使用成员变量（复用，避免每次创建）
        neighbor_z_.reserve(neighbor_reserve_capacity_);
        neighbor_labels_.reserve(neighbor_reserve_capacity_);

        // 提取后段轨迹点的真值（索引已对齐，从0开始）
        for (int w = 0; w < 4; ++w) {
            gt_tracks[w]->reserve(num_back_pts);
            const auto& pred_track = *pred_tracks[w];

            for (size_t i = 0; i < num_back_pts && i < pred_track.size(); ++i) {
                const auto& pred_pt = pred_track[i];

                WheelPoint gt_pt;
                gt_pt.x = pred_pt.x;
                gt_pt.y = pred_pt.y;
                gt_pt.z = -1.0f;  // 默认无效
                gt_pt.clz = 26.0f;  // 默认empty

                // 跳过无效预测点
                if (pred_pt.clz >= 26.0f || pred_pt.z == -1.0f) {
                    gt_tracks[w]->push_back(gt_pt);
                    continue;
                }

                // ===== 4. 邻域搜索（9宫格查询+圆形过滤）=====
                neighbor_z_.clear();
                neighbor_labels_.clear();

                const int cx = static_cast<int>(std::floor(pred_pt.x / dense_cloud_search_radius_));
                const int cy = static_cast<int>(std::floor(pred_pt.y / dense_cloud_search_radius_));

                for (int offset_y = -1; offset_y <= 1; ++offset_y) {
                    for (int offset_x = -1; offset_x <= 1; ++offset_x) {
                        const int64_t hash = static_cast<int64_t>(cy + offset_y) * 1000LL + static_cast<int64_t>(cx + offset_x);
                        auto it = grid_hash_.find(hash);
                        if (it == grid_hash_.end()) continue;

                        for (size_t idx : it->second) {
                            const float pt_x = dense_cloud.x(idx);
                            const float pt_y = dense_cloud.y(idx);
                            const float delta_x = pt_x - pred_pt.x;
                            const float delta_y = pt_y - pred_pt.y;
                            const float dist_sq = delta_x * delta_x + delta_y * delta_y;

                            if (dist_sq <= dense_cloud_search_radius_sq_) {
                                neighbor_z_.push_back(dense_cloud.z(idx));
                                neighbor_labels_.push_back(dense_cloud.label(idx));
                            }
                        }
                    }
                }

                // ===== 5. 统计真值（先确定众数label，再根据类别特性选择z值策略）=====
                // 统计邻域z值最大数量（用于调整neighbor_reserve_capacity）
                if (neighbor_z_.size() > max_neighbor_z_size_) {
                    max_neighbor_z_size_ = neighbor_z_.size();
                }

                if (neighbor_z_.size() >= dense_cloud_min_points_) {
                    // 1. 先确定众数label（多数投票，复用哈希表）
                    label_count_.clear();
                    for (auto label : neighbor_labels_) {
                        label_count_[label]++;
                    }

                    int64_t mode_label = 26;
                    int max_count = 0;
                    for (const auto& [label, count] : label_count_) {
                        if (count > max_count) {
                            max_count = count;
                            mode_label = label;
                        }
                    }
                    gt_pt.clz = (mode_label == 0) ? 26.0f  // label=0 视为无效（同 empty）
                               : static_cast<float>(mode_label);

                    // 2. 根据类别特性选择z值提取策略
                    // 障碍物类（24=moving_obstacle, 25=fixed_obstacle）：取最高的5个点的均值
                    // 非障碍物类（路面、凸起等）：取平均值
                    if (mode_label == 24 || mode_label == 25) {
                        // 障碍物类：取最高的5个点的均值（反映物体顶部高度）
                        constexpr size_t kTopPoints = 5;
                        const size_t num_top = std::min(kTopPoints, neighbor_z_.size());

                        // 部分排序：找到最大的num_top个元素
                        std::partial_sort(neighbor_z_.begin(), neighbor_z_.begin() + num_top, neighbor_z_.end(),
                                        std::greater<float>());

                        // 计算最高num_top个点的均值
                        float sum_top_z = 0.0f;
                        for (size_t k = 0; k < num_top; ++k) {
                            sum_top_z += neighbor_z_[k];
                        }
                        gt_pt.z = sum_top_z / num_top;
                    } else {
                        // 非障碍物类：取平均值（更平滑，减少波动）
                        const float sum_z = std::accumulate(neighbor_z_.begin(), neighbor_z_.end(), 0.0f);
                        gt_pt.z = sum_z / neighbor_z_.size();
                    }
                }

                gt_tracks[w]->push_back(gt_pt);
            }
        }

        LOG(INFO) << "  ✓ 轨迹真值提取: 四轮 × " << num_back_pts << " 点（后段轨迹）";

    } catch (const std::exception& e) {
        LOG(ERROR) << "  extractTrajectoryGroundTruthPhysical异常: " << e.what();
    }

    return traj_gt_physical;
}

void OfflineEvaluator::evaluateTrajectory(const WheelTrajectory& traj_pred_physical, const WheelTrajectory& traj_gt_physical)
{
    try {
        // 注意：输入traj_pred_physical和traj_gt_physical已是后段轨迹（索引从0开始）
        const size_t num_back_pts = static_cast<size_t>(traj_pred_physical.pts_num);

        if (num_back_pts == 0) {
            LOG(WARNING) << "  后段轨迹点数为0，跳过评测";
            return;
        }

        const size_t num_points = num_back_pts * 4;  // 四轮

        // 收集预测和真值的物理坐标（后段）
        const std::vector<WheelPoint>* pred_tracks[4] = {
            &traj_pred_physical.frontLeftTrajectory,
            &traj_pred_physical.frontRightTrajectory,
            &traj_pred_physical.rearLeftTrajectory,
            &traj_pred_physical.rearRightTrajectory
        };

        const std::vector<WheelPoint>* gt_tracks[4] = {
            &traj_gt_physical.frontLeftTrajectory,
            &traj_gt_physical.frontRightTrajectory,
            &traj_gt_physical.rearLeftTrajectory,
            &traj_gt_physical.rearRightTrajectory
        };

        // 评测阈值
        const double Z_TOL_M = trajectory_z_tolerance_m_;

        // 指标累积
        double z_mae_sum = 0.0;                  // 指标1：Z_MAE累积
        size_t acc_1cm_count = 0;                // 指标2：Acc_1cm计数
        size_t pass_combined_count = 0;          // 指标4：Pass_Rate_combined计数

        // 准备轨迹类别标签数据（用于IoU评测）
        std::vector<int> traj_pred_labels;
        std::vector<int> traj_gt_labels;
        traj_pred_labels.reserve(num_points);
        traj_gt_labels.reserve(num_points);

        // 当前帧时间戳和场景名（当前仅 focus_class_metrics_ 块使用，见下方 #if 0）
        [[maybe_unused]] const uint64_t current_ts_ms = getCurrentTimestampMs();
        [[maybe_unused]] const std::string& current_scene_name = scenes_[current_scene_idx_].scene_name;

        // 遍历四轮后段轨迹点（索引已对齐，从0开始）
        for (int w = 0; w < 4; ++w) {
            const auto& pred_track = *pred_tracks[w];
            const auto& gt_track = *gt_tracks[w];

            // 理论上 pred_track.size() == gt_track.size() == num_back_pts，但保留边界检查以防万一
            const size_t min_size = std::min({num_back_pts, pred_track.size(), gt_track.size()});
            for (size_t i = 0; i < min_size; ++i) {
                const auto& pred_pt = pred_track[i];
                const auto& gt_pt = gt_track[i];

                // 跳过invalid点
                if (pred_pt.clz >= 26.0f || gt_pt.clz >= 26.0f) {
                    continue;
                }

                // 指标1：累积高度误差（单位：m）
                const double z_diff = std::abs(pred_pt.z - gt_pt.z);
                z_mae_sum += z_diff;
                trajectory_height_errors_.push_back(z_diff);


                // 调试：统计z值范围
                min_pred_z_ = std::min(min_pred_z_, static_cast<double>(pred_pt.z));
                max_pred_z_ = std::max(max_pred_z_, static_cast<double>(pred_pt.z));
                min_gt_z_ = std::min(min_gt_z_, static_cast<double>(gt_pt.z));
                max_gt_z_ = std::max(max_gt_z_, static_cast<double>(gt_pt.z));
                max_z_diff_ = std::max(max_z_diff_, z_diff);

                // 指标2：高度合格判定（1cm）
                const bool z_pass = (z_diff <= Z_TOL_M);
                if (z_pass) acc_1cm_count++;

                // 指标3：收集类别标签（用于IoU评测）
                const int pred_label = static_cast<int>(pred_pt.clz);
                const int gt_label = static_cast<int>(gt_pt.clz);
                traj_pred_labels.push_back(pred_label);
                traj_gt_labels.push_back(gt_label);

                // 指标4：综合通过判定（高度+标签）
                const bool label_match = (pred_label == gt_label);
                if (z_pass && label_match) {
                    pass_combined_count++;
                }

                // 逐点关注类别统计（沥青=1 / 水泥=2）—— 暂不输出，累加代码保留备用。
                // 恢复展示时将此处及 eval_statistics.cpp 中对应 #if 0 改为 #if 1 即可。
#if 0
                if (getFocusClasses().count(gt_label)) {
                    auto& m = focus_class_metrics_[gt_label];
                    m.total++;
                    if (z_pass) m.height_pass++;
                    if (z_pass && label_match) m.combined_pass++;

                    if (label_match) {
                        m.z_error_sum += z_diff;
                        m.z_error_count++;
                        if (z_diff > m.max_z_error) {
                            m.max_z_error = z_diff;
                            m.max_error_scene = current_scene_name;
                            m.max_error_scene_idx = current_scene_idx_;
                            m.max_error_wheel = w;
                            m.max_error_point_idx = i;
                            const auto ts_it = scenes_[current_scene_idx_].timestamp_to_sample_idx.find(current_ts_ms);
                            m.max_error_frame_idx = (ts_it != scenes_[current_scene_idx_].timestamp_to_sample_idx.end()) ? ts_it->second : SIZE_MAX;
                        }
                    }
                }
#endif
            }
        }

        // 计算有效点数
        const size_t valid_pts = traj_pred_labels.size();

        if (valid_pts == 0) {
            LOG(WARNING) << "  有效轨迹点数为0，跳过评测";
            return;
        }

        // 计算指标1、2、4
        const double z_mae = z_mae_sum / valid_pts;
        const double acc_1cm = static_cast<double>(acc_1cm_count) / valid_pts;
        const double pass_rate = static_cast<double>(pass_combined_count) / valid_pts;

        // 累积统计
        trajectory_height_total_count_ += valid_pts;
        trajectory_height_correct_count_ += acc_1cm_count;
        trajectory_pass_combined_count_ += pass_combined_count;

        // 指标3：调用Iou类计算Label_mIoU（只考虑class_indices中的类别）
        if (trajectory_iou_evaluator_) {
            trajectory_iou_evaluator_->after_step_trajectory(traj_pred_labels, traj_gt_labels);
        }

        // 输出核心指标（3+1）
        LOG(INFO) << "  ✓ 轨迹评测完成:";
        LOG(INFO) << "    [1] Z_MAE: " << std::fixed << std::setprecision(4)
                  << z_mae * 100 << " cm";
        LOG(INFO) << "    [2] Acc_1cm: " << std::fixed << std::setprecision(2)
                  << acc_1cm * 100 << "% (" << acc_1cm_count << "/" << valid_pts << ")";
        LOG(INFO) << "    [3] Label_mIoU: 见最终统计（由Iou类计算）";
        LOG(INFO) << "    [4] Pass_Rate: " << std::fixed << std::setprecision(2)
                  << pass_rate * 100 << "% (" << pass_combined_count << "/" << valid_pts << ")";

        // 调试：输出z值范围（帮助判断是否有明显特征物）
        LOG(INFO) << "    [DEBUG] Pred z range: [" << std::fixed << std::setprecision(4)
                  << min_pred_z_*100 << ", " << max_pred_z_*100 << "] cm";
        LOG(INFO) << "    [DEBUG] GT z range: [" << min_gt_z_*100 << ", " << max_gt_z_*100 << "] cm";
        LOG(INFO) << "    [DEBUG] Max diff: " << max_z_diff_*100 << " cm";
        // 重置统计（为下一帧准备）
        min_pred_z_ = 1e9; max_pred_z_ = -1e9;
        min_gt_z_ = 1e9; max_gt_z_ = -1e9;
        max_z_diff_ = 0;

    } catch (const std::exception& e) {
        LOG(ERROR) << "  evaluateTrajectory异常: " << e.what();
    }
}

// ===========================================================================================
// 目标物评测（基于极值点的匹配评价；减速带/井盖/大桥接缝共用）
// ===========================================================================================

// 提取目标物（focus_target_labels_，如 {16 减速带 / 17 井盖 / 23 大桥接缝}）— 返回所有连续段候选
// 窗口过滤 + 跨帧追踪 + 代表帧化交由 trackAndEmitTargets() 处理
// 段边界：非 focus label 或 label 变化（同为 focus 但不同类）均触发 flush，防止跨 label 合并
std::vector<OfflineEvaluator::TargetObject> OfflineEvaluator::extractTargets(
    const WheelTrajectory& trajectory, int wheel_idx)
{
    std::vector<TargetObject> bumps;

    try {
        // 获取指定车轮的轨迹
        const std::vector<WheelPoint>* tracks[4] = {
            &trajectory.frontLeftTrajectory,
            &trajectory.frontRightTrajectory,
            &trajectory.rearLeftTrajectory,
            &trajectory.rearRightTrajectory
        };

        if (wheel_idx < 0 || wheel_idx >= 4 || tracks[wheel_idx]->empty()) {
            return bumps;
        }

        const auto& track = *tracks[wheel_idx];

        target_points_buffer_.clear();
        int current_seg_label = 0;   // 当前段的 label（0 表示未开始段）

        auto flush_segment = [&]() {
            if (target_points_buffer_.size() >= TARGET_MIN_POINTS) {
                bumps.push_back(createTarget(wheel_idx, current_seg_label));
            }
            target_points_buffer_.clear();
            current_seg_label = 0;
        };

        for (size_t i = 0; i < track.size(); ++i) {
            const int label = static_cast<int>(track[i].clz);
            const bool is_focus = (focus_target_labels_.count(label) > 0);

            if (!is_focus) {
                flush_segment();
                continue;
            }
            // 同为 focus 但换了 label（如 16→17 相邻）→ 先 flush 上一段，再开新段
            if (current_seg_label != 0 && label != current_seg_label) {
                flush_segment();
            }
            if (current_seg_label == 0) {
                current_seg_label = label;
            }
            target_points_buffer_.push_back(&track[i]);
        }
        flush_segment();   // 轨迹末尾兜底

    } catch (const std::exception& e) {
        LOG(ERROR) << "  extractTargets异常: " << e.what();
    }

    return bumps;
}

// 创建目标物对象：从 target_points_buffer_ 提取极值点
// 构建 TargetObject：从 target_points_buffer_ 提取极值点（max z）、前缘点 x、均值 z、宽度
OfflineEvaluator::TargetObject OfflineEvaluator::createTarget(int wheel_idx, int label)
{
    TargetObject bump;
    bump.wheel_idx = wheel_idx;
    bump.label = label;

    if (target_points_buffer_.size() > max_target_points_size_) {
        max_target_points_size_ = target_points_buffer_.size();
    }

    // 极值点：段内 max z（所有 label 统一）
    auto extreme_it = std::max_element(target_points_buffer_.begin(), target_points_buffer_.end(),
        [](const WheelPoint* a, const WheelPoint* b) { return a->z < b->z; });
    bump.x_peak = (*extreme_it)->x;
    bump.z_peak = (*extreme_it)->z;

    // 前缘点 x = 段内 x 最小值（离车最近）
    auto leading_it = std::min_element(target_points_buffer_.begin(), target_points_buffer_.end(),
        [](const WheelPoint* a, const WheelPoint* b) { return a->x < b->x; });
    bump.x_leading = (*leading_it)->x;

    // 段内均值 z（label=17/23 用作高度指标）
    double sum_z = 0.0;
    for (const WheelPoint* p : target_points_buffer_) sum_z += p->z;
    bump.mean_z = static_cast<float>(sum_z / target_points_buffer_.size());

    // 宽度：相邻点欧氏距离累加（沿轨迹路径长度）
    bump.width = 0.0f;
    for (size_t j = 1; j < target_points_buffer_.size(); ++j) {
        const float dx = target_points_buffer_[j]->x - target_points_buffer_[j-1]->x;
        const float dy = target_points_buffer_[j]->y - target_points_buffer_[j-1]->y;
        bump.width += std::sqrt(dx*dx + dy*dy);
    }

    return bump;
}

// 每帧调用：pred 走跨帧锁帧；gt 只缓存当帧 bumps（按 frame_idx 索引）。
// 累加发生在场景切换/评测收尾的 flushTargetStatsForScene()。
void OfflineEvaluator::evaluateTargets(const WheelTrajectory& pred_trajectory,
                                          const WheelTrajectory& gt_trajectory)
{
    try {
        LOG(INFO) << "\n----- 任务4：目标物评测（减速带/井盖/大桥接缝；pred 跨帧锁定 + GT 同帧配对） -----";

        // 当前帧 sample_idx（从 scenes_[current_scene_idx_] 的时间戳映射反查）
        const auto& bump_scene = scenes_[current_scene_idx_];
        const uint64_t bump_ts_ms = getCurrentTimestampMs();
        const auto bump_ts_it = bump_scene.timestamp_to_sample_idx.find(bump_ts_ms);
        const size_t bump_frame_idx = (bump_ts_it != bump_scene.timestamp_to_sample_idx.end())
                                        ? bump_ts_it->second : SIZE_MAX;

        size_t frame_pred_cand = 0, frame_gt_cand = 0;
        for (int w = 0; w < 4; ++w) {
            const auto pred_bumps = extractTargets(pred_trajectory, w);
            auto gt_bumps   = extractTargets(gt_trajectory, w);
            frame_pred_cand += pred_bumps.size();
            frame_gt_cand   += gt_bumps.size();

            // pred: 跨帧追踪 + 首次 reference_x < ideal 锁定代表帧
            trackAndEmitTargets(active_pred_tracks_[w], scene_pred_reps_[w], pred_bumps, bump_frame_idx);

            // gt: 按帧缓存；flush 时用 pred 代表帧的 frame_idx 直接查找同帧 GT
            if (!gt_bumps.empty()) {
                scene_gt_by_frame_[w][bump_frame_idx] = std::move(gt_bumps);
            }
        }

        if (frame_pred_cand || frame_gt_cand) {
            LOG(INFO) << "  当前帧候选: pred=" << frame_pred_cand << ", gt=" << frame_gt_cand;
        }
    } catch (const std::exception& e) {
        LOG(ERROR) << "  evaluateTargets异常: " << e.what();
    }
}

// 跨帧追踪 + 代表帧更新（对齐 App UpdateLockedFeatures: err < min_dist 时更新）。
//   reference_x: label=16 用 peak_x；label=17/23 用 x_leading
//   关联判据: same label + reference_x 单调递减且 Δx < same_target_dx_max
//   释放：prev_ref_x < eval_x_min 时 emit rep 到 scene_reps；未锁定则丢弃
void OfflineEvaluator::trackAndEmitTargets(std::vector<TargetTrack>& tracks,
                                         std::vector<TargetObject>& scene_reps,
                                         const std::vector<TargetObject>& frame_bumps,
                                         size_t frame_idx)
{
    auto reference_x = [](const TargetObject& b) -> float {
        return (b.label == 16) ? b.x_peak : b.x_leading;
    };

    // Step 1: 释放已越过窗口下限的 track
    for (auto it = tracks.begin(); it != tracks.end(); ) {
        if (it->prev_ref_x < target_eval_x_min_) {
            if (it->locked && !it->emitted) {
                scene_reps.push_back(it->rep);
                it->emitted = true;
            }
            it = tracks.erase(it);
        } else {
            ++it;
        }
    }

    // 只允许“进入本帧前已存在的 tracks”参与本轮关联
    const size_t existing_tracak_count = tracks.size();
    std::vector<uint8_t> track_used(existing_tracak_count, 0);
    // Step 2: 关联 + 持续更新代表帧
    //std::vector<bool> track_used(tracks.size(), false);
    for (const auto& fb : frame_bumps) {
        const float ref = reference_x(fb);
        if (ref > target_eval_x_max_) continue;   // 远场目标不入追踪
        int best_track = -1;
        double best_dx = same_target_dx_max_;
        for (size_t i = 0; i < existing_tracak_count; ++i) {
            if (track_used[i]) continue;
            if (tracks[i].label != fb.label) continue;
            const double dx = tracks[i].prev_ref_x - ref;
            if (dx > 0.0 && dx < best_dx) {
                best_dx = dx;
                best_track = static_cast<int>(i);
            }
        }

        auto try_update_best = [&](TargetTrack& tr) {
            if (ref >= eval_x_ideal_) return;
            const double err = eval_x_ideal_ - ref;
            if (err < tr.min_dist) {
                TargetObject rep = fb;
                rep.frame_idx = frame_idx;
                rep.scene_idx = current_scene_idx_;
                tr.rep = rep;
                tr.min_dist = err;
                tr.locked  = true;
            }
        };

        if (best_track >= 0) {
            auto& tr = tracks[best_track];
            track_used[best_track] = true;
            tr.prev_ref_x = ref;
            try_update_best(tr);
        } else {
            TargetTrack nt;
            nt.prev_ref_x = ref;
            nt.label      = fb.label;
            try_update_best(nt);
            tracks.push_back(std::move(nt));
        }
    }
}

// 场景切换/评测收尾：把当前场景内累计的目标物中间状态统一结算为最终统计。
// 处理流程：
//   1) 兜底输出尚未释放的 pred active track（场景结束前还没越过释放窗口下限）
//   2) 以 pred representative 为主线，按车轮、按 frame_idx 到 GT 缓存中查找同帧配对
//   3) 匹配成功后按 label 分派误差统计；pred 找不到 GT 记 FP，GT 未被命中记 FN
//   4) 清空 scene 级缓存，避免污染下一场景
// 说明：GT 不做跨帧锁帧；GT 的统计粒度由 pred representative 的 frame_idx 驱动。
void OfflineEvaluator::flushTargetStatsForScene()
{
    // Step 1: 兜底 emit pred 未释放 track（车轮尚未越过 eval_x_min 就场景结束）
    // active_pred_tracks_ 存的是“仍在追踪中的预测目标”；这里把已锁定但尚未 emit 的代表帧补入 scene_pred_reps_。
    for (int w = 0; w < 4; ++w) {
        for (auto& tr : active_pred_tracks_[w]) {
            if (tr.locked && !tr.emitted) {
                scene_pred_reps_[w].push_back(tr.rep);
                tr.emitted = true;
            }
        }
    }

    // 本次 flush 的场景级汇总计数，仅用于日志输出。
    size_t total_matched = 0, total_fp = 0, total_fn = 0;

    // Step 2: 每轮独立处理 —— pred 代表帧 → 同帧 GT 查找配对
    // per-wheel 独立结算，避免不同车轮间目标互相污染。
    for (int w = 0; w < 4; ++w) {
        auto& pred_reps = scene_pred_reps_[w];
        auto& gt_by_frame = scene_gt_by_frame_[w];

        // 记录“某一帧 GT 中哪些 index 已被 pred 命中”。
        // key: frame_idx，value: 与 gt_frame_bumps 等长的 bool 数组。
        // Step 3 会利用它统计该帧剩余未匹配的 GT（FN）。
        std::map<size_t, std::vector<bool>> gt_matched_map;

        // 遍历该车轮所有 pred representative。
        for (const auto& pred : pred_reps) {
            const int lbl = pred.label;
            auto& s = focus_target_stats_[lbl];
            s.total_pred_bumps++;  // 每个 representative 都算一个预测目标
            const TargetAnomalyRecord rec_pred{pred.scene_idx, pred.frame_idx};

            // 查找 pred.frame_idx 对应的 GT 同帧目标。
            // 正式匹配只使用未占用且 dx 小于阈值的同 label GT；FP 诊断会额外
            // 记录同帧同轮同 label 的最近 GT 距离（不受占用状态和阈值限制）。
            auto x_of = [](const TargetObject& b) {
                return (b.label == 16) ? b.x_peak : b.x_leading;
            };
            const double pred_reference_x = x_of(pred);
            bool has_nearest_gt = false;
            double nearest_gt_delta_x = 0.0;
            auto frame_it = gt_by_frame.find(pred.frame_idx);
            const TargetObject* gt_match = nullptr;
            if (frame_it != gt_by_frame.end()) {
                auto& gt_frame_bumps = frame_it->second;

                // 诊断专用：所有同 label GT 均参与最近距离搜索，即使已被其他预测占用。
                for (const auto& gt : gt_frame_bumps) {
                    if (gt.label != lbl) continue;
                    const double dx = std::abs(pred_reference_x - x_of(gt));
                    if (!has_nearest_gt || dx < nearest_gt_delta_x) {
                        has_nearest_gt = true;
                        nearest_gt_delta_x = dx;
                    }
                }

                auto& matched_flags = gt_matched_map[pred.frame_idx];
                if (matched_flags.size() < gt_frame_bumps.size()) {
                    matched_flags.resize(gt_frame_bumps.size(), false);
                }

                // 正式匹配：同帧、同 label、尚未被占用，且 dx 严格小于匹配阈值。
                double best_dx = TARGET_MATCH_THRESHOLD;
                int best_i = -1;
                for (size_t i = 0; i < gt_frame_bumps.size(); ++i) {
                    if (matched_flags[i]) continue;
                    if (gt_frame_bumps[i].label != lbl) continue;
                    const double dx = std::abs(pred_reference_x - x_of(gt_frame_bumps[i]));
                    if (dx < best_dx) { best_dx = dx; best_i = static_cast<int>(i); }
                }
                if (best_i >= 0) {
                    gt_match = &gt_frame_bumps[best_i];
                    matched_flags[best_i] = true;
                }
            }

            if (!gt_match) {
                // pred 没找到满足正式匹配条件的 GT：记为 FP，并保留最近同类 GT 诊断。
                s.false_positive++;
                s.fp_records.push_back({pred.scene_idx, pred.frame_idx, w, lbl,
                                        pred_reference_x, has_nearest_gt, nearest_gt_delta_x});
                total_fp++;
                continue;
            }

            // ── 匹配成功：按 label 分派误差 ──
            s.matched_bumps++;
            s.matched_per_wheel[w]++;
            total_matched++;

            // 宽度误差：所有目标类别统一统计，定义为代表段路径长度之差的绝对值。
            const double w_err = std::abs(pred.width - gt_match->width);
            s.width_errors.push_back(w_err);
            if (w_err > TARGET_W_ERR_LARGE) s.large_w_err_records.push_back(rec_pred);

            if (lbl == 16) {
                // 减速带：极值点 x/z 误差。
                // x_peak 表示段内最高点的纵向位置；z_peak 表示该最高点的高度。
                const double x_err = std::abs(pred.x_peak - gt_match->x_peak);
                const double z_err = std::abs(pred.z_peak - gt_match->z_peak);
                s.x_peak_errors.push_back(x_err);
                s.z_peak_errors.push_back(z_err);
                if (x_err > TARGET_X_ERR_LARGE) s.large_x_err_records.push_back(rec_pred);
                if (z_err > TARGET_Z_ERR_LARGE) s.large_z_err_records.push_back(rec_pred);
                if (x_err > TARGET_X_ERR_MID) s.mid_x_error_count++;
                if (z_err > TARGET_Z_ERR_MID) s.mid_z_error_count++;
            } else {
                // 井盖/大桥接缝：使用前缘点 x 误差 + 段均值 z 误差。
                const double x_err = std::abs(pred.x_leading - gt_match->x_leading);
                const double z_err = std::abs(pred.mean_z - gt_match->mean_z);
                s.x_leading_errors.push_back(x_err);
                s.z_mean_errors.push_back(z_err);
                if (x_err > TARGET_X_ERR_LARGE) s.large_x_err_records.push_back(rec_pred);
                if (x_err > TARGET_X_ERR_MID) s.mid_x_error_count++;
            }

            // 内存诊断：追踪 x 误差 vector 的峰值，供附录容量诊断使用。
            const size_t cur_size = std::max(s.x_peak_errors.size(), s.x_leading_errors.size());
            if (cur_size > max_target_errors_size_) {
                max_target_errors_size_ = cur_size;
            }
        }

        // Step 3: FN + total_gt — 仅统计 pred 锁定帧内的 GT 目标
        // 遍历 gt_matched_map 中出现过的 frame_idx（即被 pred representative 访问过的 GT 帧），
        // 对该帧所有 focus label 目标计 total_gt_bumps；其中未被 matched_flags 命中的记为 FN。
        for (const auto& [fidx, matched_flags] : gt_matched_map) {
            auto it = gt_by_frame.find(fidx);
            if (it == gt_by_frame.end()) continue;
            const auto& gt_frame = it->second;
            for (size_t i = 0; i < gt_frame.size(); ++i) {
                if (!focus_target_labels_.count(gt_frame[i].label)) continue;
                auto& s = focus_target_stats_[gt_frame[i].label];
                s.total_gt_bumps++;
                if (i >= matched_flags.size() || !matched_flags[i]) {
                    s.false_negative++;
                    s.fn_records.push_back({current_scene_idx_, fidx});
                    total_fn++;
                }
            }
        }
    }

    if (total_matched + total_fp + total_fn > 0) {
        LOG(INFO) << "[eval-flush] 场景 " << current_scene_idx_ << " 目标物 flush: 匹配="
                  << total_matched << ", 误检=" << total_fp << ", 漏检=" << total_fn;
    }

    // Step 4: 清空场景状态（准备下一场景）
    // 本场景的 active track / representative / GT 按帧缓存都已结算完成，必须清空后再进入下一场景。
    for (int w = 0; w < 4; ++w) {
        active_pred_tracks_[w].clear();
        scene_pred_reps_[w].clear();
        scene_gt_by_frame_[w].clear();
    }
}
