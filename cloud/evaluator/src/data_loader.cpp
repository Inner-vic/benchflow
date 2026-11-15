#include "evaluator.hpp"
#include "compat/logging_compat.hpp"
#include <filesystem>
#include <iomanip>
#include <yaml-cpp/yaml.h>

namespace fs = std::filesystem;

// ===========================================================================================
// 真值加载
// ===========================================================================================
bool OfflineEvaluator::loadRoadSurfaceGroundTruth(const SceneInfo& current_scene, size_t sample_idx_in_scene, std::vector<int64_t>& occ_labels)
{
    try {
        if (sample_idx_in_scene >= current_scene.sample_folder_paths.size()) {
            LOG(ERROR) << "[OfflineEvaluator] 样本索引越界: " << sample_idx_in_scene
                      << " >= " << current_scene.sample_folder_paths.size();
            return false;
        }

        fs::path sample_dir = current_scene.sample_folder_paths[sample_idx_in_scene];

        if (!fs::exists(sample_dir)) {
            LOG(ERROR) << "[OfflineEvaluator] 样本文件夹不存在: " << sample_dir;
            return false;
        }

        // 查找occ真值文件（occ*.npy）
        std::string occ_file_path;
        for (const auto& entry : fs::directory_iterator(sample_dir)) {
            std::string filename = entry.path().filename().string();
            if (filename.find("occ") == 0 && filename.rfind(".npy") == filename.length() - 4) {
                occ_file_path = entry.path().string();
                break;
            }
        }

        if (occ_file_path.empty()) {
            LOG(ERROR) << "[OfflineEvaluator] 未找到occ真值文件: " << sample_dir;
            return false;
        }

        std::ifstream occ_file(occ_file_path, std::ios::binary);
        if (!occ_file.is_open()) {
            LOG(ERROR) << "[OfflineEvaluator] 无法打开occ_file_path文件: " << occ_file_path;
            return false;
        }

        // 跳过NPY文件头: magic(6) + version(2) + header_len(2) + header_dict
        occ_file.seekg(8);
        uint16_t npy_header_len;
        occ_file.read(reinterpret_cast<char*>(&npy_header_len), 2);
        occ_file.seekg(10 + npy_header_len);

        // 读取数据区（float32格式）
        size_t data_offset = occ_file.tellg();
        occ_file.seekg(0, std::ios::end);
        size_t data_bytes = static_cast<size_t>(occ_file.tellg()) - data_offset;
        occ_file.seekg(data_offset);

        size_t float_count = data_bytes / sizeof(float);
        std::vector<float> raw_floats(float_count);
        occ_file.read(reinterpret_cast<char*>(raw_floats.data()), data_bytes);
        occ_file.close();

        // 转换为int64_t
        occ_labels.resize(float_count);
        for (size_t i = 0; i < float_count; ++i) {
            occ_labels[i] = static_cast<int64_t>(raw_floats[i]);
        }

        LOG(INFO) << "  ✓ 加载路面真值: " << (float_count / 5) << " 个体素";

        return true;

    } catch (const std::exception& e) {
        LOG(ERROR) << "[OfflineEvaluator] loadRoadSurfaceGroundTruth异常: " << e.what();
        return false;
    }
}

