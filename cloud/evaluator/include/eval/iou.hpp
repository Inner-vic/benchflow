#pragma once

#include <vector>
#include <string>
#include <iostream>
#include <iomanip>
#include <numeric>
#include <algorithm>
#include <map>
#include "compat/logging_compat.hpp"

class Iou {
public:
    Iou(const int num_classes, const std::vector<int>& class_indices, 
        const int empty_label, const std::vector<std::string>& label_str, 
        const std::string& name = "none",
        const std::vector<int>& drivable_surface_labels = {1},
        int H = 150, int W = 40, int D = 100) 
        : num_classes_(num_classes), 
          class_indices_(class_indices), 
          empty_label_(empty_label),
          label_str_(label_str),
          name_(name),
          drivable_surface_labels_(drivable_surface_labels),
          H_(H), W_(W), D_(D) {
        reset();
    }

    void reset() {
        // 初始化各类别统计计数器（最后一个元素存储整体占用率）
        total_gt_count_.resize(num_classes_ + 1, 0);      // 真值数量（TP+FN）
        total_tp_count_.resize(num_classes_ + 1, 0);      // 真阳性数量
        total_pred_count_.resize(num_classes_ + 1, 0);    // 预测数量（TP+FP）
        
        // 初始化单层可驾驶路面计数器
        total_seen_ds_ = 0;       // 可驾驶路面真值数量
        total_correct_ds_ = 0;    // 可驾驶路面预测正确数量
        total_positive_ds_ = 0;   // 可驾驶路面预测数量
    }

    // 累积单帧IoU统计（类别+z值必须同时匹配）
    // @param pred_labels: 预测类别标签
    // @param gt_labels: 真值类别标签
    // @param pred_z_indices: 预测z索引
    // @param gt_z_indices: 真值z索引
    void after_step(const std::vector<int64_t>& pred_labels, 
                   const std::vector<int64_t>& gt_labels,
                   const std::vector<int>& pred_z_indices, 
                   const std::vector<int>& gt_z_indices) {
        const size_t num_voxels = pred_labels.size();
        
        // 参数验证：确保所有数组大小一致
        if (gt_labels.size() != num_voxels || 
            pred_z_indices.size() != num_voxels || 
            gt_z_indices.size() != num_voxels) {
            LOG(ERROR) << "Iou: 数组大小不一致: pred_labels=" << num_voxels 
                       << ", gt_labels=" << gt_labels.size()
                       << ", pred_z=" << pred_z_indices.size() 
                       << ", gt_z=" << gt_z_indices.size();
            return;
        }
        
        LOG(INFO) << "Iou: num_voxels: " << num_voxels;
        
        // 统计各类别的TP/FP/FN
        for (size_t cls_idx = 0; cls_idx < class_indices_.size(); ++cls_idx) {
            const int target_class = class_indices_[cls_idx];
            int gt_count = 0;        // 真值中该类别的数量（TP+FN）
            int tp_count = 0;        // 真阳性：预测正确的数量
            int pred_count = 0;      // 预测为该类别的数量（TP+FP）
            
            for (size_t voxel_idx = 0; voxel_idx < num_voxels; ++voxel_idx) {
                const int64_t pred_label = pred_labels[voxel_idx];
                const int64_t gt_label = gt_labels[voxel_idx];
                
                // 统计真值数量
                if (gt_label == target_class) {
                    gt_count++;
                    // 判断是否预测正确：类别匹配 且 z索引匹配
                    if (pred_label == target_class && pred_z_indices[voxel_idx] == gt_z_indices[voxel_idx]) {
                        tp_count++;
                    }
                }
                // 统计预测数量
                if (pred_label == target_class) {
                    pred_count++;
                }
            }
            
            total_gt_count_[cls_idx] += gt_count;
            total_tp_count_[cls_idx] += tp_count;
            total_pred_count_[cls_idx] += pred_count;
        }
        
        // 统计整体占用率（所有非空类别合并）
        int overall_gt_occupied = 0;      // 真值非空体素数
        int overall_tp_occupied = 0;      // 预测正确的非空体素数
        int overall_pred_occupied = 0;    // 预测非空体素数
        
        for (size_t voxel_idx = 0; voxel_idx < num_voxels; ++voxel_idx) {
            const int64_t pred_label = pred_labels[voxel_idx];
            const int64_t gt_label = gt_labels[voxel_idx];
            
            if (gt_label != empty_label_) {
                overall_gt_occupied++;
                // 判断是否预测正确：类别匹配 且 z索引匹配
                if (pred_label == gt_label && pred_z_indices[voxel_idx] == gt_z_indices[voxel_idx]) {
                    overall_tp_occupied++;
                }
            }
            if (pred_label != empty_label_) {
                overall_pred_occupied++;
            }
        }
        LOG(INFO) << "Iou: gt_occupied=" << overall_gt_occupied 
                  << ", tp_occupied=" << overall_tp_occupied 
                  << ", pred_occupied=" << overall_pred_occupied;
        
        total_gt_count_[num_classes_] += overall_gt_occupied;
        total_tp_count_[num_classes_] += overall_tp_occupied;
        total_pred_count_[num_classes_] += overall_pred_occupied;
    }

