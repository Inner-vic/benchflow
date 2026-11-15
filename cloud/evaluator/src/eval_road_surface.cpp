#include "evaluator.hpp"
#include "compat/logging_compat.hpp"
#include <cmath>
#include <algorithm>
#include <climits>

// ===========================================================================================
// 任务1：路面评测
// ===========================================================================================
std::vector<OfflineEvaluator::OccPoint> OfflineEvaluator::convertRoadSurfaceToOccIndices(const RoadSurface* road_surface_ptr)
{
    std::vector<OccPoint> road_surface_indices;

    try {
        if (road_surface_ptr && road_surface_ptr->pts_num > 0) {
            const auto& pts = road_surface_ptr->pts;
            road_surface_indices.reserve(pts.size());


            LOG(INFO) << "  开始转换预测路面坐标（物理坐标→索引值）...";

            // 推理节点的roadOutput已按顺序存储了pred_NX_*pred_NY_个点（x,y,z,label都已填充）
            // xy是网格中心点物理坐标，z根据类别有两种情况：
            //   - empty类别(26)：z=-1.0（特殊标记）
            //   - 非empty类别(1-25，没有15类别：路面噪点)：z=网格中心点物理坐标
            // 转换为OCC索引（与真值网格对齐）
            for (size_t i = 0; i < pts.size(); ++i) {
                float x_phys = pts[i].x;
                float y_phys = pts[i].y;
                float z_phys = pts[i].z;
                int64_t label = static_cast<int64_t>(pts[i].clz);

                // 物理坐标（网格中心点）→ 真值网格索引（找最近的真值网格中心点），下述公式减去半格是因为xy是网格中心点
                int x_idx = static_cast<int>(std::round((x_phys - gt_X_MIN_ - gt_CELL_X_ / 2.0) / gt_CELL_X_));
                int y_idx = static_cast<int>(std::round((y_phys - gt_Y_MIN_ - gt_CELL_Y_ / 2.0) / gt_CELL_Y_));
                // 边界保护
                x_idx = std::max(0, std::min(x_idx, gt_NX_ - 1));
                y_idx = std::max(0, std::min(y_idx, gt_NY_ - 1));

                int z_idx;
                if (label == 26 || z_phys == -1.0f) {
                    // empty类别：z=-1.0，设置z索引为0（或任意有效值，因为empty不关心z）
                    z_idx = 0;
                } else {
                    // 非empty类别：z是预测网格中心点（200个之一），映射到真值网格索引（100个）
                    z_idx = static_cast<int>(std::round((z_phys - gt_Z_MIN_ - gt_CELL_Z_ / 2.0) / gt_CELL_Z_));
                    // 限制在[0, NZ-1]范围内（处理边界情况）
                    z_idx = std::max(0, std::min(z_idx, gt_NZ_ - 1));
                }

                // 所有索引已经过边界保护，直接添加
                OccPoint pt;
                pt.x = x_idx;
                pt.y = y_idx;
                pt.z = z_idx;
                pt.label = label;
                road_surface_indices.push_back(pt);
            }

            LOG(INFO) << "  预测路面坐标转换: " << road_surface_indices.size() << "个点（物理→OCC索引）";
        } else {
            LOG(WARNING) << "  RoadSurface为空";
        }
    } catch (const std::exception& e) {
        LOG(ERROR) << "  convertRoadSurfaceToOccIndices异常: " << e.what();
    }

    return road_surface_indices;
}

