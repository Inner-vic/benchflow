#ifndef MAP_UTIL_H
#define MAP_UTIL_H

#include <vector>
#include <map>
#include <string>
#include <algorithm>
#include <stdexcept>
#include <cmath>
#include <cstdint>
#include <numeric>
#include <memory>

// 前向声明
class GlobalMAPCalculator;

/**
 * @brief 单个样本的视觉头预测结果（map-based，支持任意任务集合）
 */
struct VisionTaskPredictions {
    std::map<std::string, std::vector<float>> heads;  // task_name -> logits
};

/**
 * @brief 单个样本的视觉头真实标签（map-based，支持任意任务集合）
 */
struct VisionTaskLabels {
    std::map<std::string, int> heads;  // task_name -> label
};

/**
 * @brief 用于将视觉评测结果回溯到输入数据的样本上下文。
 */
struct VisionSampleContext {
    size_t scene_idx = static_cast<size_t>(-1);
    size_t frame_idx = static_cast<size_t>(-1);
    uint64_t timestamp_ms = 0;
};

/**
 * @brief 一条 top-1 视觉分类错误；同时代表预测类别的 FP 和 GT 类别的 FN。
 */
struct VisionClassificationAlert {
    VisionSampleContext context;
    int gt_class = -1;
    int top1_class = -1;
    double gt_probability = 0.0;
    double top1_probability = 0.0;
    double probability_margin = 0.0;
};

/**
 * @brief 单个视觉类别的最终统计指标。
 *
 * AP 使用 One-vs-Rest 概率排序；Precision/Recall/count 使用 top-1 分类结果。
 */
struct VisionClassMetrics {
    double ap = 0.0;
    double precision = 0.0;
    double recall = 0.0;
    size_t gt_count = 0;
    size_t tp_count = 0;
    size_t pred_count = 0;
    bool has_ap = false;
    bool has_precision = false;
    bool has_recall = false;
};

/**
 * @brief 单个视觉头的最终统计指标与样本覆盖情况。
 */
struct VisionHeadMetrics {
    double map = 0.0;
    double top1_accuracy = 0.0;
    size_t valid_sample_count = 0;
    size_t gt_covered_class_count = 0;
    size_t skipped_missing_pair_count = 0;
    size_t skipped_invalid_logits_count = 0;
    size_t skipped_invalid_label_count = 0;
    std::vector<VisionClassMetrics> class_metrics;
    std::vector<VisionClassificationAlert> classification_alerts;
};

/**
 * @brief 全量mAP计算器 - 实现One-vs-Rest方法的全局mAP计算
 *
 * 支持任意视觉头任务集合，通过构造函数传入任务配置（名称/类别数/标签偏移）。
 * 新增任务只需修改 YAML 配置，无需改代码。
  * 功能说明：
 * - 累积所有样本的预测结果和真实标签
 * - 在所有样本收集完成后统一计算全量mAP
 * - 使用标准的Average Precision算法
 * 使用方式：
 * 1. 创建计算器实例，指定预期样本总数
 * 2. 逐个添加样本：add_sample(predictions, labels)
 * 3. 最后计算全量mAP：calculate_global_map()
 */
class GlobalMAPCalculator {
private:
    size_t max_sample_count_;  // 预期的最大样本数量
    size_t current_count_;     // 当前已添加的样本数量

    // 存储所有样本的数据
    std::vector<VisionTaskPredictions> all_predictions_;
    std::vector<VisionTaskLabels>      all_labels_;
    std::vector<VisionSampleContext>   all_contexts_;

    // 任务名称和对应的类别数量
    std::map<std::string, int> task_num_classes_;   // task_name -> num_classes
    // 任务名称和对应的标签偏移量（如 ego_lane_position 真值[-1,0,1-8] 需 +1 → 索引[0-9]）
    std::map<std::string, int> task_label_offsets_; // task_name -> label_offset (default 0)

    /**
     * @brief 计算Average Precision (AP) - C++实现
     */
    double calculate_average_precision(const std::vector<int>& y_true,
                                       const std::vector<double>& y_scores);

    /**
     * @brief Softmax函数实现
     */
    std::vector<double> softmax(const std::vector<float>& logits);

public:
    /**
     * @brief 构造函数
     * @param max_samples 预期的最大样本数量
     * @param class_counts 视觉头类别数量配置（可选，不传则为空）
     * @param label_offsets 各任务的标签偏移量（可选，缺省时该任务偏移为0）
     */
    explicit GlobalMAPCalculator(
        size_t max_samples = 1000,
        const std::map<std::string, int>& class_counts  = {},
        const std::map<std::string, int>& label_offsets = {});

    /**
     * @brief 添加单个样本
     * @param predictions 预测logits结果
     * @param labels 真实标签
     * @return 是否成功添加样本
     */
    bool add_sample(const VisionTaskPredictions& predictions,
                    const VisionTaskLabels& labels,
                    const VisionSampleContext& context = {});

