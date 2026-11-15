#include "postprocess/trajectory_surface_fitter.hpp"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <limits>

namespace {
constexpr const char* kWheelNames[4] = {"FL", "FR", "RL", "RR"};
constexpr double kPi = 3.14159265358979323846;

double SafeExp(double x) {
    if (x < -60.0) return 0.0;
    if (x > 60.0) return std::exp(60.0);
    return std::exp(x);
}
}  // namespace

void TrajectorySurfaceFitter::Reset() {
    for (auto& plane : last_pca_planes_) {
        plane.valid = false;
        plane.normal = Eigen::Vector3d(0.0, 0.0, 1.0);
        plane.mean = Eigen::Vector3d::Zero();
    }
    for (auto& ref : last_ref_angles_) {
        ref.valid = false;
        ref.theta = 0.0;
        ref.z0 = 0.0;
    }
}

bool TrajectorySurfaceFitter::IsValidPoint(const WheelPoint& pt, const Options& options) {
    if (pt.clz >= 15.0f || pt.z == -1.0f) {
        return false;
    }
    // if (pt.z < -0.5 || pt.z > -0.2) {
    //     return false;
    // }
    if (pt.x < options.min_x || pt.x > options.max_x) {
        return false;
    }
    return true;
}

void TrajectorySurfaceFitter::CollectPoints(const std::vector<WheelPoint>& track,
                                            const Options& options,
                                            std::vector<Eigen::Vector3d>* points) {
    points->clear();
    points->reserve(track.size());
    for (const auto& pt : track) {
        if (!IsValidPoint(pt, options)) {
            continue;
        }
        points->emplace_back(pt.x, pt.y, pt.z);
    }
}

bool TrajectorySurfaceFitter::FitPlanePca(const std::vector<Eigen::Vector3d>& points,
                                          const Options& options,
                                          Eigen::Vector3d* normal,
                                          Eigen::Vector3d* mean) {
    if (points.size() < options.min_points) {
        return false;
    }

    *mean = Eigen::Vector3d::Zero();
    for (const auto& p : points) {
        *mean += p;
    }
    *mean /= static_cast<double>(points.size());

    Eigen::Matrix3d cov = Eigen::Matrix3d::Zero();
    for (const auto& p : points) {
        Eigen::Vector3d d = p - *mean;
        cov += d * d.transpose();
    }
    cov /= static_cast<double>(points.size());

    Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> solver(cov);
    if (solver.info() != Eigen::Success) {
        return false;
    }

    *normal = solver.eigenvectors().col(0);
    if (normal->z() < 0) {
        *normal = -*normal;
    }
    normal->normalize();

    if (std::abs(normal->z()) < options.min_normal_z) {
        return false;
    }
    return true;
}

bool TrajectorySurfaceFitter::FitPlanePca2dXz(const std::vector<Eigen::Vector3d>& points,
                                              const Options& options,
                                              Eigen::Vector3d* normal,
                                              Eigen::Vector3d* mean) {
    if (points.size() < options.min_points) {
        return false;
    }

    double sum_x = 0.0;
    double sum_z = 0.0;
    double sum_xx = 0.0;
    double sum_xz = 0.0;
    for (const auto& p : points) {
        const double x = p.x();
        const double z = p.z();
        sum_x += x;
        sum_z += z;
        sum_xx += x * x;
        sum_xz += x * z;
    }

    const double n = static_cast<double>(points.size());
    const double denom = n * sum_xx - sum_x * sum_x;
    if (std::abs(denom) < 1e-9) {
        return false;
    }

    const double a = (n * sum_xz - sum_x * sum_z) / denom;
    const double b = (sum_z - a * sum_x) / n;
    const double mean_x = sum_x / n;

    *mean = Eigen::Vector3d(mean_x, 0.0, a * mean_x + b);
    *normal = Eigen::Vector3d(-a, 0.0, 1.0);
    if (normal->z() < 0) {
        *normal = -*normal;
    }
    normal->normalize();

    if (std::abs(normal->z()) < options.min_normal_z) {
        return false;
    }
    return true;
}

int TrajectorySurfaceFitter::ApplyPlaneToTrack(std::vector<WheelPoint>& track,
                                               const Options& options,
                                               const Eigen::Vector3d& normal,
                                               const Eigen::Vector3d& mean,
                                               double* max_pos_dist,
                                               double* max_neg_dist) {
    int updated = 0;
    if (max_pos_dist) *max_pos_dist = 0.0;
    if (max_neg_dist) *max_neg_dist = 0.0;

    for (auto& pt : track) {
        // if (!IsValidPoint(pt, options)) {
        //     continue;
        // }
        if (pt.clz == 26.0f || pt.z == -1.0f) {
            continue;
        }
        Eigen::Vector3d point(pt.x, pt.y, pt.z);
        const double distance = normal.dot(point - mean);
        pt.z = static_cast<float>(distance);

        if (max_pos_dist && max_neg_dist) {
            if (distance > 0) {
                *max_pos_dist = std::max(*max_pos_dist, distance);
            } else {
                *max_neg_dist = std::min(*max_neg_dist, distance);
            }
        }
        ++updated;
    }
    return updated;
}

bool TrajectorySurfaceFitter::FitPlaneWeighted(const std::vector<Eigen::Vector3d>& points,
                                               const std::vector<double>& weights,
                                               const Options& options,
                                               Eigen::Vector3d* normal,
                                               Eigen::Vector3d* mean) {
    if (points.size() < options.local_min_points || points.size() != weights.size()) {
        return false;
    }

    double wsum = 0.0;
    *mean = Eigen::Vector3d::Zero();
    for (size_t i = 0; i < points.size(); ++i) {
        const double w = weights[i];
        wsum += w;
        *mean += w * points[i];
    }
    if (wsum <= 0.0) {
        return false;
    }
    *mean /= wsum;

    Eigen::Matrix3d cov = Eigen::Matrix3d::Zero();
    for (size_t i = 0; i < points.size(); ++i) {
        Eigen::Vector3d d = points[i] - *mean;
        cov += weights[i] * (d * d.transpose());
    }
    cov /= wsum;

    Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> solver(cov);
    if (solver.info() != Eigen::Success) {
        return false;
    }

    *normal = solver.eigenvectors().col(0);
    if (normal->z() < 0) {
        *normal = -*normal;
    }
    normal->normalize();

    if (std::abs(normal->z()) < options.min_normal_z) {
        return false;
    }
    return true;
}

void TrajectorySurfaceFitter::ApplyPcaPerWheel(WheelTrajectory& trajectory) {
    Options options;
    ApplyPcaPerWheel(trajectory, options);
}