bool OfflineEvaluator::loadDensePointCloud(const SceneInfo& current_scene, size_t sample_idx_in_scene, DensePointCloud& dense_cloud)
{
    try {
        if (sample_idx_in_scene >= current_scene.sample_folder_paths.size()) {
            LOG(ERROR) << "[OfflineEvaluator] 样本索引越界: " << sample_idx_in_scene
                      << " >= " << current_scene.sample_folder_paths.size();
            return false;
        }

        fs::path sample_dir = current_scene.sample_folder_paths[sample_idx_in_scene];

        if (!fs::exists(sample_dir)) {
            LOG(ERROR) << "[OfflineEvaluator] 样本文件夹不存在: " << sample_dir;
            return false;
        }

        // 查找稠密点云文件（dense_labeled_*.npy）
        std::string dense_file_path;
        for (const auto& entry : fs::directory_iterator(sample_dir)) {
            std::string filename = entry.path().filename().string();
            if (filename.find("dense_labeled_") == 0 && filename.rfind(".npy") == filename.length() - 4) {
                dense_file_path = entry.path().string();
                break;
            }
        }

        if (dense_file_path.empty()) {
            LOG(WARNING) << "[OfflineEvaluator] 未找到稠密点云文件（dense_labeled_*.npy）: " << sample_dir;
            return false;
        }

        std::ifstream file(dense_file_path, std::ios::binary);
        if (!file.is_open()) {
            LOG(ERROR) << "[OfflineEvaluator] 无法打开稠密点云文件: " << dense_file_path;
            return false;
        }

        // 跳过NPY文件头（v1.0格式：magic(6) + version(2) + header_len(2) + header_dict）
        file.seekg(8);
        uint16_t npy_header_len;
        file.read(reinterpret_cast<char*>(&npy_header_len), 2);
        if (!file) {
            LOG(ERROR) << "  NPY文件头读取失败: " << dense_file_path;
            file.close();
            return false;
        }
        file.seekg(10 + npy_header_len);

        // 读取数据区（float32格式，每个点5维：x, y, z, label, drivable_flag）
        size_t data_offset = file.tellg();
        file.seekg(0, std::ios::end);
        size_t data_bytes = static_cast<size_t>(file.tellg()) - data_offset;
        file.seekg(data_offset);

        size_t float_count = data_bytes / sizeof(float);
        if (float_count % 5 != 0) {
            LOG(ERROR) << "[OfflineEvaluator] 稠密点云数据维度异常（需5维）: " << float_count;
            file.close();
            return false;
        }

        // 检查空数据
        if (float_count == 0) {
            LOG(ERROR) << "[OfflineEvaluator] 稠密点云数据为空: " << dense_file_path;
            file.close();
            return false;
        }

        // 预分配内存（避免频繁扩容）
        dense_cloud.num_points = float_count / 5;

        // 统计最大点数（用于调整reserve_capacity参考）
        if (dense_cloud.num_points > max_dense_cloud_points_) {
            max_dense_cloud_points_ = dense_cloud.num_points;
        }

        if (dense_cloud.raw_data.capacity() < float_count) {
            // dense_cloud_reserve_capacity_是点数，需要转换为float数量（5维数据）
            size_t reserve_float_count = dense_cloud_reserve_capacity_ * 5;
            dense_cloud.raw_data.reserve(std::max(float_count, reserve_float_count));
        }
        dense_cloud.raw_data.resize(float_count);

        file.read(reinterpret_cast<char*>(dense_cloud.raw_data.data()), data_bytes);
        if (!file) {
            LOG(ERROR) << "  稠密点云数据读取失败: " << dense_file_path;
            file.close();
            return false;
        }
        file.close();

        LOG(INFO) << "  ✓ 加载稠密点云: " << dense_cloud.num_points << " 个点 ("
                  << std::fixed << std::setprecision(2) << (data_bytes / 1024.0 / 1024.0) << " MB)";

        return true;

    } catch (const std::exception& e) {
        LOG(ERROR) << "[OfflineEvaluator] loadDensePointCloud异常: " << e.what();
        return false;
    }
}