    /**
     * @brief 计算每个视觉头及其所有类别的最终指标。
     * @return task_name -> 该视觉头的 mAP、top-1 指标和逐类别明细
     */
    std::map<std::string, VisionHeadMetrics> calculate_detailed_metrics();

    /**
     * @brief 计算全量mAP（使用One-vs-Rest方法）
     * @return 每个视觉任务的mAP分数
     */
    std::map<std::string, double> calculate_global_map();

    /**
     * @brief 重置计算器，清空所有数据
     */
    void   reset();

    /**
     * @brief 获取当前样本数量
     */
    size_t get_current_count() const { return current_count_; }

    /**
     * @brief 获取最大样本容量
     */
    size_t get_max_capacity()  const { return max_sample_count_; }

    /**
     * @brief 检查是否已达到预期样本数量
     */
    bool   is_complete()       const { return current_count_ >= max_sample_count_; }

    /**
     * @brief 扩展样本容量（用于处理超出预期的样本数量）
     */
    void expand_capacity(size_t additional_capacity) {
        max_sample_count_ += additional_capacity;
        all_predictions_.reserve(max_sample_count_);
        all_labels_.reserve(max_sample_count_);
        all_contexts_.reserve(max_sample_count_);
    }
};

// ====================================================================
// GlobalMAPCalculator 类实现
// ====================================================================

inline GlobalMAPCalculator::GlobalMAPCalculator(
    size_t max_samples,
    const std::map<std::string, int>& class_counts,
    const std::map<std::string, int>& label_offsets)
    : max_sample_count_(max_samples), current_count_(0),
      task_num_classes_(class_counts), task_label_offsets_(label_offsets)
{
    // 预分配空间以提高性能
    all_predictions_.reserve(max_sample_count_);
    all_labels_.reserve(max_sample_count_);
    all_contexts_.reserve(max_sample_count_);
}

inline bool GlobalMAPCalculator::add_sample(const VisionTaskPredictions& predictions,
                                             const VisionTaskLabels& labels,
                                             const VisionSampleContext& context) {
    // 如果超出预期容量，自动扩展
    if (current_count_ >= max_sample_count_) {
        expand_capacity(1000); // 每次扩展1000个样本的容量
    }

    all_predictions_.push_back(predictions);
    all_labels_.push_back(labels);
    all_contexts_.push_back(context);
    current_count_++;
    return true; // 始终返回成功
}

inline void GlobalMAPCalculator::reset() {
    all_predictions_.clear();
    all_labels_.clear();
    all_contexts_.clear();
    current_count_ = 0;
}

inline std::vector<double> GlobalMAPCalculator::softmax(const std::vector<float>& logits) {
    std::vector<double> result(logits.size());

    // 找到最大值以提高数值稳定性
    float max_logit = *std::max_element(logits.begin(), logits.end());

    // 计算exp和sum
    double sum_exp = 0.0;
    for (size_t i = 0; i < logits.size(); ++i) {
        result[i] = std::exp(logits[i] - max_logit);
        sum_exp += result[i];
    }

    // 归一化
    for (size_t i = 0; i < result.size(); ++i) {
        result[i] /= sum_exp;
    }

    return result;
}

inline double GlobalMAPCalculator::calculate_average_precision(
    const std::vector<int>& y_true, const std::vector<double>& y_scores)
{
    if (y_true.size() != y_scores.size() || y_true.empty()) {
        return 0.0;
    }

    // 创建索引并按分数排序
    std::vector<size_t> indices(y_scores.size());
    std::iota(indices.begin(), indices.end(), 0);
    std::sort(indices.begin(), indices.end(), [&y_scores](size_t a, size_t b) {
        return y_scores[a] > y_scores[b]; // 降序排列
    });

    int total_positives = std::accumulate(y_true.begin(), y_true.end(), 0);
    if (total_positives == 0) {
        return 0.0; // 没有正样本
    }

    // 使用与sklearn.average_precision_score相同的算法
    // 计算precision-recall曲线下的面积（插值方法）
    std::vector<double> precisions;
    std::vector<double> recalls;

    // 添加起始点 (recall=0, precision=1)
    precisions.push_back(1.0);
    recalls.push_back(0.0);

    int tp = 0, fp = 0;
    for (size_t idx : indices) {
        if (y_true[idx] == 1) tp++; else fp++;
        precisions.push_back(static_cast<double>(tp) / (tp + fp));
        recalls.push_back(static_cast<double>(tp) / total_positives);
    }

    // 插值 Precision 包络下的离散 PR 面积
    double ap = 0.0;
    for (size_t i = 1; i < recalls.size(); ++i) {
        double delta_recall = recalls[i] - recalls[i - 1];
        if (delta_recall > 0) {
            //当前实现更接近传统的插值 AP 方案（常见于历史 VOC 风格评测），
            //并不保证能与 sklearn 的 average_precision_score 逐位一致。
            double max_prec = *std::max_element(precisions.begin() + i, precisions.end());
            ap += delta_recall * max_prec;
        }
    }
    return ap;
}