void TrajectorySurfaceFitter::ApplyPcaPerWheel(WheelTrajectory& trajectory, const Options& options) {
    std::vector<WheelPoint>* tracks[4] = {
        &trajectory.frontLeftTrajectory,
        &trajectory.frontRightTrajectory,
        &trajectory.rearLeftTrajectory,
        &trajectory.rearRightTrajectory
    };

    for (int w = 0; w < 4; ++w) {
        auto& track = *tracks[w];
        std::vector<Eigen::Vector3d> points;
        CollectPoints(track, options, &points);

        Eigen::Vector3d normal;
        Eigen::Vector3d mean;
        bool fit_ok = FitPlanePca(points, options, &normal, &mean);

        if (!fit_ok) {
            if (last_pca_planes_[w].valid) {
                double max_pos = 0.0;
                double max_neg = 0.0;
                int updated = ApplyPlaneToTrack(track, options,
                                                last_pca_planes_[w].normal,
                                                last_pca_planes_[w].mean,
                                                &max_pos, &max_neg);
                LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                          << " PCA reuse (updated=" << updated
                          << ", nz=" << last_pca_planes_[w].normal.z() << ")";
            } else {
                LOG(WARNING) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                             << " PCA skipped (points=" << points.size() << ")";
            }
            continue;
        }

        double max_pos = 0.0;
        double max_neg = 0.0;
        int updated = ApplyPlaneToTrack(track, options, normal, mean, &max_pos, &max_neg);

        last_pca_planes_[w].normal = normal;
        last_pca_planes_[w].mean = mean;
        last_pca_planes_[w].valid = true;

        LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                  << " PCA done (pts=" << points.size()
                  << ", updated=" << updated
                  << ", above=" << max_pos << "m"
                  << ", below=" << max_neg << "m"
                  << ", nz=" << normal.z() << ")";
    }
}

void TrajectorySurfaceFitter::ApplyPca2dPerWheel(WheelTrajectory& trajectory) {
    Options options;
    ApplyPca2dPerWheel(trajectory, options);
}

void TrajectorySurfaceFitter::ApplyPca2dPerWheel(WheelTrajectory& trajectory, const Options& options) {
    std::vector<WheelPoint>* tracks[4] = {
        &trajectory.frontLeftTrajectory,
        &trajectory.frontRightTrajectory,
        &trajectory.rearLeftTrajectory,
        &trajectory.rearRightTrajectory
    };

    for (int w = 0; w < 4; ++w) {
        auto& track = *tracks[w];
        std::vector<Eigen::Vector3d> points;
        CollectPoints(track, options, &points);

        Eigen::Vector3d normal;
        Eigen::Vector3d mean;
        const bool fit_ok = FitPlanePca2dXz(points, options, &normal, &mean);

        if (!fit_ok) {
            if (last_pca_planes_[w].valid) {
                double max_pos = 0.0;
                double max_neg = 0.0;
                int updated = ApplyPlaneToTrack(track, options,
                                                last_pca_planes_[w].normal,
                                                last_pca_planes_[w].mean,
                                                &max_pos, &max_neg);
                LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                          << " PCA2D reuse (updated=" << updated
                          << ", nz=" << last_pca_planes_[w].normal.z() << ")";
            } else {
                LOG(WARNING) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                             << " PCA2D skipped (points=" << points.size() << ")";
            }
            continue;
        }

        double max_pos = 0.0;
        double max_neg = 0.0;
        int updated = ApplyPlaneToTrack(track, options, normal, mean, &max_pos, &max_neg);

        last_pca_planes_[w].normal = normal;
        last_pca_planes_[w].mean = mean;
        last_pca_planes_[w].valid = true;

        LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                  << " PCA2D done (pts=" << points.size()
                  << ", updated=" << updated
                  << ", above=" << max_pos << "m"
                  << ", below=" << max_neg << "m"
                  << ", nz=" << normal.z() << ")";
    }
}

void TrajectorySurfaceFitter::ApplyReferenceAlignPerWheel(WheelTrajectory& trajectory) {
    Options options;
    ApplyReferenceAlignPerWheel(trajectory, options);
}

void TrajectorySurfaceFitter::ApplyReferenceAlignPerWheel(WheelTrajectory& trajectory, const Options& ref_options) {
    std::vector<WheelPoint>* tracks[4] = {
        &trajectory.frontLeftTrajectory,
        &trajectory.frontRightTrajectory,
        &trajectory.rearLeftTrajectory,
        &trajectory.rearRightTrajectory
    };

    for (int w = 0; w < 4; ++w) {
        auto& track = *tracks[w];
        std::vector<Eigen::Vector3d> points;
        CollectPoints(track, ref_options, &points);

        Eigen::Vector3d normal;
        Eigen::Vector3d mean;
        const bool fit_ok = FitPlanePca2dXz(points, ref_options, &normal, &mean);
        double theta = 0.0;
        double z0 = 0.0;
        if (fit_ok && std::abs(normal.z()) >= 1e-9) {
            const double slope = -normal.x() / normal.z();
            const double raw_theta = std::atan(slope);
            if (last_ref_angles_[w].valid) {
                const double alpha = std::clamp(ref_options.ref_angle_alpha, 0.0, 1.0);
                theta = alpha * raw_theta + (1.0 - alpha) * last_ref_angles_[w].theta;
            } else {
                theta = raw_theta;
            }
        } else if (last_ref_angles_[w].valid) {
            theta = last_ref_angles_[w].theta;
        } else {
            LOG(WARNING) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                         << " RefAlign skipped (points=" << points.size() << ")";
            continue;
        }
        const double cos_t = std::cos(theta);
        const double sin_t = std::sin(theta);
        if (fit_ok) {
            const double z0_raw = -mean.x() * sin_t + mean.z() * cos_t;
            // Apply smoothing to z0 to reduce height jitter
            if (last_ref_angles_[w].valid) {
                const double alpha_z0 = std::clamp(ref_options.ref_z0_alpha, 0.0, 1.0);
                z0 = alpha_z0 * z0_raw + (1.0 - alpha_z0) * last_ref_angles_[w].z0;
            } else {
                z0 = z0_raw;
            }
            last_ref_angles_[w].theta = theta;
            last_ref_angles_[w].z0 = z0;
            last_ref_angles_[w].valid = true;
        } else if (last_ref_angles_[w].valid) {
            z0 = last_ref_angles_[w].z0;
        }
        int updated = 0;
        const double ref_center = (ref_options.min_x + ref_options.max_x) / 2.0;

        for (auto& pt : track) {
            if (pt.clz == 26.0f || pt.z == -1.0f) {
                continue;
            }
            const double x = pt.x;
            const double z = pt.z;

            // Calculate fully corrected height
            const double z_corrected = (-x * sin_t + z * cos_t) - z0;

            if (!ref_options.use_distance_weight) {
                // Original method: full correction everywhere
                pt.z = static_cast<float>(z_corrected);
            } else {
                // Distance-weighted method
                const double dist_from_ref = std::abs(x - ref_center);
                double weight = 1.0;

                if (dist_from_ref >= ref_options.weight_transition_end) {
                    weight = ref_options.weight_min;
                } else if (dist_from_ref > ref_options.weight_transition_start) {
                    const double t = (dist_from_ref - ref_options.weight_transition_start) /
                                   (ref_options.weight_transition_end - ref_options.weight_transition_start);
                    weight = 1.0 - t * (1.0 - ref_options.weight_min);
                }

                // Only remove offset, no angle correction
                const double z_offset_only = z - z0;

                // Weighted blending
                pt.z = static_cast<float>(weight * z_corrected + (1.0 - weight) * z_offset_only);
            }
            ++updated;
        }

        LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                  << " RefAlign done (ref_pts=" << points.size()
                  << ", updated=" << updated
                  << ", ref_deg=" << (theta * 180.0 / kPi)
                  << ", dist_weight=" << (ref_options.use_distance_weight ? "ON" : "OFF") << ")";
    }
}

