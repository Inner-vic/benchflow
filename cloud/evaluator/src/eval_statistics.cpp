#include "evaluator.hpp"
#include "compat/logging_compat.hpp"
#include <iomanip>
#include <iostream>
#include <fstream>
#include <sstream>
#include <numeric>
#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <tuple>
#include <chrono>
#include <filesystem>

namespace {

class TeeBuf : public std::streambuf {
public:
    TeeBuf(std::streambuf* console, std::streambuf* file)
        : console_(console), file_(file) {}
protected:
    int overflow(int c) override {
        if (c == EOF) return !EOF;
        if (console_) console_->sputc(static_cast<char>(c));
        if (file_) file_->sputc(static_cast<char>(c));
        return c;
    }
    std::streamsize xsputn(const char* s, std::streamsize n) override {
        if (console_) console_->sputn(s, n);
        if (file_) file_->sputn(s, n);
        return n;
    }
    int sync() override {
        if (console_) console_->pubsync();
        if (file_) file_->pubsync();
        return 0;
    }
private:
    std::streambuf* console_;
    std::streambuf* file_;
};

} // namespace

void OfflineEvaluator::outputFinalStatistics()
{
    if (has_output_final_stats_) {
        return;
    }
    has_output_final_stats_ = true;

    // 自动保存报告到文件
    namespace fs = std::filesystem;
    auto now = std::chrono::system_clock::now();
    auto time_t_now = std::chrono::system_clock::to_time_t(now);
    std::tm tm_now{};
#ifdef _WIN32
    localtime_s(&tm_now, &time_t_now);
#else
    localtime_r(&time_t_now, &tm_now);
#endif

    std::ostringstream filename_ss;
    filename_ss << "eval_report_"
                << std::put_time(&tm_now, "%Y-%m-%d_%H-%M-%S")
                << ".txt";

    fs::path report_dir = fs::path("eval_report");
    fs::create_directories(report_dir);
    fs::path report_path = report_dir / filename_ss.str();

    std::ofstream report_file(report_path);
    std::streambuf* orig_buf = std::cout.rdbuf();
    std::unique_ptr<TeeBuf> tee_buf;
    if (report_file.is_open()) {
        tee_buf = std::make_unique<TeeBuf>(orig_buf, report_file.rdbuf());
        std::cout.rdbuf(tee_buf.get());
        LOG(INFO) << " 评测报告将保存至: " << fs::absolute(report_path).string();
    }

    const std::string separator(60, '=');
    const std::string thin_sep(60, '-');

    LOG(INFO) << " OfflineEvaluator: 候选预测帧 " << received_samples_
              << "，进入真值匹配 " << evaluation_samples_
              << "，至少完成一项 " << completed_samples_ << " 帧";

    // ═══════════════════════════════════════════════════════════════════════════
    // 计算阶段：先获取所有 KPI 数值，再按层级顺序输出
    // ═══════════════════════════════════════════════════════════════════════════

    // 路面/轨迹 IoU 在详情区域调用 after_epoch() 时直接输出

    // 轨迹 KPI
    double traj_z_mae_cm = 0.0, traj_acc_1cm_pct = 0.0, traj_pass_rate_pct = 0.0;
    if (trajectory_height_total_count_ > 0) {
        double z_mae_sum = std::accumulate(trajectory_height_errors_.begin(), trajectory_height_errors_.end(), 0.0);
        traj_z_mae_cm = (z_mae_sum / trajectory_height_errors_.size()) * 100.0;
        traj_acc_1cm_pct = static_cast<double>(trajectory_height_correct_count_) / trajectory_height_total_count_ * 100.0;
        traj_pass_rate_pct = static_cast<double>(trajectory_pass_combined_count_) / trajectory_height_total_count_ * 100.0;
    }

    // 目标物评测详细指标见 [3/4] 段按 label 展开（label 语义差异大，不再输出合并数字）
    bool has_any_target = false;
    for (const auto& [_, s] : focus_target_stats_) {
        if (s.matched_bumps + s.false_positive + s.false_negative > 0) { has_any_target = true; break; }
    }

    // 视觉分类明细指标与 Overall mAP。只有拥有有效成对样本的 head 才参与汇总。
    double vision_overall_map = 0.0;
    size_t vision_valid_count = 0;
    std::map<std::string, VisionHeadMetrics> vision_metrics;
    if (vision_map_calculator_) {
        vision_metrics = vision_map_calculator_->calculate_detailed_metrics();
        for (const auto& [name, metrics] : vision_metrics) {
            if (metrics.valid_sample_count == 0) continue;
            vision_overall_map += metrics.map;
            vision_valid_count++;
        }
        if (vision_valid_count > 0) vision_overall_map /= static_cast<double>(vision_valid_count);
    }

    // 性能统计
    double perf_avg_ms = 0.0, perf_total_s = 0.0;
    if (!evaluation_times_.empty()) {
        perf_avg_ms = std::accumulate(evaluation_times_.begin(), evaluation_times_.end(), 0.0) / evaluation_times_.size();
        perf_total_s = std::accumulate(evaluation_times_.begin(), evaluation_times_.end(), 0.0) / 1000.0;
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // 第一层：核心 KPI 总览
    // ═══════════════════════════════════════════════════════════════════════════
    std::cout << "\n";
    std::cout << "╔══════════════════════════════════════════════════════════════╗\n";
    std::cout << "║                     评 测 结 果 总 览                        ║\n";
    std::cout << "╠══════════════════════════════════════════════════════════════╣\n";

    // 帧级概况：任务 KPI 的实际样本数见各自的任务覆盖率统计。
    if (evaluation_samples_ > 0) {
        std::cout << "║ 候选 " << received_samples_
                  << " | 匹配流程 " << evaluation_samples_
                  << " | 至少完成一项 " << completed_samples_ << " ("
                  << std::fixed << std::setprecision(1)
                  << (completed_samples_ * 100.0 / evaluation_samples_) << "% )"
                  << std::endl;
    }
    std::cout << "╠══════════════════════════════════════════════════════════════╣\n";

    // 各任务核心 KPI（仅输出已启用的任务）
    if (eval_road_) {
        std::cout << "║ [路面]   mIoU = (见下方详情)                                ║" << std::endl;
    }
    if (eval_traj_ && trajectory_height_total_count_ > 0) {
        std::cout << "║ [轨迹]   Z_MAE = " << std::fixed << std::setprecision(2) << traj_z_mae_cm << "cm"
                  << "  Acc_1cm = " << std::setprecision(1) << traj_acc_1cm_pct << "%"
                  << "  Pass = " << traj_pass_rate_pct << "%"
                  << "  (帧: " << traj_task_stats_.evaluated << ")" << std::endl;
    }
    if (eval_traj_ && has_any_target) {
        std::cout << "║ [目标物] 详见 [3/4] 目标物评测详情（按 label 展开）           ║" << std::endl;
    }
    if (eval_vision_ && vision_valid_count > 0) {
        std::cout << "║ [视觉]   mAP = " << std::fixed << std::setprecision(4) << vision_overall_map
                  << "  (帧: " << vision_task_stats_.evaluated << ")" << std::endl;
    }

    std::cout << "╠══════════════════════════════════════════════════════════════╣\n";
    if (!evaluation_times_.empty()) {
        std::cout << "║ 耗时: 平均 " << std::fixed << std::setprecision(1) << perf_avg_ms << "ms/帧"
                  << "  总计 " << std::setprecision(1) << perf_total_s << "s" << std::endl;
    }
    std::cout << "╚══════════════════════════════════════════════════════════════╝\n";
    std::cout << std::endl;

    // ═══════════════════════════════════════════════════════════════════════════
    // 第二层：各任务详细指标
    // ═══════════════════════════════════════════════════════════════════════════

    // [1/4] 路面评测详情
    if (eval_road_) {
        std::cout << separator << "\n[1/4] 路面评测详情\n" << separator << std::endl;
        if (road_surface_iou_evaluator_) {
            road_surface_iou_evaluator_->after_epoch();
        } else {
            std::cout << "  路面评测器未初始化，无统计数据" << std::endl;
        }
        std::cout << separator << std::endl;
    }

    // [2/4] 轨迹评测详情
    if (eval_traj_ && trajectory_height_total_count_ > 0) {
        std::cout << "\n" << separator << "\n[2/4] 轨迹评测详情\n" << separator << std::endl;

        // 轨迹类别 IoU
        std::cout << "\n--- 轨迹类别 IoU ---" << std::endl;
        if (trajectory_iou_evaluator_) {
            trajectory_iou_evaluator_->after_epoch();
        }

        // 核心指标汇总
        double max_err = 0.0;
        for (double err : trajectory_height_errors_) {
            max_err = std::max(max_err, err);
        }

        std::cout << "\n总轨迹点数: " << trajectory_height_total_count_ << std::endl;

        std::cout << "\n[指标1] Z_MAE（高度平均绝对误差）:" << std::endl;
        std::cout << "  - 平均绝对误差: " << std::fixed << std::setprecision(4) << traj_z_mae_cm << " cm" << std::endl;
        std::cout << "  - 最大误差: " << max_err * 100.0 << " cm" << std::endl;

        std::cout << "\n[指标2] Acc_1cm（高度合格率）:" << std::endl;
        std::cout << "  - 合格率: " << std::fixed << std::setprecision(2) << traj_acc_1cm_pct << "%" << std::endl;
        std::cout << "  - 合格点数: " << trajectory_height_correct_count_ << " / " << trajectory_height_total_count_ << std::endl;

        std::cout << "\n[指标3] Label_mIoU:" << std::endl;
        std::cout << "  - 见上方轨迹类别 IoU 详细统计" << std::endl;

        std::cout << "\n[指标4] Pass_Rate_combined（综合通过率，最终指标）:" << std::endl;
        std::cout << "  - 定义: 高度≤1cm 且 标签完全一致" << std::endl;
        std::cout << "  - 通过率: " << std::fixed << std::setprecision(2) << traj_pass_rate_pct << "%" << std::endl;
        std::cout << "  - 通过点数: " << trajectory_pass_combined_count_ << " / " << trajectory_height_total_count_ << std::endl;

        // 特别关注类别统计 —— 暂不输出（沥青/水泥点级细节）
        // focus_class_metrics_ 仅覆盖 {1, 2}（见 getFocusClasses()）；累加代码保留在
        // evaluateTrajectory 中，需要恢复展示时把下面 #if 0 改为 #if 1 即可。
        // 关注类别 {16, 17, 23} 的点级指标已迁移到 [3/4] 目标物段（基于代表帧目标段内点集）。
#if 0
        if (!focus_class_metrics_.empty()) {
            std::cout << "\n" << thin_sep << "\n特别关注类别统计（沥青/水泥）\n" << thin_sep << std::endl;
            const auto& class_names = getFocusClassNames();
            const std::vector<int> class_order = {1, 2};

            for (int cls : class_order) {
                auto it = focus_class_metrics_.find(cls);
                if (it == focus_class_metrics_.end() || it->second.total == 0) continue;
                const auto& m = it->second;

                // z_MAE 分母 = z_error_count（IoU 交集样本数），避免被错检点稀释；除零 guard
                const double cls_z_mae_cm = m.z_error_count > 0
                    ? (m.z_error_sum / m.z_error_count) * 100.0 : 0.0;
                const double acc_pct = static_cast<double>(m.height_pass) / m.total * 100.0;
                const double pass_pct = static_cast<double>(m.combined_pass) / m.total * 100.0;

                std::cout << "\n[" << class_names.at(cls) << " (class " << cls << ")]" << std::endl;
                std::cout << "  - Z_MAE(IoU交集): " << std::fixed << std::setprecision(4) << cls_z_mae_cm
                          << " cm (基于 " << m.z_error_count << "/" << m.total << " 点)" << std::endl;
                std::cout << "  - Acc_1cm: " << std::fixed << std::setprecision(2) << acc_pct << "% ("
                          << m.height_pass << "/" << m.total << ")" << std::endl;
                std::cout << "  - Pass_Rate: " << std::fixed << std::setprecision(2) << pass_pct << "% ("
                          << m.combined_pass << "/" << m.total << ")" << std::endl;
                std::cout << "  - 最大Z误差: " << std::fixed << std::setprecision(4) << m.max_z_error * 100.0 << " cm"
                          << std::endl;
                std::cout << "      场景" << m.max_error_scene_idx << "_" << m.max_error_scene
                          << ", 帧" << m.max_error_frame_idx
                          << ", 车轮" << m.max_error_wheel << ", 点索引" << m.max_error_point_idx
                          << std::endl;
            }
        }
#endif
        std::cout << separator << std::endl;
    }

    // [3/4] 目标物评测详情（减速带 16 / 井盖 17 / 大桥接缝 23）
    if (has_any_target) {
        std::cout << "\n" << separator << "\n[3/4] 目标物评测详情（减速带/井盖/大桥接缝，按 label 展开）\n"
                  << separator << std::endl;

        const auto& class_names = getFocusClassNames();
        // 返回 mean / max / P80 / P90（单位：cm）；errors 为空时全 0
        // 分位用 nearest-rank：idx = min(size-1, floor(q*size))；样本少时 P80/P90 可能等于 max
        auto calc_stats = [](std::vector<double> errors) {
            if (errors.empty()) return std::make_tuple(0.0, 0.0, 0.0, 0.0);
            std::sort(errors.begin(), errors.end());
            const double mean = std::accumulate(errors.begin(), errors.end(), 0.0) / errors.size() * 100.0;
            const double max_val = errors.back() * 100.0;
            auto quantile = [&](double q) {
                const size_t idx = std::min(errors.size() - 1, static_cast<size_t>(std::floor(q * errors.size())));
                return errors[idx] * 100.0;
            };
            return std::make_tuple(mean, max_val, quantile(0.80), quantile(0.90));
        };
        auto print_err_line = [](const char* name, const std::tuple<double,double,double,double>& stat) {
            auto [m, mx, p80, p90] = stat;
            std::cout << "      " << name
                      << ": mean=" << std::fixed << std::setprecision(2) << m
                      << "cm  max=" << mx
                      << "cm  P80=" << p80
                      << "cm  P90=" << p90 << "cm" << std::endl;
        };
        auto print_records_by_scene = [&](const std::vector<TargetAnomalyRecord>& records, const char* header) {
            if (records.empty()) return;
            std::map<size_t, std::vector<size_t>> by_scene;
            for (const auto& r : records) by_scene[r.scene_idx].push_back(r.frame_idx);
            std::cout << "    " << header << " [" << by_scene.size() << "场景/" << records.size() << "条]:" << std::endl;
            for (const auto& [s_idx, f_list] : by_scene) {
                std::cout << "      场景" << s_idx << "_" << scenes_[s_idx].scene_name << ": ";
                for (const size_t f : f_list) {
                    if (f == SIZE_MAX) std::cout << "N/A ";
                    else               std::cout << f << " ";
                }
                std::cout << std::endl;
            }
        };
        auto print_false_positive_records = [&](const std::vector<FalsePositiveRecord>& records) {
            if (records.empty()) return;
            const std::array<const char*, 4> wheel_names = {"FL", "FR", "RL", "RR"};
            std::cout << "    误检 (FP) 详细诊断 [" << records.size() << "条]:" << std::endl;
            for (const auto& r : records) {
                std::cout << "      ";
                if (r.scene_idx < scenes_.size()) {
                    std::cout << "场景" << r.scene_idx << "_" << scenes_[r.scene_idx].scene_name;
                } else {
                    std::cout << "场景N/A";
                }
                std::cout << " / 帧";
                if (r.frame_idx == SIZE_MAX) std::cout << "N/A";
                else                          std::cout << r.frame_idx;
                std::cout << " / 车轮";
                if (r.wheel_idx >= 0 && r.wheel_idx < static_cast<int>(wheel_names.size())) {
                    std::cout << wheel_names[r.wheel_idx];
                } else {
                    std::cout << r.wheel_idx;
                }
                std::cout << " / label=" << r.label << " / "
                          << (r.label == 16 ? "x_peak=" : "x_leading=")
                          << std::fixed << std::setprecision(3) << r.reference_x << "m / 最近GT Δx=";
                if (r.has_nearest_gt) {
                    std::cout << std::fixed << std::setprecision(3) << r.nearest_gt_delta_x << "m";
                } else {
                    std::cout << "N/A（同帧同车轮无同 label GT）";
                }
                std::cout << std::endl;
            }
        };

        for (const auto& [lbl, s] : focus_target_stats_) {
            const bool has_any = (s.matched_bumps > 0 || s.false_positive > 0 || s.false_negative > 0);
            if (!has_any) continue;

            const auto name_it = class_names.find(lbl);
            const std::string name = (name_it != class_names.end()) ? name_it->second : ("label_" + std::to_string(lbl));

            std::cout << "\n" << thin_sep << std::endl;
            std::cout << "--- " << name << " (label=" << lbl << ") ---" << std::endl;

            // ── 目标级 ──
            std::cout << "  [目标级]" << std::endl;
            std::cout << "    预测: " << s.total_pred_bumps
                      << ", 真值: " << s.total_gt_bumps
                      << ", 匹配: " << s.matched_bumps
                      << ", 误检: " << s.false_positive
                      << ", 漏检: " << s.false_negative << std::endl;
            if (s.total_pred_bumps > 0 && s.total_gt_bumps > 0) {
                const double prec = static_cast<double>(s.matched_bumps) / s.total_pred_bumps * 100.0;
                const double rec  = static_cast<double>(s.matched_bumps) / s.total_gt_bumps * 100.0;
                const double fa   = static_cast<double>(s.false_positive) / s.total_pred_bumps * 100.0;
                std::cout << "    精确率: " << std::fixed << std::setprecision(2) << prec << "%"
                          << ", 召回率: " << rec << "%"
                          << ", 误检率: " << fa << "%" << std::endl;
            }

            // 误差 mean/max/P80/P90（按 label 语义选字段）
            // 减速带 16：极值点 x_peak / 极值点 z_peak / 宽度
            // 井盖 17 & 大桥接缝 23：前缘点 x_leading / 段均值 z_mean / 宽度
            if (s.matched_bumps > 0) {
                std::cout << "    误差 (基于 " << s.matched_bumps << " 个匹配):" << std::endl;
                if (lbl == 16) {
                    print_err_line("纵向x_peak   ", calc_stats(s.x_peak_errors));
                    print_err_line("高度z_peak   ", calc_stats(s.z_peak_errors));
                } else {
                    print_err_line("纵向x_leading", calc_stats(s.x_leading_errors));
                    print_err_line("高度z_mean   ", calc_stats(s.z_mean_errors));
                }
                print_err_line("宽度         ", calc_stats(s.width_errors));
            }

            // 中误差占比（label=16 有 x + z；17/23 只有 x）
            if (s.matched_bumps > 0) {
                std::cout << "    中误差占比: 纵向>" << TARGET_X_ERR_MID*100 << "cm: "
                          << std::fixed << std::setprecision(2)
                          << static_cast<double>(s.mid_x_error_count) / s.matched_bumps * 100.0 << "% ("
                          << s.mid_x_error_count << "/" << s.matched_bumps << ")";
                if (lbl == 16) {
                    std::cout << ", 高度>" << TARGET_Z_ERR_MID*100 << "cm: "
                              << static_cast<double>(s.mid_z_error_count) / s.matched_bumps * 100.0 << "% ("
                              << s.mid_z_error_count << "/" << s.matched_bumps << ")";
                }
                std::cout << std::endl;
            }

            // ── 异常告警 ──
            std::cout << "  [异常告警]" << std::endl;
            if (lbl == 16) {
                print_records_by_scene(s.large_x_err_records, "目标级 纵向x_peak>20cm");
                print_records_by_scene(s.large_z_err_records, "目标级 高度z_peak>5cm");
            } else {
                print_records_by_scene(s.large_x_err_records, "目标级 前缘x_leading>20cm");
            }
            print_records_by_scene(s.large_w_err_records, "目标级 宽度>20cm");
            print_false_positive_records(s.fp_records);
            print_records_by_scene(s.fn_records, "漏检 (FN)");
        }

        std::cout << separator << std::endl;
    }

    // [4/4] 视觉分类详情
    if (eval_vision_ && vision_map_calculator_) {
        std::cout << "\n" << separator << "\n[4/4] 视觉分类详情\n" << separator << std::endl;

        if (vision_metrics.empty()) {
            std::cout << "  视觉分类评测器无统计数据" << std::endl;
        } else {
            const auto format_metric = [](double value, bool available) {
                if (!available) return std::string("N/A");
                std::ostringstream stream;
                stream << std::fixed << std::setprecision(4) << value;
                return stream.str();
            };

            for (const auto& [task, configured_classes] : vision_head_order_) {
                const auto metrics_it = vision_metrics.find(task);
                if (metrics_it == vision_metrics.end()) continue;
                const auto& head_metrics = metrics_it->second;

                std::cout << "\n" << thin_sep << std::endl;
                std::cout << "--- " << task << " ---" << std::endl;
                if (head_metrics.valid_sample_count == 0) {
                    std::cout << "  无有效样本"
                              << "（缺少预测/GT配对: " << head_metrics.skipped_missing_pair_count
                              << "，logits无效: " << head_metrics.skipped_invalid_logits_count
                              << "，GT标签越界: " << head_metrics.skipped_invalid_label_count << "）"
                              << std::endl;
                    continue;
                }

                std::cout << "  mAP: " << std::fixed << std::setprecision(4) << head_metrics.map
                          << " | 有效样本: " << head_metrics.valid_sample_count
                          << " | GT覆盖类别: " << head_metrics.gt_covered_class_count << "/" << configured_classes
                          << " | Top-1 Accuracy: " << std::setprecision(2)
                          << head_metrics.top1_accuracy * 100.0 << "%" << std::endl;
                std::cout << "  跳过样本: 缺少预测/GT配对=" << head_metrics.skipped_missing_pair_count
                          << "，logits无效=" << head_metrics.skipped_invalid_logits_count
                          << "，GT标签越界=" << head_metrics.skipped_invalid_label_count << std::endl;

                std::cout << "\n" << std::left
                          << std::setw(14) << "Class"
                          << std::setw(12) << "AP"
                          << std::setw(12) << "Precision"
                          << std::setw(12) << "Recall"
                          << std::setw(12) << "GT_Count"
                          << std::setw(12) << "TP_Count"
                          << std::setw(12) << "Pred_Count" << std::endl;
                std::cout << std::string(86, '-') << std::endl;

                for (size_t cls = 0; cls < head_metrics.class_metrics.size(); ++cls) {
                    const auto& class_metrics = head_metrics.class_metrics[cls];
                    std::cout << std::left
                              << std::setw(14) << ("class_" + std::to_string(cls))
                              << std::setw(12) << format_metric(class_metrics.ap, class_metrics.has_ap)
                              << std::setw(12) << format_metric(class_metrics.precision, class_metrics.has_precision)
                              << std::setw(12) << format_metric(class_metrics.recall, class_metrics.has_recall)
                              << std::setw(12) << class_metrics.gt_count
                              << std::setw(12) << class_metrics.tp_count
                              << std::setw(12) << class_metrics.pred_count << std::endl;
                }

                std::cout << "\n  [异常告警]" << std::endl;
                if (head_metrics.classification_alerts.empty()) {
                    std::cout << "    无 Top-1 分类错误" << std::endl;
                    continue;
                }

                std::cout << "    Top-1 分类错误 [" << head_metrics.classification_alerts.size()
                          << "条]：每条同时计为预测类 FP 和 GT 类 FN" << std::endl;
                for (const auto& alert : head_metrics.classification_alerts) {
                    std::cout << "      ";
                    if (alert.context.scene_idx < scenes_.size()) {
                        std::cout << "场景" << alert.context.scene_idx << "_"
                                  << scenes_[alert.context.scene_idx].scene_name;
                    } else {
                        std::cout << "场景N/A";
                    }
                    std::cout << " / 帧";
                    if (alert.context.frame_idx == SIZE_MAX) std::cout << "N/A";
                    else                                     std::cout << alert.context.frame_idx;
                    std::cout << " / ts=" << alert.context.timestamp_ms << "ms"
                              << " / 预测=class_" << alert.top1_class
                              << " (FP, P=" << std::fixed << std::setprecision(4)
                              << alert.top1_probability << ")"
                              << " / GT=class_" << alert.gt_class
                              << " (FN, P=" << alert.gt_probability << ")"
                              << " / 概率差=+" << alert.probability_margin << " / 原因=";
                    if (alert.top1_probability < 0.50) {
                        std::cout << "低置信度错误：模型最高类别概率不足 0.50";
                    } else if (alert.probability_margin < 0.10) {
                        std::cout << "边界混淆：预测类与 GT 类概率差小于 0.10";
                    } else {
                        std::cout << "高置信度错误：预测类概率明显高于 GT 类";
                    }
                    std::cout << std::endl;
                }
            }

            std::cout << "\nOverall mAP: ";
            if (vision_valid_count > 0) {
                std::cout << std::fixed << std::setprecision(4) << vision_overall_map
                          << " (有效视觉头: " << vision_valid_count << "/" << vision_metrics.size() << ")";
            } else {
                std::cout << "N/A（无有效视觉头）";
            }
            std::cout << std::endl;
            std::cout << "视觉帧样本数: " << vision_map_calculator_->get_current_count() << std::endl;
        }
        std::cout << separator << std::endl;
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // 第三层：诊断信息（附录）
    // ═══════════════════════════════════════════════════════════════════════════

    std::cout << "\n" << separator << "\n[附录A] 任务覆盖率与帧完成度\n" << separator << std::endl;
    std::cout << "评测场景数: " << scenes_.size() << (max_scenes_for_debug_ > 0 ? " (调试模式)" : " (全量模式)") << std::endl;
    std::cout << "候选预测帧: " << received_samples_ << std::endl;
    std::cout << "进入真值匹配流程: " << evaluation_samples_ << std::endl;
    std::cout << "至少完成一项任务: " << completed_samples_ << std::endl;
    std::cout << "未匹配真值时间戳: " << skipped_no_gt_samples_ << std::endl;
    std::cout << "匹配后无可评任务: " << skipped_all_tasks_samples_ << std::endl;

    std::cout << "\n任务覆盖率（每项 KPI 仅使用该任务的完成评测帧）:" << std::endl;
    std::cout << "任务 | 预测可用 | GT可用 | 完成评测 | 缺预测 | 缺GT" << std::endl;
    const auto print_task_stats = [](const char* name, const TaskEvaluationStats& stats) {
        std::cout << name << " | " << stats.prediction_available
                  << " | " << stats.ground_truth_available
                  << " | " << stats.evaluated
                  << " | " << stats.skipped_prediction_missing
                  << " | " << stats.skipped_ground_truth_missing << std::endl;
    };
    if (eval_road_) print_task_stats("路面", road_task_stats_);
    if (eval_traj_) print_task_stats("轨迹/目标物", traj_task_stats_);
    if (eval_vision_) print_task_stats("视觉", vision_task_stats_);

    std::cout << "\n各场景完成度（至少完成一项任务的帧数/真值样本目录数）:" << std::endl;
    for (size_t i = 0; i < scenes_.size(); ++i) {
        const auto& scene = scenes_[i];
        std::cout << "  [" << (i+1) << "] " << scene.scene_name << ": "
                  << scene.completed_samples << "/" << scene.total_samples;
        if (scene.completed_samples == scene.total_samples) {
            std::cout << " ✓";
        } else {
            const int diff = static_cast<int>(scene.total_samples) - static_cast<int>(scene.completed_samples);
            std::cout << " ⚠ (差异: " << diff << ")";
        }
        std::cout << std::endl;
    }

    // [附录B] 内存与容量诊断
    bool has_memory_diag = (eval_traj_ && max_dense_cloud_points_ > 0) ||
                           max_target_points_size_ > 0 || max_target_errors_size_ > 0;
    if (has_memory_diag) {
        std::cout << "\n" << separator << "\n[附录B] 内存与容量诊断\n" << separator << std::endl;

        if (eval_traj_ && max_dense_cloud_points_ > 0) {
            std::cout << "\n稠密点云:" << std::endl;
            std::cout << "  单帧最大点数: " << std::left << std::setw(10) << max_dense_cloud_points_
                      << " / 配置容量: " << std::left << std::setw(10) << dense_cloud_reserve_capacity_;
            if (max_dense_cloud_points_ > dense_cloud_reserve_capacity_) {
                std::cout << " ⚠️  不足";
            } else {
                std::cout << " ✓";
            }
            std::cout << std::endl;

            if (max_filtered_indices_size_ > 0) {
                std::cout << "  ROI过滤后最大: " << std::left << std::setw(10) << max_filtered_indices_size_
                          << " / 固定容量: " << std::left << std::setw(10) << filtered_indices_.capacity();
                if (max_filtered_indices_size_ > filtered_indices_.capacity()) {
                    std::cout << " ⚠️  不足";
                } else {
                    std::cout << " ✓";
                }
                std::cout << std::endl;
            }

            if (max_neighbor_z_size_ > 0) {
                std::cout << "  邻域最大数量: " << std::left << std::setw(10) << max_neighbor_z_size_
                          << " / 配置容量: " << std::left << std::setw(10) << neighbor_reserve_capacity_;
                if (max_neighbor_z_size_ > neighbor_reserve_capacity_) {
                    std::cout << " ⚠️  不足";
                } else {
                    std::cout << " ✓";
                }
                std::cout << std::endl;
            }
        }

        if (max_target_points_size_ > 0) {
            std::cout << "\n目标物段缓冲:" << std::endl;
            std::cout << "  单个最大点数: " << std::left << std::setw(10) << max_target_points_size_
                      << " / 容量:     " << std::left << std::setw(10) << target_points_buffer_.capacity();
            if (max_target_points_size_ > target_points_buffer_.capacity()) {
                std::cout << " ⚠️  不足";
            } else {
                std::cout << " ✓";
            }
            std::cout << std::endl;
        }

        if (max_target_errors_size_ > 0) {
            std::cout << "  误差容器使用: " << std::left << std::setw(10) << max_target_errors_size_
                      << " / 配置容量: " << std::left << std::setw(10) << TARGET_ERRORS_RESERVE;
            if (max_target_errors_size_ > TARGET_ERRORS_RESERVE) {
                std::cout << " ⚠️  不足";
            } else {
                std::cout << " ✓";
            }
            std::cout << std::endl;
        }
    }

    // [附录C] 性能统计
    if (!evaluation_times_.empty()) {
        const double min_time = *std::min_element(evaluation_times_.begin(), evaluation_times_.end());
        const double max_time = *std::max_element(evaluation_times_.begin(), evaluation_times_.end());

        std::cout << "\n" << separator << "\n[附录C] 性能统计\n" << separator << std::endl;
        std::cout << "完成评测帧数（至少完成一项）: " << evaluation_times_.size() << std::endl;
        if (eval_traj_) std::cout << "轨迹/目标物完成次数: " << traj_task_stats_.evaluated << std::endl;
        if (eval_vision_) std::cout << "视觉完成次数: " << vision_task_stats_.evaluated << std::endl;
        if (eval_road_) std::cout << "路面完成次数: " << road_task_stats_.evaluated << std::endl;
        std::cout << "平均评测时间: " << std::fixed << std::setprecision(2) << perf_avg_ms << " ms" << std::endl;
        std::cout << "最小评测时间: " << min_time << " ms" << std::endl;
        std::cout << "最大评测时间: " << max_time << " ms" << std::endl;
        std::cout << separator << std::endl;
    }

    std::cout << "\n所有场景评测完成。" << std::endl;

    // 恢复 cout 并关闭报告文件
    std::cout.rdbuf(orig_buf);
    if (report_file.is_open()) {
        report_file.close();
        LOG(INFO) << " 评测报告已保存: " << fs::absolute(report_path).string();
    }
}