    // 单层可驾驶路面统计方法（对应metric_util.py的single_layer_ds_count的else分支）
    void single_layer_ds_count(const std::vector<int64_t>& final_occ, const std::vector<int64_t>& gt_occ_drivable) 
    {
        if (final_occ.size() != static_cast<size_t>(H_ * W_ * D_) || gt_occ_drivable.size() != static_cast<size_t>(H_ * W_ * D_)) {
            return;
        }
        
        // 统计gt_occ_drivable==1的数量
        for (size_t i = 0; i < gt_occ_drivable.size(); ++i) {
            if (gt_occ_drivable[i] == 1) {
                total_seen_ds_++;
            }
        }
        
        // 构建整体可驾驶预测mask并统计total_positive_ds
        std::vector<bool> ds_final_occ_mask_global(H_ * W_ * D_, false);
        for (size_t i = 0; i < final_occ.size(); ++i) {
            bool is_drivable = std::find(drivable_surface_labels_.begin(), 
                                       drivable_surface_labels_.end(), 
                                       final_occ[i]) != drivable_surface_labels_.end();
            ds_final_occ_mask_global[i] = is_drivable;
            if (is_drivable) {
                total_positive_ds_++;
            }
        }
        
        // 为每个水平位置(H*W)处理可驾驶路面统计
        for (int hw = 0; hw < H_ * W_; ++hw) {
            // 找到当前位置的argmax（对于boolean数组，torch.argmax返回第一个True的索引）
            int ds_layer_idx = -1;
            for (int d = 0; d < D_; ++d) {  // 从前往后找，匹配torch.argmax行为
                int voxel_idx = hw * D_ + d;
                if (ds_final_occ_mask_global[voxel_idx]) {
                    ds_layer_idx = d;
                    break;
                }
            }
            
            // 在argmax位置检查预测和真实标签的匹配
            if (ds_layer_idx >= 0) {
                int selected_voxel_idx = hw * D_ + ds_layer_idx;
                bool ds_final_occ_argmax_mask = ds_final_occ_mask_global[selected_voxel_idx];
                bool ds_gt_occ_argmax_mask = (gt_occ_drivable[selected_voxel_idx] == 1);
                
                if (ds_final_occ_argmax_mask && ds_gt_occ_argmax_mask) {
                    total_correct_ds_++;
                }
            }
        }
    }