void TrajectorySurfaceFitter::ApplyIterativeTrunkLinePerWheel(WheelTrajectory& trajectory) {
    Options options;
    ApplyIterativeTrunkLinePerWheel(trajectory, options, nullptr);
}

void TrajectorySurfaceFitter::ApplyIterativeTrunkLinePerWheel(
    WheelTrajectory& trajectory,
    const Options& options,
    const std::array<std::vector<std::pair<size_t, double>>, 4>* correspondences) {
    
    std::vector<WheelPoint>* tracks[4] = {
        &trajectory.frontLeftTrajectory,
        &trajectory.frontRightTrajectory,
        &trajectory.rearLeftTrajectory,
        &trajectory.rearRightTrajectory
    };

    for (int w = 0; w < 4; ++w) {
        auto& track = *tracks[w];
        std::vector<Eigen::Vector3d> points;
        CollectPoints(track, options, &points);

        if (points.size() < options.min_points) {
            if (last_pca_planes_[w].valid) {
                double max_pos = 0.0;
                double max_neg = 0.0;
                int updated = ApplyPlaneToTrack(track, options,
                                                last_pca_planes_[w].normal,
                                                last_pca_planes_[w].mean,
                                                &max_pos, &max_neg);
                LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                          << " TrunkLine reuse (updated=" << updated
                          << ", nz=" << last_pca_planes_[w].normal.z() << ")";
            } else {
                LOG(WARNING) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                             << " TrunkLine skipped (points=" << points.size() << ")";
            }
            continue;
        }

        const double trunk_near_min = options.trunk_near_min;
        
        // ========== 场景检测：根据前后帧Z差判断台阶/凹坑 ==========
        double trunk_near_max = options.trunk_near_max;  // 默认值（凹坑模式）
        // SceneType detected_scene = SceneType::NORMAL;
        
        // if (correspondences != nullptr) {
        //     const auto& corr = (*correspondences)[w];
        //     SceneDetectResult scene_result = DetectSceneType(track, corr, options);
        //     detected_scene = scene_result.type;
            
        //     if (scene_result.type == SceneType::STEP) {
        //         // 台阶场景（检测到旋转）：使用较小的近端范围，近端点更可靠
        //         trunk_near_max = options.trunk_near_max_step;
        //         LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
        //                   << " STEP detected (rotation=" << std::fixed << std::setprecision(2)
        //                   << scene_result.rotation_angle_deg << "deg"
        //                   << ", mean_dz=" << std::setprecision(1) << (scene_result.mean_delta_z * 100) << "cm"
        //                   << ", rmse=" << (scene_result.std_delta_z * 100) << "cm"
        //                   << ", pts=" << scene_result.point_count << ")"
        //                   << " -> trunk_near_max=" << trunk_near_max;
        //     } else if (scene_result.type == SceneType::PIT) {
        //         // 凹坑场景（无旋转但有局部异常）：使用较大的近端范围，需要更多点稳定拟合
        //         trunk_near_max = options.trunk_near_max;
        //         LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
        //                   << " PIT detected (rotation=" << std::fixed << std::setprecision(2)
        //                   << scene_result.rotation_angle_deg << "deg"
        //                   << ", max_residual=" << std::setprecision(1) << (scene_result.max_residual * 100) << "cm"
        //                   << ", pts=" << scene_result.point_count << ")"
        //                   << " -> trunk_near_max=" << trunk_near_max;
        //     }
        // }

        // ========== 阶段0: 曲率检测标记凹凸离群点 ==========
        std::vector<bool> is_outlier(track.size(), false);
        size_t curvature_outlier_count = 0;

        // 执行曲率检测（无论影子模式开关状态）
        CurvatureResult curv_result;

        // 如果不是影子模式，将检测到的凹凸点标记为离群点
        if (!curvature_options_.enable_shadow_mode) {
            curv_result = DetectFeaturesByCurvature(track, options, curvature_options_);
            for (size_t i = 0; i < curv_result.features.size(); ++i) {
                if (curv_result.features[i] != FeatureType::NONE) {
                    is_outlier[i] = true;
                    ++curvature_outlier_count;
                }
            }
        }

        // 记录检测结果日志
        std::string mode_str = curvature_options_.use_multiscale_fusion ? "[MultiscaleFusion]" : "[CurvatureMethod]";
        std::string effect_str = curvature_options_.enable_shadow_mode ? "[SHADOW]" : "[ACTIVE]";
        LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                  << " " << mode_str << " " << effect_str
                  << " | bumps: " << curv_result.bump_regions << "个(" << curv_result.bump_count << "点)"
                  << ", steps: " << curv_result.step_regions << "个(" << curv_result.step_count << "点)"
                  << " | max_dev=" << std::fixed << std::setprecision(3) << (curv_result.max_deviation * 100) << " cm"
                  << ", max_curv=" << std::fixed << std::setprecision(3) << curv_result.max_curvature << " m^-1"
                  << ", max_slope=" << std::fixed << std::setprecision(3) << curv_result.max_slope
                  << " | thresh: " << (curvature_options_.use_multiscale_fusion ?
                      "dev=" + std::to_string(static_cast<int>(curvature_options_.deviation_threshold * 100)) + "cm" :
                      "curv=" + std::to_string(curvature_options_.curvature_threshold))
                  << " | outliers_marked: " << curvature_outlier_count;

        // ========== 阶段1: 基于上一帧残差标记离群点（追加标记）==========
        size_t frame_outlier_count = 0;

        if (curvature_options_.enable_shadow_mode && correspondences != nullptr) {
            const auto& corr = (*correspondences)[w];
            for (const auto& [cur_idx, last_z] : corr) {
                // 上一帧对应点的残差绝对值超过阈值，标记为离群点
                if (std::abs(last_z) > options.near_outlier_threshold) {
                    if (cur_idx < track.size() && !is_outlier[cur_idx]) {
                        is_outlier[cur_idx] = true;
                        ++frame_outlier_count;
                    }
                }
            }
        }

        size_t total_outlier_count = curvature_outlier_count + frame_outlier_count;
        if (total_outlier_count > 0) {
            LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                      << " total outliers: " << total_outlier_count
                      << " (curvature: " << curvature_outlier_count
                      << ", frame_residual: " << frame_outlier_count << ")";
        }

        // ========== 阶段2: 收集干净的近端点做初步拟合 ==========
        std::vector<Eigen::Vector3d> near_points;
        near_points.reserve(points.size());

        for (size_t i = 0; i < track.size(); ++i) {
            const auto& pt = track[i];
            if (!IsValidPoint(pt, options)) {
                continue;
            }
            if (pt.x >= trunk_near_min && pt.x <= trunk_near_max) {
                // 只有非离群点才参与初步拟合
                if (!is_outlier[i]) {
                    near_points.emplace_back(pt.x, pt.y, pt.z);
                }
            }
        }

        Eigen::Vector3d normal;
        Eigen::Vector3d mean;
        Options init_options = options;
        init_options.min_points = std::min(options.min_points, options.trunk_min_points);
        
        // 用干净的近端点做初步拟合
        bool fit_ok = FitPlanePca2dXz(near_points, init_options, &normal, &mean);
        if (!fit_ok) {
            // 近端点不够，用全部点拟合
            LOG(WARNING) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                         << " TrunkLine fit failed (near_points=" << near_points.size() << ")";
            fit_ok = FitPlanePca2dXz(points, init_options, &normal, &mean);
        }

        if (!fit_ok) {
            if (last_pca_planes_[w].valid) {
                double max_pos = 0.0;
                double max_neg = 0.0;
                int updated = ApplyPlaneToTrack(track, options,
                                                last_pca_planes_[w].normal,
                                                last_pca_planes_[w].mean,
                                                &max_pos, &max_neg);
                LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                          << " TrunkLine reuse (updated=" << updated
                          << ", nz=" << last_pca_planes_[w].normal.z() << ")";
            } else {
                LOG(WARNING) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                             << " TrunkLine skipped (points=" << points.size() << ")";
            }
            continue;
        }

        // ========== 阶段3: 迭代优化（离群点降低权重）==========
        size_t last_inlier_count = 0;
        Eigen::Vector3d last_normal = normal;
        size_t iter_used = 0;
        bool converged = false;
        bool weight_boosted = false;

        // 建立points到track索引的映射（用于查找离群点标记）
        std::vector<size_t> point_to_track_idx;
        point_to_track_idx.reserve(points.size());
        for (size_t i = 0; i < track.size(); ++i) {
            if (IsValidPoint(track[i], options)) {
                point_to_track_idx.push_back(i);
            }
        }

        for (size_t iter = 0; iter < options.trunk_max_iter; ++iter) {
            std::vector<Eigen::Vector3d> inlier_points;
            std::vector<double> weights;
            inlier_points.reserve(points.size());
            weights.reserve(points.size());

            size_t inlier_count = 0;
            for (size_t pi = 0; pi < points.size(); ++pi) {
                const auto& p = points[pi];
                const double dist = std::abs(normal.dot(p - mean)); 
                const bool is_near = (p.x() >= trunk_near_min && p.x() <= trunk_near_max);
                const bool is_inlier = is_near || dist < options.trunk_threshold;
                if (!is_inlier) {
                    continue;
                }
                inlier_points.push_back(p);
                
                // 确定权重：近端点高权重，但离群点降低权重
                double weight = 1.0;
                if (is_near) {
                    size_t track_idx = (pi < point_to_track_idx.size()) ? point_to_track_idx[pi] : 0;
                    if (track_idx < is_outlier.size() && is_outlier[track_idx]) {
                        // 离群点使用低权重
                        weight = options.near_outlier_weight;
                    } else {
                        // 正常近端点使用高权重
                        weight = options.trunk_near_weight;
                    }
                }
                weights.push_back(weight);
                ++inlier_count;
            }

            if (inlier_count < options.trunk_min_points) {
                break;
            }

            Eigen::Vector3d new_normal;
            Eigen::Vector3d new_mean;
            if (!FitLineWeightedXz(inlier_points, weights, options, &new_normal, &new_mean)) {
                break;
            }

            double near_max_error = 0.0;
            for (size_t pi = 0; pi < points.size(); ++pi) {
                const auto& p = points[pi];
                if (p.x() >= trunk_near_min && p.x() <= trunk_near_max) {
                    // 新增：排除离群点
                    size_t track_idx = (pi < point_to_track_idx.size()) ? point_to_track_idx[pi] : 0;
                    if (track_idx < is_outlier.size() && is_outlier[track_idx]) {
                        continue;
                    }
                    near_max_error = std::max(near_max_error, std::abs(new_normal.dot(p - new_mean)));
                }
            }

            if (near_max_error > options.trunk_near_constraint * 2.0) {
                weight_boosted = true;
                for (size_t i = 0; i < inlier_points.size(); ++i) {
                    if (inlier_points[i].x() >= trunk_near_min && inlier_points[i].x() <= trunk_near_max) {
                        // 只对非离群点增加权重
                        if (weights[i] > options.near_outlier_weight) {
                            weights[i] = options.trunk_near_weight * 1.2;
                        }
                    }
                }
                if (FitLineWeightedXz(inlier_points, weights, options, &new_normal, &new_mean)) {
                    // 使用增强权重的拟合结果（Use the boosted-weight fitting result）
                    near_max_error = 0.0;
                    for (const auto& p : points) {
                        if (p.x() >= trunk_near_min && p.x() <= trunk_near_max) {
                            near_max_error = std::max(near_max_error, std::abs(new_normal.dot(p - new_mean)));
                        }
                    }
                }
            }

            double angle_change = 0.0;
            if (iter > 0) {
                const double dot = std::clamp(last_normal.dot(new_normal), -1.0, 1.0);
                angle_change = std::acos(dot);
            }

            normal = new_normal;
            mean = new_mean;
            iter_used = iter + 1;

            // 收敛判定：内点数稳定、角度变化小、近端误差满足约束
            if (iter > 0 && inlier_count == last_inlier_count &&
                angle_change < options.trunk_converge_eps &&
                near_max_error <= options.trunk_near_constraint) {
                converged = true;
                break;
            }

            last_inlier_count = inlier_count;
            last_normal = new_normal;
        }

        double max_pos = 0.0;
        double max_neg = 0.0;
        const int updated = ApplyPlaneToTrack(track, options, normal, mean, &max_pos, &max_neg);

        // 计算最终近端误差（用于日志输出）
        double final_near_max_error = 0.0;
        for (const auto& p : points) {
            if (p.x() >= trunk_near_min && p.x() <= trunk_near_max) {
                final_near_max_error = std::max(final_near_max_error, std::abs(normal.dot(p - mean)));
            }
        }

        // 计算路面倾角（角度制）
        const double slope_angle_deg = std::acos(std::clamp(normal.z(), 0.0, 1.0)) * 180.0 / kPi;

        last_pca_planes_[w].normal = normal;
        last_pca_planes_[w].mean = mean;
        last_pca_planes_[w].valid = true;

        LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                  << " TrunkLine done"
                  << " | pts=" << points.size()
                  << ", outliers=" << total_outlier_count
                  << ", clean_near=" << near_points.size()
                  << ", inliers=" << last_inlier_count
                  << " (" << std::fixed << std::setprecision(1)
                  << (last_inlier_count * 100.0 / points.size()) << "%)"
                  << " | updated=" << updated
                  << " | iter=" << iter_used << "/" << options.trunk_max_iter
                  << ", converged=" << (converged ? "Y" : "N")
                  << ", weight_boost=" << (weight_boosted ? "Y" : "N")
                  << " | near_err=" << std::fixed << std::setprecision(2)
                  << (final_near_max_error * 1000.0) << "mm"
                  << " (thresh=" << (options.trunk_near_constraint * 2000.0) << "mm)"
                  << " | residual: +" << std::fixed << std::setprecision(1)
                  << (max_pos * 1000.0) << "/-"
                  << (max_neg * 1000.0) << "mm"
                  << " | slope=" << std::fixed << std::setprecision(2)
                  << slope_angle_deg << "°"
                  << ", nz=" << std::fixed << std::setprecision(4) << normal.z();
    }
}