bool OfflineEvaluator::loadVisionClassificationGroundTruth(const SceneInfo& current_scene, size_t sample_idx_in_scene, std::map<std::string, int>& vision_labels)
{
    try {
        vision_labels.clear();
        if (sample_idx_in_scene >= current_scene.sample_folder_paths.size()) {
            LOG(ERROR) << "[OfflineEvaluator] 样本索引越界: " << sample_idx_in_scene
                      << " >= " << current_scene.sample_folder_paths.size();
            return false;
        }

        fs::path sample_dir = current_scene.sample_folder_paths[sample_idx_in_scene];
        if (!fs::exists(sample_dir)) {
            LOG(ERROR) << "[OfflineEvaluator] 样本文件夹不存在: " << sample_dir;
            return false;
        }

        std::string img_file_path;
        for (const auto& entry : fs::directory_iterator(sample_dir)) {
            std::string filename = entry.path().filename().string();
            if (filename.rfind(".json") == filename.length() - 5) {
                img_file_path = entry.path().string();
                break;
            }
        }
        if (img_file_path.empty()) {
            LOG(ERROR) << "[OfflineEvaluator] 未找到视觉分类真值文件: " << sample_dir;
            return false;
        }

        std::ifstream label_file(img_file_path);
        if (!label_file.is_open()) {
            LOG(ERROR) << "[OfflineEvaluator] 无法打开 json: " << img_file_path;
            return false;
        }
        YAML::Node json_data = YAML::Load(label_file);
        label_file.close();

        // 按 vision_class_counts_ 列出的 head name 从 JSON 取值，缺失记 WARNING
        for (const auto& kv : json_data) {
            std::string image_key = kv.first.as<std::string>();
            if (image_key.find(".jpg") == std::string::npos || !kv.second["label"]) continue;
            auto label_node = kv.second["label"];
            for (const auto& [name, _C] : vision_class_counts_) {
                if (!label_node[name]) {
                    LOG(WARNING) << "  GT 缺少视觉头: " << name;
                    continue;
                }
                try {
                    vision_labels[name] = label_node[name].as<int32_t>();
                } catch (const YAML::BadConversion& e) {
                    LOG(WARNING) << "  字段 '" << name << "' 类型转换失败: " << e.what();
                }
            }
            break;
        }

        LOG(INFO) << "  加载视觉分类真值: " << vision_labels.size() << " 个标签";
        return true;
    } catch (const std::exception& e) {
        LOG(ERROR) << "[OfflineEvaluator] loadVisionClassificationGroundTruth异常: " << e.what();
        return false;
    }
}