std::vector<OfflineEvaluator::OccPoint> OfflineEvaluator::extractSingleLayerFromGroundTruth(const std::vector<int64_t>& occ_raw_data)
{
    std::vector<OccPoint> single_layer;

    try {
        // occ_raw_data格式: [x, y, z, class, ds] * N
        size_t num_voxels = occ_raw_data.size() / 5;

        if (num_voxels == 0) {
            LOG(WARNING) << "  真值数据为空";
            return single_layer;
        }

        LOG(INFO) << "  开始从 " << num_voxels << " 个体素中提取单层（真值xyz已是索引值）...";

        // column_data[x][y] = vector of (z_idx, class)，在init()中已初始化，运行时不变，只需清空数据
        for (int x = 0; x < gt_NX_; ++x) {
            for (int y = 0; y < gt_NY_; ++y) {
                column_data_[x][y].clear();
            }
        }

        // 遍历所有体素，按列分组
        // 注意：真值文件中的x/y/z已经是索引值(如 0-249.0, 0-79.0, 0-99.0，为float32形式)，不是物理坐标
        for (size_t i = 0; i < num_voxels; ++i) {
            int grid_x = static_cast<int>(occ_raw_data[i * 5 + 0]);  // x索引: 0-249
            int grid_y = static_cast<int>(occ_raw_data[i * 5 + 1]);  // y索引: 0-79
            int grid_z = static_cast<int>(occ_raw_data[i * 5 + 2]);  // z索引: 0-99
            int64_t cls = occ_raw_data[i * 5 + 3];                   // 类别: 1-25, 26(empty)

            // 跳过empty标签和越界数据
            if (cls == 26 || grid_x < 0 || grid_x >= gt_NX_ ||
                grid_y < 0 || grid_y >= gt_NY_ || grid_z < 0 || grid_z >= gt_NZ_) {
                continue;
            }

            column_data_[grid_x][grid_y].push_back({grid_z, cls});
        }

        // 预分配所有网格点（包括empty列）
        single_layer.reserve(gt_NX_ * gt_NY_);

        // 遍历所有网格列，提取单层数据
        int road_count = 0, obstacle_count = 0, empty_count = 0;
        for (int grid_x = 0; grid_x < gt_NX_; ++grid_x) {
            for (int grid_y = 0; grid_y < gt_NY_; ++grid_y) {
                const auto& voxels = column_data_[grid_x][grid_y];

                if (voxels.empty()) {
                    // 该列全为empty
                    single_layer.push_back({grid_x, grid_y, 0, 26});
                    empty_count++;
                } else {
                    // 该列有非空体素，分类：路面类别(1-23, 不含15) vs 障碍物类别(24-25)
                    int min_road_z = INT_MAX, max_obstacle_z = INT_MIN;
                    int64_t road_label = 0, obstacle_label = 0;

                    for (const auto& [z_idx, cls] : voxels) {
                        if (cls >= 1 && cls <= 23 && cls != 15) {  // 路面类别
                            if (z_idx < min_road_z) {
                                min_road_z = z_idx;
                                road_label = cls;
                            }
                        } else if (cls == 24 || cls == 25) {  // 障碍物类别
                            if (z_idx > max_obstacle_z) {
                                max_obstacle_z = z_idx;
                                obstacle_label = cls;
                            }
                        }
                    }

                    if (min_road_z != INT_MAX) {
                        // 路面类别：取最低点（z索引最小）
                        single_layer.push_back({grid_x, grid_y, min_road_z, road_label});
                        road_count++;
                    } else if (max_obstacle_z != INT_MIN) {
                        // 障碍物类别：取最高点（z索引最大）
                        single_layer.push_back({grid_x, grid_y, max_obstacle_z, obstacle_label});
                        obstacle_count++;
                    }
                }
            }
        }

        LOG(INFO) << "  单层提取完成: 总点数=" << single_layer.size()
                  << " (路面=" << road_count << ", 障碍物=" << obstacle_count
                  << ", empty=" << empty_count << ")";

    } catch (const std::exception& e) {
        LOG(ERROR) << "  extractSingleLayerFromGroundTruth异常: " << e.what();
    }

    return single_layer;
}

void OfflineEvaluator::evaluateRoadSurface(const std::vector<OccPoint>& predicted,
                                        const std::vector<OccPoint>& ground_truth)
{
    try {
        //已在init()中初始化，运行时不变，只需重置为默认值
        std::fill(pred_labels_.begin(), pred_labels_.end(), 26);  // 26=empty
        std::fill(gt_labels_.begin(), gt_labels_.end(), 26);
        std::fill(pred_z_.begin(), pred_z_.end(), 0);
        std::fill(gt_z_.begin(), gt_z_.end(), 0);

        // 填充预测网格（标签+z索引）
        // 注：predicted数据已在convertRoadSurfaceToOccIndices中完成边界检查
        int pred_non_empty = 0;
        for (const auto& pt : predicted) {
            const int idx = pt.y + pt.x * gt_NY_;  // 索引公式: y + x*NY
            pred_labels_[idx] = pt.label;
            pred_z_[idx] = pt.z;
            if (pt.label != 26) pred_non_empty++;
        }

        // 填充真值网格（标签+z索引）
        // 注：ground_truth数据已在extractSingleLayerFromGroundTruth中完成边界检查
        int gt_non_empty = 0;
        for (const auto& pt : ground_truth) {
            const int idx = pt.y + pt.x * gt_NY_;
            gt_labels_[idx] = pt.label;
            gt_z_[idx] = pt.z;
            if (pt.label != 26) gt_non_empty++;
        }

        const int grid_size = gt_NX_ * gt_NY_;
        LOG(INFO) << "  预测非空=" << pred_non_empty << ", 真值非空=" << gt_non_empty
                  << ", 网格大小=" << grid_size;

        // 执行IoU评测（类别+z值匹配）
        if (road_surface_iou_evaluator_) {
            road_surface_iou_evaluator_->after_step(pred_labels_, gt_labels_, pred_z_, gt_z_);
            LOG(INFO) << "  ✓ 路面IoU评测完成";
        }
    } catch (const std::exception& e) {
        LOG(ERROR) << "  evaluateRoadSurface异常: " << e.what();
    }
}