void TrajectorySurfaceFitter::ApplyTangentAlignedLocalFitPerWheel(WheelTrajectory& trajectory) {
    Options options;
    ApplyTangentAlignedLocalFitPerWheel(trajectory, options);
}

void TrajectorySurfaceFitter::ApplyTangentAlignedLocalFitPerWheel(WheelTrajectory& trajectory, const Options& options) {
    std::vector<WheelPoint>* tracks[4] = {
        &trajectory.frontLeftTrajectory,
        &trajectory.frontRightTrajectory,
        &trajectory.rearLeftTrajectory,
        &trajectory.rearRightTrajectory
    };

    const double radius2 = options.local_radius * options.local_radius;

    for (int w = 0; w < 4; ++w) {
        auto& track = *tracks[w];
        int updated = 0;
        auto is_usable = [](const WheelPoint& pt) {
            return pt.clz < 15.0f && pt.z != -1.0f;
        };

        for (size_t i = 0; i < track.size(); ++i) {
            auto& center_pt = track[i];
            if (!is_usable(center_pt)) {
                continue;
            }

            // Estimate tangent from nearest valid neighbors along the trajectory.
            bool has_prev = false;
            bool has_next = false;
            Eigen::Vector2d prev_xy;
            Eigen::Vector2d next_xy;
            double prev_z = 0.0;
            double next_z = 0.0;
            for (size_t j = i; j-- > 0; ) {
                if (is_usable(track[j])) {
                    prev_xy = Eigen::Vector2d(track[j].x, track[j].y);
                    prev_z = track[j].z;
                    has_prev = true;
                    break;
                }
            }
            for (size_t j = i + 1; j < track.size(); ++j) {
                if (is_usable(track[j])) {
                    next_xy = Eigen::Vector2d(track[j].x, track[j].y);
                    next_z = track[j].z;
                    has_next = true;
                    break;
                }
            }
            if (!has_prev || !has_next) {
                continue;
            }

            Eigen::Vector2d tangent = next_xy - prev_xy;
            const double t_norm = tangent.norm();
            if (t_norm < 1e-6) {
                continue;
            }
            tangent /= t_norm;
            const double yaw = std::atan2(tangent.y(), tangent.x());
            const double cos_y = std::cos(-yaw);
            const double sin_y = std::sin(-yaw);

            std::vector<Eigen::Vector3d> neighbors;
            neighbors.reserve(options.local_max_neighbors);
            for (const auto& pt : track) {
                if (!is_usable(pt)) {
                    continue;
                }
                const double dx = pt.x - center_pt.x;
                const double dy = pt.y - center_pt.y;
                const double d2 = dx * dx + dy * dy;
                if (d2 > radius2) {
                    continue;
                }
                const double x_rot = cos_y * dx - sin_y * dy;
                const double y_rot = sin_y * dx + cos_y * dy;
                neighbors.emplace_back(x_rot, y_rot, pt.z);
                if (neighbors.size() >= options.local_max_neighbors) {
                    break;
                }
            }

            if (neighbors.size() < options.local_min_points) {
                const double dxp = prev_xy.x() - center_pt.x;
                const double dyp = prev_xy.y() - center_pt.y;
                const double dxn = next_xy.x() - center_pt.x;
                const double dyn = next_xy.y() - center_pt.y;
                const double x_rot_prev = cos_y * dxp - sin_y * dyp;
                const double x_rot_next = cos_y * dxn - sin_y * dyn;
                const double denom = x_rot_next - x_rot_prev;
                if (std::abs(denom) < 1e-6) {
                    continue;
                }
                const double slope = (next_z - prev_z) / denom;
                const double intercept = prev_z - slope * x_rot_prev;
                const double z_fit = intercept;  // center is at x_rot = 0
                const double residual = center_pt.z - z_fit;
                center_pt.z = static_cast<float>(residual);
                ++updated;
                continue;
            }

            Options local_opts = options;
            local_opts.min_points = options.local_min_points;
            Eigen::Vector3d normal;
            Eigen::Vector3d mean;
            if (!FitPlanePca2dXz(neighbors, local_opts, &normal, &mean)) {
                continue;
            }

            const double slope = -normal.x() / normal.z();
            const double intercept = mean.z() - slope * mean.x();
            const double z_fit = slope * 0.0 + intercept;  // center is at x_rot = 0
            const double residual = center_pt.z - z_fit;
            center_pt.z = static_cast<float>(residual);
            ++updated;
        }

        LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                  << " TangentLocalFit done (updated=" << updated << ")";
    }
}