    // 累积单帧轨迹IoU统计（仅类别匹配，不考虑高度）
    // @param pred_labels: 预测类别标签（物理坐标形式）
    // @param gt_labels: 真值类别标签（物理坐标形式）
    void after_step_trajectory(const std::vector<int>& pred_labels, 
                               const std::vector<int>& gt_labels) {
        const size_t num_points = pred_labels.size();
        
        // 参数验证：确保数组大小一致
        if (gt_labels.size() != num_points) {
            LOG(ERROR) << "Iou: 数组大小不一致: pred_labels=" << num_points  << ", gt_labels=" << gt_labels.size();
            return;
        }
        
        // 统计各类别的TP/FP/FN（只考虑class_indices中的类别）
        for (size_t cls_idx = 0; cls_idx < class_indices_.size(); ++cls_idx) {
            const int target_class = class_indices_[cls_idx];
            int gt_count = 0;        // 真值中该类别的数量（TP+FN）
            int tp_count = 0;        // 真阳性：预测正确的数量
            int pred_count = 0;      // 预测为该类别的数量（TP+FP）
            
            for (size_t i = 0; i < num_points; ++i) {
                const int pred_label = pred_labels[i];
                const int gt_label = gt_labels[i];
                
                // 统计真值数量
                if (gt_label == target_class) {
                    gt_count++;
                    // 判断是否预测正确：仅类别匹配
                    if (pred_label == target_class) {
                        tp_count++;
                    }
                }
                // 统计预测数量
                if (pred_label == target_class) {
                    pred_count++;
                }
            }
            
            total_gt_count_[cls_idx] += gt_count;
            total_tp_count_[cls_idx] += tp_count;
            total_pred_count_[cls_idx] += pred_count;
        }
        
        // 统计整体占用率（所有非空类别合并）
        int overall_gt_occupied = 0;      // 真值非空点数
        int overall_tp_occupied = 0;      // 预测正确的非空点数
        int overall_pred_occupied = 0;    // 预测非空点数
        
        for (size_t i = 0; i < num_points; ++i) {
            const int pred_label = pred_labels[i];
            const int gt_label = gt_labels[i];
            
            // 真值非空
            if (gt_label != empty_label_) {
                overall_gt_occupied++;
                // 预测正确（类别匹配）
                if (pred_label == gt_label) {
                    overall_tp_occupied++;
                }
            }
            // 预测非空
            if (pred_label != empty_label_) {
                overall_pred_occupied++;
            }
        }
        
        // 累积到最后一个维度（索引num_classes_）
        total_gt_count_[num_classes_] += overall_gt_occupied;
        total_tp_count_[num_classes_] += overall_tp_occupied;
        total_pred_count_[num_classes_] += overall_pred_occupied;
    }