OfflineEvaluator::GroundTruthLoadStatus OfflineEvaluator::loadGroundTruthByTimestamp(
    uint64_t pred_timestamp_ms, std::vector<int64_t>& occ_labels,
    DensePointCloud& dense_cloud, std::map<std::string, int>& vision_labels)
{
    GroundTruthLoadStatus status;
    try {
        if (current_scene_idx_ >= scenes_.size()) {
            LOG(ERROR) << "  场景索引无效: " << current_scene_idx_ << " >= " << scenes_.size();
            return status;
        }

        // 场景切换逻辑：预测时间戳超出当前场景真值时间范围。
        // 仅视觉评测时严格按图像时间戳切换；其他模式保留 5 秒切换阈值。
        if (pred_timestamp_ms > scenes_[current_scene_idx_].max_timestamp_ms) {
            const uint64_t time_diff = pred_timestamp_ms - scenes_[current_scene_idx_].max_timestamp_ms;
            const uint64_t SCENE_SWITCH_THRESHOLD_MS = 5000;
            const bool should_switch = (!eval_road_ && !eval_traj_) || (time_diff > SCENE_SWITCH_THRESHOLD_MS);

            if (!should_switch) {
                LOG(INFO) << "  预测时间戳(帧首)超出真值最大时间戳(帧首) " << time_diff
                          << " ms (<5s)，继续查找真值";
            } else {
                if (!eval_road_ && !eval_traj_) {
                    LOG(INFO) << "  [VisionOnly] 时间戳超出场景 " << time_diff << " ms，立即切换下一场景";
                } else {
                    LOG(INFO) << "  时间戳超出场景 " << time_diff << " ms (>5s)，切换下一场景";
                }

                if (current_scene_idx_ + 1 >= scenes_.size()) {
                    LOG(INFO) << "  所有场景已评测完毕（时间戳超出最后场景）";
                    all_scenes_completed_ = true;
                    return status;
                }

                if (eval_traj_) flushTargetStatsForScene();
                current_scene_idx_++;
                LOG(INFO) << "\n========== 切换场景 ==========";
                LOG(INFO) << " 场景 [" << (current_scene_idx_ + 1) << "/" << scenes_.size() << "]: "
                          << scenes_[current_scene_idx_].scene_name;
                LOG(INFO) << " 时间戳范围: [" << scenes_[current_scene_idx_].min_timestamp_ms
                          << " - " << scenes_[current_scene_idx_].max_timestamp_ms << "] ms";
                LOG(INFO) << "================================\n";

                if (eval_traj_) {
                    traj_postprocess_manager_.resetSceneState();
                    LOG(INFO) << " 评测器历史状态已重置";
                }

                if (pred_timestamp_ms < scenes_[current_scene_idx_].min_timestamp_ms ||
                    pred_timestamp_ms > scenes_[current_scene_idx_].max_timestamp_ms) {
                    LOG(WARNING) << "  预测时间戳不在新场景范围内，跳过";
                    return status;
                }
            }
        }

        const SceneInfo& current_scene = scenes_[current_scene_idx_];
        const uint64_t TOLERANCE_MS = (!eval_road_ && !eval_traj_) ? 0 : 10;
        uint64_t best_gt_timestamp = 0;
        uint64_t min_time_diff = UINT64_MAX;

        for (const auto& [gt_timestamp, sample_idx] : current_scene.timestamp_to_sample_idx) {
            const uint64_t time_diff = (gt_timestamp > pred_timestamp_ms)
                ? (gt_timestamp - pred_timestamp_ms) : (pred_timestamp_ms - gt_timestamp);
            if (time_diff < min_time_diff) {
                min_time_diff = time_diff;
                best_gt_timestamp = gt_timestamp;
                if (time_diff == 0) break;
            }
        }

        if (min_time_diff > TOLERANCE_MS) {
            LOG(WARNING) << "  未找到匹配真值: 帧首=" << pred_timestamp_ms
                         << " ms, 最近时间差=" << min_time_diff
                         << " ms (超过容差 " << TOLERANCE_MS << " ms)";
            if (current_scene_idx_ == scenes_.size() - 1 &&
                pred_timestamp_ms > current_scene.max_timestamp_ms) {
                LOG(INFO) << "  最后一个场景已评测完毕";
                all_scenes_completed_ = true;
            }
            return status;
        }

        const size_t gt_sample_idx = current_scene.timestamp_to_sample_idx.at(best_gt_timestamp);
        status.timestamp_matched = true;
        status.sample_idx = gt_sample_idx;
        LOG(INFO) << "  ✓ 匹配真值: 预测帧首=" << pred_timestamp_ms << " ms, 真值帧首="
                  << best_gt_timestamp << " ms, 时间差=" << min_time_diff
                  << " ms, 样本索引=" << gt_sample_idx;

        // 固定顺序独立加载各任务真值，单项失败不影响其他任务。
        if (eval_road_) {
            status.road_ready = loadRoadSurfaceGroundTruth(current_scene, gt_sample_idx, occ_labels);
        }
        if (eval_traj_) {
            status.traj_ready = loadDensePointCloud(current_scene, gt_sample_idx, dense_cloud);
        }
        if (eval_vision_) {
            status.vision_ready = loadVisionClassificationGroundTruth(current_scene, gt_sample_idx, vision_labels)
                               && !vision_labels.empty();
            if (!status.vision_ready) {
                LOG(WARNING) << "  视觉真值不可用：缺少标签文件或配置视觉头标签";
            }
        }

        LOG(INFO) << "  真值任务状态: occ=" << (!eval_road_ ? "未启用" : status.road_ready ? "可用" : "缺失")
                  << ", dense=" << (!eval_traj_ ? "未启用" : status.traj_ready ? "可用" : "缺失")
                  << ", vision=" << (!eval_vision_ ? "未启用" : status.vision_ready ? "可用" : "缺失");
        return status;

    } catch (const std::exception& e) {
        LOG(ERROR) << "  loadGroundTruthByTimestamp异常: " << e.what();
        return status;
    }
}