void TrajectorySurfaceFitter::ApplyMovingPcaPerWheel(WheelTrajectory& trajectory) {
    Options options;
    ApplyMovingPcaPerWheel(trajectory, options);
}

void TrajectorySurfaceFitter::ApplyMovingPcaPerWheel(WheelTrajectory& trajectory, const Options& options) {
    std::vector<WheelPoint>* tracks[4] = {
        &trajectory.frontLeftTrajectory,
        &trajectory.frontRightTrajectory,
        &trajectory.rearLeftTrajectory,
        &trajectory.rearRightTrajectory
    };

    const double radius2 = options.local_radius * options.local_radius;
    const double inv_sigma2 = 1.0 / (options.local_sigma * options.local_sigma);

    for (int w = 0; w < 4; ++w) {
        auto& track = *tracks[w];
        int updated = 0;

        for (auto& center_pt : track) {
            if (!IsValidPoint(center_pt, options)) {
                continue;
            }

            std::vector<Eigen::Vector3d> neighbors;
            std::vector<double> weights;
            neighbors.reserve(options.local_max_neighbors);
            weights.reserve(options.local_max_neighbors);

            for (const auto& pt : track) {
                if (!IsValidPoint(pt, options)) {
                    continue;
                }
                const double dx = pt.x - center_pt.x;
                const double dy = pt.y - center_pt.y;
                const double dz = pt.z - center_pt.z;
                const double d2 = dx * dx + dy * dy + dz * dz;
                if (d2 > radius2) {
                    continue;
                }
                const double wgt = SafeExp(-0.5 * d2 * inv_sigma2);
                neighbors.emplace_back(pt.x, pt.y, pt.z);
                weights.push_back(wgt);
                if (neighbors.size() >= options.local_max_neighbors) {
                    break;
                }
            }

            Eigen::Vector3d normal;
            Eigen::Vector3d mean;
            bool fit_ok = FitPlaneWeighted(neighbors, weights, options, &normal, &mean);
            if (!fit_ok) {
                if (last_pca_planes_[w].valid) {
                    const double distance = last_pca_planes_[w].normal.dot(
                        Eigen::Vector3d(center_pt.x, center_pt.y, center_pt.z) -
                        last_pca_planes_[w].mean);
                    center_pt.z = static_cast<float>(distance);
                    ++updated;
                }
                continue;
            }

            const double distance = normal.dot(
                Eigen::Vector3d(center_pt.x, center_pt.y, center_pt.z) - mean);
            center_pt.z = static_cast<float>(distance);
            ++updated;
        }

        LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                  << " MovingPCA done (updated=" << updated << ")";
    }
}

