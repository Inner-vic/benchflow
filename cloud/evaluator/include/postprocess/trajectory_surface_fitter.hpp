#pragma once

#include <array>
#include <cstddef>
#include <utility>
#include <vector>

#include <Eigen/Dense>

#include "data/wheel_trajectory.hpp"
#include "compat/logging_compat.hpp"

using namespace uroad_m100;

class TrajectorySurfaceFitter {
public:
    // ===== 场景类型检测 =====
    enum class SceneType { NORMAL, STEP, PIT };
    
    struct SceneDetectResult {
        SceneType type = SceneType::NORMAL;
        double mean_delta_z = 0.0;      // 平均Z差 (m)
        double std_delta_z = 0.0;       // Z差标准差/RMSE (m)
        double max_abs_delta_z = 0.0;   // 最大Z差绝对值 (m)
        double rotation_angle_deg = 0.0; // 检测到的旋转角度 (度)
        double max_residual = 0.0;      // 线性拟合后的最大残差 (m)
        size_t point_count = 0;         // 有效点数
    };
    
    // ===== 曲率检测相关（影子模式） =====
    enum class FeatureType { NONE, BUMP, STEP };
    
    struct CurvatureOptions {
        int smooth_window = 5;              // 平滑窗口（点数），5个点约25cm
        int curvature_step = 3;             // 计算曲率的点间隔，3个点约15cm
        double curvature_threshold = 0.5;   // 曲率阈值 (m^-1)，检测凹凸（保留作为辅助）
        double max_feature_width = 1.5;     // 最大特征宽度 (m)，超过认为是坡道过渡
        double step_slope_threshold = 0.15; // 斜率阈值，检测台阶 (>1.5cm)
        double point_spacing = 0.05;        // 点间距 (m)
        bool enable_shadow_mode = true;     // 影子模式：只记录日志，不影响结果

        // ===== 多尺度融合参数（新增）=====
        bool use_multiscale_fusion = true;  // 启用多尺度融合检测
        double deviation_threshold = 0.02;  // 偏差阈值 (m)，2cm，检测凹凸的统一标准
        int min_anomaly_points = 5;         // 最少连续点数才认定为异常区域
    };
    
    struct CurvatureResult {
        std::vector<FeatureType> features;  // 每个点的特征类型
        size_t bump_count = 0;              // 凹凸点数
        size_t step_count = 0;              // 台阶点数
        size_t bump_regions = 0;            // 凹凸数量（合并连续区域后）
        size_t step_regions = 0;            // 台阶数量（合并连续区域后）
        double max_curvature = 0.0;         // 最大曲率
        double max_slope = 0.0;             // 最大斜率
        double max_deviation = 0.0;         // 最大偏差（多尺度融合模式）
    };
    
    struct Options {
        double min_x = 5.0;
        double max_x = 25.0;
        double min_normal_z = 1e-6;
        size_t min_points = 10;
        double ref_angle_alpha = 0.3;  // smoothing for reference alignment angle
        double ref_z0_alpha = 0.2;     // smoothing for reference z0 height

        // Distance-weighted correction parameters (for ApplyReferenceAlignPerWheel)
        bool use_distance_weight = false;       // enable distance-weighted correction
        double weight_transition_start = 12.0;  // start distance for weight decay (meters)
        double weight_transition_end = 25.0;    // end distance for full decay (meters)
        double weight_min = 0.3;                // minimum weight for far points (0.0-1.0)

        // Local neighborhood parameters (used by MovingPCA and MLS)
        // Neighbor search uses XY distance.
        double local_radius = 1.5;      // meters
        size_t local_min_points = 6;
        double local_sigma = 0.8;       // meters
        size_t local_max_neighbors = 60;

        // Iterative trunk line fitting parameters (XZ line)
        double trunk_near_min = 5.0;        // meters
        double trunk_near_max = 10.0;       // meters
        double trunk_threshold = 0.015;      // meters (15mm)
        double trunk_near_weight = 30.0;     // near range weight multiplier
        double trunk_near_constraint = 0.001; // meters (1mm)
        size_t trunk_max_iter = 10;
        size_t trunk_min_points = 8;
        double trunk_converge_eps = 1e-4;    // radians

        // Near-range outlier detection parameters (based on last frame PCA residuals)
        double near_outlier_threshold = 0.01;  // meters (10mm), last frame residual threshold
        double near_outlier_weight = 1.0;      // weight for outlier points (reduced from trunk_near_weight)

        // Adaptive scene detection parameters
        double trunk_near_max_step = 10.0;     // meters, trunk_near_max for STEP scene
        double scene_step_mean_threshold = 0.03;  // meters (3cm), mean delta_z threshold for STEP detection
        double scene_step_std_threshold = 0.02;   // meters (2cm), std threshold for STEP (low std = consistent offset)
        double scene_pit_max_threshold = 0.05;    // meters (5cm), max delta_z threshold for PIT detection
    };

    TrajectorySurfaceFitter() = default;

    void Reset();

    // Per-wheel PCA plane fitting with fallback to last plane when points are insufficient.
    void ApplyPcaPerWheel(WheelTrajectory& trajectory);
    void ApplyPcaPerWheel(WheelTrajectory& trajectory, const Options& options);

    // Per-wheel 2D PCA-like fitting in XZ (plane z = ax + b) with fallback.
    void ApplyPca2dPerWheel(WheelTrajectory& trajectory);
    void ApplyPca2dPerWheel(WheelTrajectory& trajectory, const Options& options);