// ===========================================================================================
// 场景管理函数
// ===========================================================================================
bool OfflineEvaluator::initializeScenes()
{
    try {
        LOG(INFO) << " OfflineEvaluator: 初始化场景列表，扫描目录: " << labeled_data_root_;

        // 检查真值目录，获取所有场景文件夹
        if (!fs::exists(labeled_data_root_) || !fs::is_directory(labeled_data_root_)) {
            LOG(ERROR) << " 真值目录不存在: " << labeled_data_root_;
            return false;
        }

        // 收集所有场景文件夹（以配置的前缀开头的文件夹）
        for (const auto& entry : fs::directory_iterator(labeled_data_root_)) {
            if (entry.is_directory()) {
                std::string scene_name = entry.path().filename().string();
                // 过滤：只保留以指定前缀开头的场景文件夹
                if (scene_name.find(scene_folder_prefix_) == 0) {
                    SceneInfo scene;
                    scene.scene_name = scene_name;
                    scene.total_samples = getSampleCountInScene(scene_name);
                    scene.completed_samples = 0;

                    if (scene.total_samples > 0) {
                        // 构建时间戳映射
                        if (!buildTimestampMapForScene(scene)) {
                            LOG(WARNING) << "   ⊗ 场景 " << scene_name << " 时间戳映射构建失败，跳过";
                            continue;
                        }
                        scenes_.push_back(scene);
                    } else {
                        LOG(WARNING) << "   ⊗ 场景 " << scene_name << " 没有样本，跳过";
                    }
                }
            }
        }

        // 按场景名称排序（确保评测顺序一致）
        std::sort(scenes_.begin(), scenes_.end(),
            [](const SceneInfo& a, const SceneInfo& b) {
                return a.scene_name < b.scene_name;
            });

        if (scenes_.empty()) {
            LOG(ERROR) << " 未找到任何有效场景";
            return false;
        }

        // 调试模式：限制场景数量
        if (max_scenes_for_debug_ > 0 && scenes_.size() > max_scenes_for_debug_) {
            scenes_.resize(max_scenes_for_debug_);
            LOG(INFO) << " ⚠️  调试模式：已限制为前 " << max_scenes_for_debug_ << " 个场景";
        }

        LOG(INFO) << " 场景列表初始化完成，共 " << scenes_.size() << " 个场景:";
        for (size_t i = 0; i < scenes_.size(); ++i) {
            LOG(INFO) << "   [" << i << "] " << scenes_[i].scene_name
                      << " (" << scenes_[i].total_samples << " 样本, 时间戳范围: "
                      << scenes_[i].min_timestamp_ms << " - " << scenes_[i].max_timestamp_ms << " ms)";
        }

        current_scene_idx_ = 0;
        LOG(INFO) << " 当前场景: " << scenes_[current_scene_idx_].scene_name;

    return true;

    } catch (const std::exception& e) {
        LOG(ERROR) << " initializeScenes异常: " << e.what();
        return false;
    }
}

size_t OfflineEvaluator::getSampleCountInScene(const std::string& scene_name)
{
    try {
        // 扫描场景文件夹，统计样本数量
        // 优先查找 <scene>/dataset/ 子目录，兼容新数据集结构
        fs::path scene_path = fs::path(labeled_data_root_) / scene_name;
        fs::path dataset_sub = scene_path / "dataset";
        if (fs::exists(dataset_sub) && fs::is_directory(dataset_sub)) {
            scene_path = dataset_sub;
        }

        if (!fs::exists(scene_path) || !fs::is_directory(scene_path)) {
            LOG(WARNING) << " 场景文件夹不存在: " << scene_path;
            return 0;
        }

        // 统计场景文件夹下的子文件夹数量（每个子文件夹对应一个样本）
        size_t count = 0;
        for (const auto& entry : fs::directory_iterator(scene_path)) {
            if (entry.is_directory()) {
                count++;
            }
        }

        return count;

    } catch (const std::exception& e) {
        LOG(ERROR) << " getSampleCountInScene异常: " << e.what();
        return 0;
    }
}