bool TrajectorySurfaceFitter::FitHeightWeightedPlane(const std::vector<Eigen::Vector3d>& points,
                                                     const std::vector<double>& weights,
                                                     double* a,
                                                     double* b,
                                                     double* c) {
    if (points.size() < 3 || points.size() != weights.size()) {
        return false;
    }

    double s_xx = 0.0, s_xy = 0.0, s_x = 0.0;
    double s_yy = 0.0, s_y = 0.0, s_w = 0.0;
    double s_xz = 0.0, s_yz = 0.0, s_z = 0.0;

    for (size_t i = 0; i < points.size(); ++i) {
        const double w = weights[i];
        const double x = points[i].x();
        const double y = points[i].y();
        const double z = points[i].z();
        s_xx += w * x * x;
        s_xy += w * x * y;
        s_x += w * x;
        s_yy += w * y * y;
        s_y += w * y;
        s_w += w;
        s_xz += w * x * z;
        s_yz += w * y * z;
        s_z += w * z;
    }

    Eigen::Matrix3d mat;
    mat << s_xx, s_xy, s_x,
           s_xy, s_yy, s_y,
           s_x,  s_y,  s_w;

    Eigen::Vector3d rhs(s_xz, s_yz, s_z);
    const double det = mat.determinant();
    if (std::abs(det) < 1e-9) {
        return false;
    }

    Eigen::Vector3d sol = mat.ldlt().solve(rhs);
    *a = sol.x();
    *b = sol.y();
    *c = sol.z();
    return true;
}

bool TrajectorySurfaceFitter::FitLineWeightedXz(const std::vector<Eigen::Vector3d>& points,
                                                const std::vector<double>& weights,
                                                const Options& options,
                                                Eigen::Vector3d* normal,
                                                Eigen::Vector3d* mean) {
    if (points.size() < options.trunk_min_points || points.size() != weights.size()) {
        return false;
    }

    double s_w = 0.0;
    double s_x = 0.0;
    double s_z = 0.0;
    double s_xx = 0.0;
    double s_xz = 0.0;

    for (size_t i = 0; i < points.size(); ++i) {
        const double w = weights[i];
        const double x = points[i].x();
        const double z = points[i].z();
        s_w += w;
        s_x += w * x;
        s_z += w * z;
        s_xx += w * x * x;
        s_xz += w * x * z;
    }

    if (s_w <= 0.0) {
        return false;
    }

    const double denom = s_w * s_xx - s_x * s_x;
    if (std::abs(denom) < 1e-9) {
        return false;
    }

    const double a = (s_w * s_xz - s_x * s_z) / denom;
    const double b = (s_z - a * s_x) / s_w;
    const double mean_x = s_x / s_w;

    *mean = Eigen::Vector3d(mean_x, 0.0, a * mean_x + b);
    *normal = Eigen::Vector3d(-a, 0.0, 1.0);
    if (normal->z() < 0) {
        *normal = -*normal;
    }
    normal->normalize();

    if (std::abs(normal->z()) < options.min_normal_z) {
        return false;
    }
    return true;
}

void TrajectorySurfaceFitter::ApplyMlsPerWheel(WheelTrajectory& trajectory) {
    Options options;
    ApplyMlsPerWheel(trajectory, options);
}