    // Per-wheel reference alignment: fit reference segment in XZ and rotate z to level it.
    void ApplyReferenceAlignPerWheel(WheelTrajectory& trajectory);
    void ApplyReferenceAlignPerWheel(WheelTrajectory& trajectory, const Options& ref_options);

    // Per-wheel iterative trunk line fitting (near-range constraint + inlier selection).
    // correspondences: optional, array of 4 wheels, each contains pairs of (current_frame_index, last_frame_z_residual)
    //                  used for outlier detection based on last frame PCA residuals
    void ApplyIterativeTrunkLinePerWheel(WheelTrajectory& trajectory);
    void ApplyIterativeTrunkLinePerWheel(WheelTrajectory& trajectory, const Options& options,
                                         const std::array<std::vector<std::pair<size_t, double>>, 4>* correspondences = nullptr);

    // Per-wheel tangent-aligned local fit: align by local tangent and output shape residual.
    void ApplyTangentAlignedLocalFitPerWheel(WheelTrajectory& trajectory);
    void ApplyTangentAlignedLocalFitPerWheel(WheelTrajectory& trajectory, const Options& options);

    // Per-wheel moving PCA (local weighted PCA plane per point).
    void ApplyMovingPcaPerWheel(WheelTrajectory& trajectory);
    void ApplyMovingPcaPerWheel(WheelTrajectory& trajectory, const Options& options);

    // Per-wheel MLS (local weighted height fit z = ax + by + c).
    void ApplyMlsPerWheel(WheelTrajectory& trajectory);
    void ApplyMlsPerWheel(WheelTrajectory& trajectory, const Options& options);

    // ===== 场景类型检测（台阶/凹坑自适应）=====
    // 基于前后帧Z差分布判断场景类型：
    // DetectSceneType 已移除（调用方已注释，待未来重新启用时恢复）

    // ===== 曲率检测（影子模式）=====
    // 基于曲率检测凹凸，基于斜率突变检测台阶
    // 影子模式：只记录日志和统计，不影响实际的离群点标记
    CurvatureResult DetectFeaturesByCurvature(
        const std::vector<WheelPoint>& track,
        const Options& options,
        const CurvatureOptions& curv_opts);

    // 设置曲率检测选项
    void SetCurvatureOptions(const CurvatureOptions& opts) { curvature_options_ = opts; }
    const CurvatureOptions& GetCurvatureOptions() const { return curvature_options_; }

private:
    // ===== 辅助函数：多尺度融合检测 =====
    static std::vector<double> MovingAverageSmooth(
        const std::vector<double>& data, int window);

    static std::vector<double> MovingAverageWithGapCheck(
        const std::vector<double>& data,
        const std::vector<double>& x_vals,
        int window,
        double max_gap);
    struct PlaneCache {
        Eigen::Vector3d normal = Eigen::Vector3d(0.0, 0.0, 1.0);
        Eigen::Vector3d mean = Eigen::Vector3d::Zero();
        bool valid = false;
    };
    struct RefAngleCache {
        double theta = 0.0;
        double z0 = 0.0;
        bool valid = false;
    };

    static bool IsValidPoint(const WheelPoint& pt, const Options& options);
    static void CollectPoints(const std::vector<WheelPoint>& track,
                              const Options& options,
                              std::vector<Eigen::Vector3d>* points);
    static bool FitPlanePca(const std::vector<Eigen::Vector3d>& points,
                            const Options& options,
                            Eigen::Vector3d* normal,
                            Eigen::Vector3d* mean);
    static bool FitPlanePca2dXz(const std::vector<Eigen::Vector3d>& points,
                                const Options& options,
                                Eigen::Vector3d* normal,
                                Eigen::Vector3d* mean);
    static int ApplyPlaneToTrack(std::vector<WheelPoint>& track,
                                 const Options& options,
                                 const Eigen::Vector3d& normal,
                                 const Eigen::Vector3d& mean,
                                 double* max_pos_dist,
                                 double* max_neg_dist);

    static bool FitPlaneWeighted(const std::vector<Eigen::Vector3d>& points,
                                 const std::vector<double>& weights,
                                 const Options& options,
                                 Eigen::Vector3d* normal,
                                 Eigen::Vector3d* mean);
    static bool FitHeightWeightedPlane(const std::vector<Eigen::Vector3d>& points,
                                       const std::vector<double>& weights,
                                       double* a,
                                       double* b,
                                       double* c);
    static bool FitLineWeightedXz(const std::vector<Eigen::Vector3d>& points,
                                  const std::vector<double>& weights,
                                  const Options& options,
                                  Eigen::Vector3d* normal,
                                  Eigen::Vector3d* mean);

    std::array<PlaneCache, 4> last_pca_planes_{};
    std::array<RefAngleCache, 4> last_ref_angles_{};
    
    // 曲率检测选项
    CurvatureOptions curvature_options_{};
    
    // 曲率检测辅助函数
    static std::vector<double> MovingAverage(const std::vector<double>& data, int window);
    // MovingAverageWithGapCheck 已在第161行声明，此处删除重复声明
    static double EstimateFeatureWidth(const std::vector<double>& curvature, 
                                       const std::vector<double>& x_vals,
                                       size_t center_idx, 
                                       double threshold);
};