bool OfflineEvaluator::buildTimestampMapForScene(SceneInfo& scene)
{
    try {
        LOG(INFO) << "   构建场景时间戳映射: " << scene.scene_name;

        fs::path scene_path = fs::path(labeled_data_root_) / scene.scene_name;
        fs::path dataset_sub = scene_path / "dataset";
        if (fs::exists(dataset_sub) && fs::is_directory(dataset_sub)) {
            scene_path = dataset_sub;
        }

        if (!fs::exists(scene_path) || !fs::is_directory(scene_path)) {
            LOG(ERROR) << "   场景文件夹不存在: " << scene_path;
            return false;
        }

        // 收集所有样本文件夹
        std::vector<std::string> sample_folders;
        for (const auto& entry : fs::directory_iterator(scene_path)) {
            if (entry.is_directory()) {
                sample_folders.push_back(entry.path().filename().string());
            }
        }

        // 按数字排序
        std::sort(sample_folders.begin(), sample_folders.end(),
            [](const std::string& a, const std::string& b) {
                try {
                    return std::stoull(a) < std::stoull(b);
                } catch (...) {
                    return a < b;
                }
            });

        // 缓存样本文件夹路径（避免重复排序，提升性能约400倍）
        scene.sample_folder_paths.reserve(sample_folders.size());
        for (const auto& folder_name : sample_folders) {
            scene.sample_folder_paths.push_back((scene_path / folder_name).string());
        }

        // 解析时间戳并构建映射
        std::vector<uint64_t> timestamps;
        for (size_t i = 0; i < sample_folders.size(); ++i) {
            try {
                std::string folder_name = sample_folders[i];

                // 文件夹名格式：序号_时间戳ms（如 7_1761358529876）
                size_t underscore_pos = folder_name.find('_');
                uint64_t timestamp_ms;

                if (underscore_pos != std::string::npos) {
                    timestamp_ms = std::stoull(folder_name.substr(underscore_pos + 1));
                } else {
                    timestamp_ms = std::stoull(folder_name);
                }

                scene.timestamp_to_sample_idx[timestamp_ms] = i;
                timestamps.push_back(timestamp_ms);
            } catch (const std::exception& e) {
                LOG(WARNING) << "   解析文件夹名失败: " << sample_folders[i] << ", " << e.what();
                continue;
            }
        }

        if (timestamps.empty()) {
            LOG(ERROR) << "   未找到有效时间戳";
            return false;
        }

        // 记录时间戳范围
        auto [min_it, max_it] = std::minmax_element(timestamps.begin(), timestamps.end());
        scene.min_timestamp_ms = *min_it;
        scene.max_timestamp_ms = *max_it;

        LOG(INFO) << "   ✓ 时间戳映射完成: " << timestamps.size() << " 个样本, "
                  << "范围 [" << scene.min_timestamp_ms << " - " << scene.max_timestamp_ms << "] ms";

        return true;

    } catch (const std::exception& e) {
        LOG(ERROR) << "   buildTimestampMapForScene异常: " << e.what();
        return false;
    }
}

// ===========================================================================================
// 预测目录扫描 & .npy 加载
// ===========================================================================================
namespace {

// 跳过 .npy v1.0 文件头，返回数据体大小（字节）；成功时 file 指向数据起点
static bool skipNpyHeader(std::ifstream& f, size_t& data_bytes)
{
    f.seekg(8);
    uint16_t hdr_len = 0;
    f.read(reinterpret_cast<char*>(&hdr_len), 2);
    if (!f) return false;
    f.seekg(10 + hdr_len);
    size_t data_offset = f.tellg();
    f.seekg(0, std::ios::end);
    size_t total = f.tellg();
    f.seekg(data_offset);
    data_bytes = total - data_offset;
    return true;
}

// 从帧目录中查找以 <prefix> 开头、以 <suffix>（默认 ".npy"）结尾的文件
static std::string findFileWithPrefix(const std::string& dir, const std::string& prefix,
                                      const std::string& suffix = ".npy")
{
    if (!fs::exists(dir)) return {};
    for (const auto& e : fs::directory_iterator(dir)) {
        std::string name = e.path().filename().string();
        if (name.rfind(prefix, 0) == 0 && name.size() > suffix.size() &&
            name.substr(name.size() - suffix.size()) == suffix) {
            return e.path().string();
        }
    }
    return {};
}

} // namespace