void TrajectorySurfaceFitter::ApplyMlsPerWheel(WheelTrajectory& trajectory, const Options& options) {
    std::vector<WheelPoint>* tracks[4] = {
        &trajectory.frontLeftTrajectory,
        &trajectory.frontRightTrajectory,
        &trajectory.rearLeftTrajectory,
        &trajectory.rearRightTrajectory
    };

    const double radius2 = options.local_radius * options.local_radius;
    const double inv_sigma2 = 1.0 / (options.local_sigma * options.local_sigma);

    for (int w = 0; w < 4; ++w) {
        auto& track = *tracks[w];
        int updated = 0;

        for (auto& center_pt : track) {
            if (!IsValidPoint(center_pt, options)) {
                continue;
            }

            std::vector<Eigen::Vector3d> neighbors;
            std::vector<double> weights;
            neighbors.reserve(options.local_max_neighbors);
            weights.reserve(options.local_max_neighbors);

            for (const auto& pt : track) {
                if (!IsValidPoint(pt, options)) {
                    continue;
                }
                const double dx = pt.x - center_pt.x;
                const double dy = pt.y - center_pt.y;
                const double d2 = dx * dx + dy * dy;
                if (d2 > radius2) {
                    continue;
                }
                const double wgt = SafeExp(-0.5 * d2 * inv_sigma2);
                neighbors.emplace_back(pt.x, pt.y, pt.z);
                weights.push_back(wgt);
                if (neighbors.size() >= options.local_max_neighbors) {
                    break;
                }
            }

            if (neighbors.size() < options.local_min_points) {
                if (last_pca_planes_[w].valid) {
                    const double distance = last_pca_planes_[w].normal.dot(
                        Eigen::Vector3d(center_pt.x, center_pt.y, center_pt.z) -
                        last_pca_planes_[w].mean);
                    center_pt.z = static_cast<float>(distance);
                    ++updated;
                }
                continue;
            }

            double a = 0.0, b = 0.0, c = 0.0;
            if (!FitHeightWeightedPlane(neighbors, weights, &a, &b, &c)) {
                if (last_pca_planes_[w].valid) {
                    const double distance = last_pca_planes_[w].normal.dot(
                        Eigen::Vector3d(center_pt.x, center_pt.y, center_pt.z) -
                        last_pca_planes_[w].mean);
                    center_pt.z = static_cast<float>(distance);
                    ++updated;
                }
                continue;
            }

            const double z_fit = a * center_pt.x + b * center_pt.y + c;
            const double residual = center_pt.z - z_fit;
            center_pt.z = static_cast<float>(residual);
            ++updated;
        }

        LOG(INFO) << "TrajectorySurfaceFitter: " << kWheelNames[w]
                  << " MLS done (updated=" << updated << ")";
    }
}

// ===== 曲率检测实现（影子模式）=====

std::vector<double> TrajectorySurfaceFitter::MovingAverage(const std::vector<double>& data, int window) {
    if (data.empty() || window <= 0) return data;
    
    std::vector<double> result(data.size());
    const int half_win = window / 2;
    
    for (size_t i = 0; i < data.size(); ++i) {
        double sum = 0.0;
        int count = 0;
        for (int j = -half_win; j <= half_win; ++j) {
            int idx = static_cast<int>(i) + j;
            if (idx >= 0 && idx < static_cast<int>(data.size())) {
                sum += data[idx];
                ++count;
            }
        }
        result[i] = (count > 0) ? sum / count : data[i];
    }
    return result;
}

// 简单移动平均（不检查断点）
std::vector<double> TrajectorySurfaceFitter::MovingAverageSmooth(
    const std::vector<double>& data, int window) {

    if (data.empty() || window <= 0) {
        return data;
    }

    std::vector<double> result(data.size());
    const int half_win = window / 2;

    for (size_t i = 0; i < data.size(); ++i) {
        size_t start = (i > static_cast<size_t>(half_win)) ? (i - half_win) : 0;
        size_t end = std::min(data.size(), i + half_win + 1);

        double sum = 0.0;
        for (size_t j = start; j < end; ++j) {
            sum += data[j];
        }
        result[i] = sum / (end - start);
    }

    return result;
}

// 带断点保护的移动平均：只对空间连续的点做平滑
std::vector<double> TrajectorySurfaceFitter::MovingAverageWithGapCheck(
    const std::vector<double>& data,
    const std::vector<double>& x_vals,
    int window,
    double max_gap) {

    if (data.empty() || window <= 0 || data.size() != x_vals.size()) {
        return data;
    }

    std::vector<double> result(data.size());
    const int half_win = window / 2;
    
    for (size_t i = 0; i < data.size(); ++i) {
        double sum = data[i];
        int count = 1;
        
        // 向左扩展，遇到断点停止
        for (int j = 1; j <= half_win; ++j) {
            int idx = static_cast<int>(i) - j;
            if (idx < 0) break;
            
            // 检查是否有断点（当前点和前一个点的间距）
            double dx = x_vals[idx + 1] - x_vals[idx];
            if (dx > max_gap) break;  // 遇到断点，停止向左扩展
            
            sum += data[idx];
            ++count;
        }
        
        // 向右扩展，遇到断点停止
        for (int j = 1; j <= half_win; ++j) {
            int idx = static_cast<int>(i) + j;
            if (idx >= static_cast<int>(data.size())) break;
            
            // 检查是否有断点
            double dx = x_vals[idx] - x_vals[idx - 1];
            if (dx > max_gap) break;  // 遇到断点，停止向右扩展
            
            sum += data[idx];
            ++count;
        }
        
        result[i] = sum / count;
    }
    return result;
}

double TrajectorySurfaceFitter::EstimateFeatureWidth(const std::vector<double>& curvature,
                                                      const std::vector<double>& x_vals,
                                                      size_t center_idx,
                                                      double threshold) {
    if (curvature.empty() || center_idx >= curvature.size()) return 0.0;
    
    // 向左找边界
    size_t left_idx = center_idx;
    while (left_idx > 0 && std::abs(curvature[left_idx - 1]) > threshold * 0.5) {
        --left_idx;
    }
    
    // 向右找边界
    size_t right_idx = center_idx;
    while (right_idx < curvature.size() - 1 && std::abs(curvature[right_idx + 1]) > threshold * 0.5) {
        ++right_idx;
    }
    
    // 计算宽度
    if (left_idx < x_vals.size() && right_idx < x_vals.size()) {
        return std::abs(x_vals[right_idx] - x_vals[left_idx]);
    }
    return 0.0;
}