    // 计算并输出epoch级别的IoU指标
    std::pair<float, float> after_epoch() {
        std::vector<float> class_ious, class_precisions, class_recalls;

        // 计算各类别的IoU、Precision、Recall
        for (int cls_idx = 0; cls_idx < num_classes_; ++cls_idx) {
            const int gt_count = total_gt_count_[cls_idx];      // TP+FN
            const int tp_count = total_tp_count_[cls_idx];      // TP
            const int pred_count = total_pred_count_[cls_idx];  // TP+FP
            
            float precision = 0.0f;
            if (pred_count > 0) {
                precision = static_cast<float>(tp_count) / pred_count;
            }
            class_precisions.push_back(precision);
            
            float recall = 0.0f;
            float iou = 0.0f;
            if (gt_count == 0) {
                // 真值中无该类别，默认为完美匹配
                recall = 1.0f;
                iou = 1.0f;
            } else {
                recall = static_cast<float>(tp_count) / gt_count;
                // IoU = TP / (TP+FP+FN) = TP / (gt_count + pred_count - tp_count)
                iou = static_cast<float>(tp_count) / (gt_count + pred_count - tp_count);
            }
            class_ious.push_back(iou);
            class_recalls.push_back(recall);
        }

        // 计算平均IoU
        float mean_iou = 0.0f;
        if (!class_ious.empty()) {
            mean_iou = std::accumulate(class_ious.begin(), class_ious.end(), 0.0f) / class_ious.size();
        }

        // 打印统计表格（包含IoU、Precision、Recall）
        print_count_table();
        
        // 输出平均IoU
        LOG(INFO) << "Iou: mean_iou: " << (mean_iou * 100.0f);

        // 计算整体占用率IoU（所有非空类别）
        const int overall_gt = total_gt_count_[num_classes_];
        const int overall_tp = total_tp_count_[num_classes_];
        const int overall_pred = total_pred_count_[num_classes_];
        float overall_iou = 0.0f;
        if (overall_gt > 0) {
            overall_iou = static_cast<float>(overall_tp) / (overall_gt + overall_pred - overall_tp);
        }
        LOG(INFO) << "Iou: overall_occupancy_iou: " << (overall_iou * 100.0f);
        
        // 计算单层可驾驶路面IoU（暂时注释，不输出）
        // float single_layer_ds_iou = 0.0f;
        // if (total_seen_ds_ > 0) {
        //     single_layer_ds_iou = static_cast<float>(total_correct_ds_) /
        //                          std::max(1, (total_seen_ds_ + total_positive_ds_ - total_correct_ds_));
        // }

        // LOG(INFO) << "Iou: single layer driveable surface iou: " << (single_layer_ds_iou * 100.0f);
        // LOG(INFO) << "Iou: single layer driveable surface seen: " << total_seen_ds_;
        // LOG(INFO) << "Iou: single layer driveable surface correct: " << total_correct_ds_;
        // LOG(INFO) << "Iou: single layer driveable surface positive: " << total_positive_ds_;

        // std::cout << "iou2: " << overall_iou * 100.0f << std::endl;
        // std::cout << "single layer driveable surface iou: " << single_layer_ds_iou * 100.0f << std::endl;
        return std::make_pair(mean_iou * 100.0f, overall_iou * 100.0f);
    }

private:
    // 打印统计数据（表格格式：类别数行×6列）
    void print_count_table() {
        // 表头
        std::cout << "\n" << std::string(135, '-') << std::endl;
        std::cout << std::left << std::setw(20) << "Class"
                  << std::left << std::setw(18) << "IoU"
                  << std::left << std::setw(18) << "Precision"
                  << std::left << std::setw(18) << "Recall"
                  << std::left << std::setw(12) << "GT_Count"
                  << std::left << std::setw(12) << "TP_Count"
                  << std::left << std::setw(12) << "Pred_Count" << std::endl;
        std::cout << std::string(135, '-') << std::endl;
        
        // 数据行（只输出前num_classes_个类别，最后一个是overall统计）
        for (int i = 0; i < num_classes_; ++i) {
            const int gt_count = total_gt_count_[i];
            const int tp_count = total_tp_count_[i];
            const int pred_count = total_pred_count_[i];
            
            // 计算IoU、Precision、Recall
            float precision = (pred_count > 0) ? (static_cast<float>(tp_count) / pred_count) : 0.0f;
            float recall = (gt_count > 0) ? (static_cast<float>(tp_count) / gt_count) : 1.0f;
            float iou = (gt_count > 0) ? (static_cast<float>(tp_count) / (gt_count + pred_count - tp_count)) : 1.0f;
            
            std::cout << std::left << std::setw(20) << label_str_[i]
                      << std::left << std::setw(18) << std::fixed << std::setprecision(4) << (iou * 100.0f)
                      << std::left << std::setw(18) << std::fixed << std::setprecision(4) << (precision * 100.0f)
                      << std::left << std::setw(18) << std::fixed << std::setprecision(4) << (recall * 100.0f)
                      << std::left << std::setw(12) << gt_count
                      << std::left << std::setw(12) << tp_count
                      << std::left << std::setw(12) << pred_count << std::endl;
        }
        
        // Overall统计行
        std::cout << std::string(135, '-') << std::endl;
        const int overall_gt = total_gt_count_[num_classes_];
        const int overall_tp = total_tp_count_[num_classes_];
        const int overall_pred = total_pred_count_[num_classes_];
        float overall_precision = (overall_pred > 0) ? (static_cast<float>(overall_tp) / overall_pred) : 0.0f;
        float overall_recall = (overall_gt > 0) ? (static_cast<float>(overall_tp) / overall_gt) : 1.0f;
        float overall_iou = (overall_gt > 0) ? (static_cast<float>(overall_tp) / (overall_gt + overall_pred - overall_tp)) : 1.0f;
        
        std::cout << std::left << std::setw(20) << "Overall(occupied)"
                  << std::left << std::setw(18) << std::fixed << std::setprecision(4) << (overall_iou * 100.0f)
                  << std::left << std::setw(18) << std::fixed << std::setprecision(4) << (overall_precision * 100.0f)
                  << std::left << std::setw(18) << std::fixed << std::setprecision(4) << (overall_recall * 100.0f)
                  << std::left << std::setw(12) << overall_gt
                  << std::left << std::setw(12) << overall_tp
                  << std::left << std::setw(12) << overall_pred << std::endl;
        std::cout << std::string(135, '-') << std::endl;
    }
    
    // ===== 基本配置 =====
    int num_classes_;
    std::vector<int> class_indices_;
    int empty_label_;
    std::vector<std::string> label_str_;
    std::string name_;
    
    // ===== 各类别IoU统计（索引0~num_classes-1对应各类别，索引num_classes对应整体占用率） =====
    std::vector<int> total_gt_count_;          // 各类别真值数量（TP+FN）
    std::vector<int> total_tp_count_;          // 各类别真阳性数量（预测正确）
    std::vector<int> total_pred_count_;        // 各类别预测数量（TP+FP）
    
     // ===== 单层可驾驶路面统计 =====
    std::vector<int> drivable_surface_labels_;
    int H_, W_, D_;  // 体素网格尺寸（高×宽×深）

    // 单层可驾驶路面统计计数器
    int total_seen_ds_;
    int total_correct_ds_;
    int total_positive_ds_;
};