inline std::map<std::string, VisionHeadMetrics> GlobalMAPCalculator::calculate_detailed_metrics() {
    std::map<std::string, VisionHeadMetrics> results;

    for (const auto& [task_name, num_classes] : task_num_classes_) {
        VisionHeadMetrics head_metrics;
        head_metrics.class_metrics.resize(static_cast<size_t>(num_classes));

        int label_offset = 0;
        auto off_it = task_label_offsets_.find(task_name);
        if (off_it != task_label_offsets_.end()) label_offset = off_it->second;

        std::vector<std::vector<double>> all_probs;
        std::vector<int> all_task_labels;
        std::vector<int> all_top1_predictions;
        std::vector<VisionSampleContext> valid_contexts;

        for (size_t i = 0; i < current_count_; ++i) {
            const auto& pred = all_predictions_[i];
            const auto& label = all_labels_[i];
            const auto pred_it = pred.heads.find(task_name);
            const auto label_it = label.heads.find(task_name);

            if (pred_it == pred.heads.end() || label_it == label.heads.end()) {
                head_metrics.skipped_missing_pair_count++;
                continue;
            }
            if (pred_it->second.size() != static_cast<size_t>(num_classes) || pred_it->second.empty()) {
                head_metrics.skipped_invalid_logits_count++;
                continue;
            }

            const int adjusted_label = label_it->second + label_offset;
            if (adjusted_label < 0 || adjusted_label >= num_classes) {
                head_metrics.skipped_invalid_label_count++;
                continue;
            }

            const auto probs = softmax(pred_it->second);
            const auto top1_it = std::max_element(probs.begin(), probs.end());
            all_probs.push_back(probs);
            all_task_labels.push_back(adjusted_label);
            all_top1_predictions.push_back(static_cast<int>(std::distance(probs.begin(), top1_it)));
            valid_contexts.push_back(i < all_contexts_.size() ? all_contexts_[i] : VisionSampleContext{});
        }

        head_metrics.valid_sample_count = all_task_labels.size();
        size_t total_top1_correct = 0;
        for (size_t i = 0; i < all_task_labels.size(); ++i) {
            if (all_top1_predictions[i] == all_task_labels[i]) continue;
            const int gt_class = all_task_labels[i];
            const int top1_class = all_top1_predictions[i];
            const double gt_probability = all_probs[i][static_cast<size_t>(gt_class)];
            const double top1_probability = all_probs[i][static_cast<size_t>(top1_class)];
            head_metrics.classification_alerts.push_back({
                valid_contexts[i], gt_class, top1_class, gt_probability,
                top1_probability, top1_probability - gt_probability
            });
        }
        std::vector<double> ap_scores;

        for (int cls = 0; cls < num_classes; ++cls) {
            auto& class_metrics = head_metrics.class_metrics[static_cast<size_t>(cls)];
            std::vector<int> y_true(all_task_labels.size());
            std::vector<double> y_scores(all_task_labels.size());

            for (size_t i = 0; i < all_task_labels.size(); ++i) {
                const bool is_gt_class = (all_task_labels[i] == cls);
                const bool is_pred_class = (all_top1_predictions[i] == cls);
                y_true[i] = is_gt_class ? 1 : 0;
                y_scores[i] = all_probs[i][static_cast<size_t>(cls)];

                if (is_gt_class) class_metrics.gt_count++;
                if (is_pred_class) class_metrics.pred_count++;
                if (is_gt_class && is_pred_class) {
                    class_metrics.tp_count++;
                    total_top1_correct++;
                }
            }

            if (class_metrics.gt_count > 0) {
                class_metrics.ap = calculate_average_precision(y_true, y_scores);
                class_metrics.has_ap = true;
                class_metrics.recall = static_cast<double>(class_metrics.tp_count) /
                                       static_cast<double>(class_metrics.gt_count);
                class_metrics.has_recall = true;
                ap_scores.push_back(class_metrics.ap);
                head_metrics.gt_covered_class_count++;
            }
            if (class_metrics.pred_count > 0) {
                class_metrics.precision = static_cast<double>(class_metrics.tp_count) /
                                          static_cast<double>(class_metrics.pred_count);
                class_metrics.has_precision = true;
            }
        }

        if (!ap_scores.empty()) {
            head_metrics.map = std::accumulate(ap_scores.begin(), ap_scores.end(), 0.0) /
                               static_cast<double>(ap_scores.size());
        }
        if (head_metrics.valid_sample_count > 0) {
            head_metrics.top1_accuracy = static_cast<double>(total_top1_correct) /
                                         static_cast<double>(head_metrics.valid_sample_count);
        }

        results[task_name] = std::move(head_metrics);
    }

    return results;
}

inline std::map<std::string, double> GlobalMAPCalculator::calculate_global_map() {
    std::map<std::string, double> results;
    const auto detailed_results = calculate_detailed_metrics();
    for (const auto& [task_name, metrics] : detailed_results) {
        results[task_name] = metrics.map;
    }
    return results;
}

// 移除了calculate_current_map函数，因为现在只需要最终的全量计算

#endif // MAP_UTIL_H