TrajectorySurfaceFitter::CurvatureResult TrajectorySurfaceFitter::DetectFeaturesByCurvature(
    const std::vector<WheelPoint>& track,
    const Options& options,
    const CurvatureOptions& curv_opts) {

    CurvatureResult result;
    result.features.resize(track.size(), FeatureType::NONE);

    // Step 1: 提取有效点的 (x, z) 序列，并记录原始索引映射
    std::vector<double> x_vals, z_vals;
    std::vector<size_t> valid_indices;  // valid_indices[i] = 原始track中的索引

    for (size_t i = 0; i < track.size(); ++i) {
        const auto& pt = track[i];
        if (pt.clz == 26.0f || pt.z == -1.0f) continue;
        if (pt.x < options.min_x || pt.x > options.max_x) continue;

        x_vals.push_back(pt.x);
        z_vals.push_back(pt.z);
        valid_indices.push_back(i);
    }

    const size_t N = x_vals.size();
    if (N < 50) {
        return result;  // 点太少，无法计算
    }

    // ========== 多尺度融合检测 ==========
    if (curv_opts.use_multiscale_fusion) {
        // 多尺度基准窗口：3m, 5m, 7.5m
        std::vector<int> baseline_windows = {61, 101, 151};
        std::vector<double> z_dev_fused(N, 0.0);

        for (int baseline_window : baseline_windows) {
            if (baseline_window >= static_cast<int>(N)) {
                continue;
            }

            // 建立基准线
            std::vector<double> z_baseline = MovingAverageSmooth(z_vals, baseline_window);

            // 计算偏差（取绝对值）
            std::vector<double> z_deviation(N);
            for (size_t i = 0; i < N; ++i) {
                z_deviation[i] = std::abs(z_vals[i] - z_baseline[i]);
            }

            // 小窗口平滑偏差
            std::vector<double> z_dev_smooth = MovingAverageSmooth(z_deviation, curv_opts.smooth_window);

            // MAX融合：取各尺度的最大偏差
            for (size_t i = 0; i < N; ++i) {
                z_dev_fused[i] = std::max(z_dev_fused[i], z_dev_smooth[i]);
            }
        }

        // 基于偏差阈值检测凹凸
        const double deviation_threshold = curv_opts.deviation_threshold;

        // 连续区域检测
        bool in_anomaly = false;
        size_t anomaly_start = 0;
        std::vector<std::pair<size_t, size_t>> anomaly_regions;

        for (size_t i = 0; i < N; ++i) {
            if (z_dev_fused[i] > deviation_threshold) {
                if (!in_anomaly) {
                    in_anomaly = true;
                    anomaly_start = i;
                }
            } else {
                if (in_anomaly) {
                    in_anomaly = false;
                    // 检查连续点数是否满足最小要求
                    if (i - anomaly_start >= static_cast<size_t>(curv_opts.min_anomaly_points)) {
                        anomaly_regions.push_back({anomaly_start, i - 1});
                    }
                }
            }
        }

        // 处理最后一个区域
        if (in_anomaly && N - anomaly_start >= static_cast<size_t>(curv_opts.min_anomaly_points)) {
            anomaly_regions.push_back({anomaly_start, N - 1});
        }

        // 标记特征点
        for (const auto& region : anomaly_regions) {
            size_t start = region.first;
            size_t end = region.second;

            // 计算区域宽度
            double width = x_vals[end] - x_vals[start];

            // 找最大偏差点
            double max_dev = z_dev_fused[start];
            for (size_t i = start + 1; i <= end; ++i) {
                if (z_dev_fused[i] > max_dev) {
                    max_dev = z_dev_fused[i];
                }
            }

            // 更新统计
            if (max_dev > result.max_deviation) {
                result.max_deviation = max_dev;
            }

            // 标记特征类型
            FeatureType feature_type;
            if (width < curv_opts.max_feature_width) {
                feature_type = FeatureType::BUMP;
                ++result.bump_regions;
            } else {
                feature_type = FeatureType::STEP;
                ++result.step_regions;
            }

            // 标记整个区域
            for (size_t i = start; i <= end; ++i) {
                size_t track_idx = valid_indices[i];
                result.features[track_idx] = feature_type;

                if (feature_type == FeatureType::BUMP) {
                    ++result.bump_count;
                } else {
                    ++result.step_count;
                }
            }
        }

        // 复用max_curvature字段存储最大偏差（向后兼容）
        result.max_curvature = result.max_deviation;

        return result;
    }

    // ========== 原有的曲率检测方法（保留作为fallback）==========
    const int step = curv_opts.curvature_step;
    const double normal_spacing = curv_opts.point_spacing;
    const double max_gap = normal_spacing * 2.5;

    std::vector<double> z_smooth = MovingAverageWithGapCheck(z_vals, x_vals, curv_opts.smooth_window, max_gap);

    std::vector<double> slope(N, 0.0);
    std::vector<double> curvature(N, 0.0);
    std::vector<bool> is_gap(N, false);

    // 预先检测断点位置
    for (size_t i = 1; i < N; ++i) {
        double dx = x_vals[i] - x_vals[i - 1];
        if (dx > max_gap) {
            is_gap[i] = true;
            is_gap[i - 1] = true;
        }
    }

    for (size_t i = 1; i < N - 1; ++i) {
        if (is_gap[i] || is_gap[i - 1] || is_gap[i + 1]) {
            continue;
        }

        double dx_left = x_vals[i] - x_vals[i - 1];
        double dx_right = x_vals[i + 1] - x_vals[i];
        double local_dx = dx_left + dx_right;
        if (std::abs(local_dx) > 1e-6) {
            slope[i] = (z_smooth[i + 1] - z_smooth[i - 1]) / local_dx;
        }
    }

    for (size_t i = static_cast<size_t>(step); i < N - static_cast<size_t>(step); ++i) {
        bool has_gap = false;
        for (int j = -step; j <= step; ++j) {
            if (is_gap[i + j]) {
                has_gap = true;
                break;
            }
        }
        if (has_gap) {
            continue;
        }

        double local_dx_ext = (x_vals[i + step] - x_vals[i - step]) / 2.0;
        if (std::abs(local_dx_ext) > 1e-6) {
            curvature[i] = (z_smooth[i + step] - 2.0 * z_smooth[i] + z_smooth[i - step])
                          / (local_dx_ext * local_dx_ext);
        }
    }

    for (size_t i = 0; i < N; ++i) {
        const size_t track_idx = valid_indices[i];

        if (std::abs(curvature[i]) > result.max_curvature) {
            result.max_curvature = std::abs(curvature[i]);
        }
        if (std::abs(slope[i]) > result.max_slope) {
            result.max_slope = std::abs(slope[i]);
        }

        if (std::abs(slope[i]) > curv_opts.step_slope_threshold) {
            result.features[track_idx] = FeatureType::STEP;
            ++result.step_count;
        }
        else if (std::abs(curvature[i]) > curv_opts.curvature_threshold) {
            double width = EstimateFeatureWidth(curvature, x_vals, i, curv_opts.curvature_threshold);
            if (width < curv_opts.max_feature_width) {
                result.features[track_idx] = FeatureType::BUMP;
                ++result.bump_count;
            }
        }
    }

    FeatureType last_type = FeatureType::NONE;
    for (size_t i = 0; i < result.features.size(); ++i) {
        FeatureType curr_type = result.features[i];
        if (curr_type != FeatureType::NONE && curr_type != last_type) {
            if (curr_type == FeatureType::BUMP) {
                ++result.bump_regions;
            } else if (curr_type == FeatureType::STEP) {
                ++result.step_regions;
            }
        }
        last_type = curr_type;
    }

    return result;
}