bool OfflineEvaluator::loadPredRoadSurface(const std::string& frame_dir, uint64_t /*ts_ms*/, RoadSurface& out)
{
    try {
        std::string path = findFileWithPrefix(frame_dir, "pred_road_");
        if (path.empty()) { LOG(WARNING) << "  未找到 pred_road_*.npy: " << frame_dir; return false; }

        std::ifstream f(path, std::ios::binary);
        if (!f.is_open()) { LOG(ERROR) << "  无法打开: " << path; return false; }
        size_t data_bytes = 0;
        if (!skipNpyHeader(f, data_bytes)) return false;

        size_t float_count = data_bytes / sizeof(float);
        if (float_count % 4 != 0) {
            LOG(ERROR) << "  pred_road 维度异常（需 [N,4]）: " << float_count;
            return false;
        }
        std::vector<float> raw(float_count);
        f.read(reinterpret_cast<char*>(raw.data()), data_bytes);

        size_t N = float_count / 4;
        out.pts_num = static_cast<int32_t>(N);
        out.pts.resize(N);
        for (size_t i = 0; i < N; ++i) {
            out.pts[i].x   = raw[i * 4 + 0];
            out.pts[i].y   = raw[i * 4 + 1];
            out.pts[i].z   = raw[i * 4 + 2];
            out.pts[i].clz = raw[i * 4 + 3];
        }
        return true;
    } catch (const std::exception& e) {
        LOG(ERROR) << "  loadPredRoadSurface 异常: " << e.what();
        return false;
    }
}


bool OfflineEvaluator::loadPredTrajectory(const std::string& frame_dir, uint64_t /*ts_ms*/,
                                          WheelTrajectory& out, int32_t& total_pts)
{
    try {
        std::string path = findFileWithPrefix(frame_dir, "pred_traj_");
        if (path.empty()) { LOG(WARNING) << "  未找到 pred_traj_*.npy: " << frame_dir; return false; }

        std::ifstream f(path, std::ios::binary);
        if (!f.is_open()) { LOG(ERROR) << "  无法打开: " << path; return false; }
        size_t data_bytes = 0;
        if (!skipNpyHeader(f, data_bytes)) return false;

        // 格式：|u1 字节流 = 16B header [N_FL,N_FR,N_RL,N_RR] int32×4
        //                 + 各轮 N_w×4 float32 [x,y,z,clz] 顺序拼接（各轮点数可不等）
        if (data_bytes < 16) {
            LOG(ERROR) << "  pred_traj 数据过短: " << data_bytes;
            return false;
        }
        std::array<int32_t, 4> back_per_wheel{};
        f.read(reinterpret_cast<char*>(back_per_wheel.data()), 16);

        std::vector<WheelPoint>* tracks[4] = {
            &out.frontLeftTrajectory,
            &out.frontRightTrajectory,
            &out.rearLeftTrajectory,
            &out.rearRightTrajectory
        };
        int32_t min_pts = std::numeric_limits<int32_t>::max();
        for (int w = 0; w < 4; ++w) {
            const int32_t n = back_per_wheel[w];
            if (n < 0) { LOG(ERROR) << "  pred_traj header 点数非法: wheel=" << w; return false; }
            tracks[w]->resize(static_cast<std::size_t>(n));
            for (int i = 0; i < n; ++i) {
                float p[4];
                f.read(reinterpret_cast<char*>(p), sizeof(p));
                (*tracks[w])[i].x   = p[0];
                (*tracks[w])[i].y   = p[1];
                (*tracks[w])[i].z   = p[2];
                (*tracks[w])[i].clz = p[3];
            }
            if (n > 0) min_pts = std::min(min_pts, n);
        }
        // pts_num 取各轮最小值，确保评测循环对所有轮均有效
        total_pts = (min_pts == std::numeric_limits<int32_t>::max()) ? 0 : min_pts;
        out.pts_num = total_pts;
        return true;
    } catch (const std::exception& e) {
        LOG(ERROR) << "  loadPredTrajectory 异常: " << e.what();
        return false;
    }
}

