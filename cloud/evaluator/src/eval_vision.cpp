#include "evaluator.hpp"
#include "compat/logging_compat.hpp"

// ===========================================================================================
// 任务3：视觉分类评测
// ===========================================================================================
void OfflineEvaluator::evaluateVisionClassification(const AuxiliaryOutputs& predicted,
                                                 const std::map<std::string, int>& ground_truth)
{
    try {
        if (ground_truth.empty()) {
            LOG(WARNING) << "  视觉分类真值为空，跳过";
            return;
        }
        VisionTaskPredictions current_predictions;
        for (const auto& [name, logits] : predicted.heads) {
            if (vision_class_counts_.count(name))
                current_predictions.heads[name] = logits;
        }

        VisionTaskLabels current_labels;
        for (const auto& [name, label] : ground_truth) {
            if (vision_class_counts_.count(name))
                current_labels.heads[name] = label;
        }

        if (vision_map_calculator_) {
            VisionSampleContext context;
            context.scene_idx = current_scene_idx_;
            context.frame_idx = current_gt_sample_idx_;
            context.timestamp_ms = getCurrentTimestampMs();
            vision_map_calculator_->add_sample(current_predictions, current_labels, context);
            LOG(INFO) << "  视觉分类mAP评测完成，累积样本: "
                      << vision_map_calculator_->get_current_count();
        }
    } catch (const std::exception& e) {
        LOG(ERROR) << "  evaluateVisionClassification异常: " << e.what();
    }
}