bool OfflineEvaluator::loadPredVision(const std::string& frame_dir, uint64_t /*ts_ms*/, AuxiliaryOutputs& out)
{
    // pred_img_<ts13>.json 形如 {"head_name": [logits...]}；按 vision_head_order_ 取需要的 head 与维度
    try {
        out.heads.clear();
        const std::string path = findFileWithPrefix(frame_dir, "pred_img_", ".json");
        if (path.empty()) {
            LOG(WARNING) << "  未找到 pred_img_*.json: " << frame_dir;
            return false;
        }
        std::ifstream f(path);
        if (!f.is_open()) { LOG(ERROR) << "  无法打开: " << path; return false; }

        YAML::Node json_data = YAML::Load(f);  // YAML 是 JSON 超集，可直接解析 JSON
        if (!json_data || !json_data.IsMap()) {
            LOG(ERROR) << "  pred_img JSON 根节点非 map: " << path;
            return false;
        }

        for (const auto& [name, dim] : vision_head_order_) {
            const auto node = json_data[name];
            if (!node || !node.IsSequence()) {
                LOG(WARNING) << "  pred_img 缺少视觉头: " << name << " @ " << path;
                continue;
            }
            if (node.size() != dim) {
                LOG(WARNING) << "  pred_img head dim mismatch " << name
                             << " expected=" << dim << " got=" << node.size()
                             << " @ " << path;
                continue;
            }
            std::vector<float> logits;
            logits.reserve(dim);
            try {
                for (const auto& v : node) logits.push_back(v.as<float>());
            } catch (const YAML::BadConversion& e) {
                LOG(WARNING) << "  pred_img logits 转换失败 " << name << ": " << e.what();
                continue;
            }
            out.heads.emplace(name, std::move(logits));
        }
        return !out.heads.empty();
    } catch (const std::exception& e) {
        LOG(ERROR) << "  loadPredVision 异常: " << e.what();
        return false;
    }
}

// 扫描预测目录：按场景前缀过滤、按文件名（数字）排序，从每个帧目录文件名提取 13 位 ms 时间戳
bool OfflineEvaluator::initializePredScenes()
{
    try {
        LOG(INFO) << " OfflineEvaluator: 初始化预测场景列表，扫描目录: " << pred_data_root_;
        if (!fs::exists(pred_data_root_) || !fs::is_directory(pred_data_root_)) {
            LOG(ERROR) << " 预测目录不存在: " << pred_data_root_;
            return false;
        }

        for (const auto& entry : fs::directory_iterator(pred_data_root_)) {
            if (!entry.is_directory()) continue;
            const std::string scene_name = entry.path().filename().string();
            if (scene_name.find(scene_folder_prefix_) != 0) continue;

            PredSceneInfo info;
            info.scene_name = scene_name;

            // 收集帧目录并排序
            std::vector<std::pair<uint64_t, std::string>> frames;
            for (const auto& fe : fs::directory_iterator(entry.path())) {
                if (!fe.is_directory()) continue;
                const std::string fname = fe.path().filename().string();
                // 解析时间戳：支持 "<idx>_<ts_ms>" 或纯 ts_ms
                uint64_t ts_ms = 0;
                size_t us = fname.find('_');
                try {
                    if (us != std::string::npos) ts_ms = std::stoull(fname.substr(us + 1));
                    else                          ts_ms = std::stoull(fname);
                } catch (...) {
                    LOG(WARNING) << "  解析帧目录失败: " << fname;
                    continue;
                }
                // 容错：若是 16 位纳秒，归一化为 13 位 ms
                if (ts_ms > 100'000'000'000'000ULL) ts_ms /= 1'000'000ULL;
                frames.emplace_back(ts_ms, fe.path().string());
            }

            std::sort(frames.begin(), frames.end(),
                      [](const auto& a, const auto& b) { return a.first < b.first; });

            if (!frames.empty()) {
                info.frames = std::move(frames);
                pred_scenes_.push_back(std::move(info));
            } else {
                LOG(WARNING) << "  预测场景 " << scene_name << " 无帧目录，跳过";
            }
        }

        std::sort(pred_scenes_.begin(), pred_scenes_.end(),
                  [](const PredSceneInfo& a, const PredSceneInfo& b) {
                      return a.scene_name < b.scene_name;
                  });

        if (pred_scenes_.empty()) {
            LOG(ERROR) << " 未找到任何预测场景";
            return false;
        }

        LOG(INFO) << " 预测场景列表初始化完成，共 " << pred_scenes_.size() << " 个场景";
        for (size_t i = 0; i < pred_scenes_.size(); ++i) {
            LOG(INFO) << "   [" << i << "] " << pred_scenes_[i].scene_name
                      << " (" << pred_scenes_[i].frames.size() << " 帧)";
        }
        return true;
    } catch (const std::exception& e) {
        LOG(ERROR) << " initializePredScenes异常: " << e.what();
        return false;
    }
}
