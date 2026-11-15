/*!
 * @file traj_postprocess_manager.hpp
 * @brief 离线评测真值轨迹 PCA 拟合器
 *
 * 真值轨迹从多帧拼接的稠密点云（VCS 坐标系，已去噪）按 xy 直接提取，
 * 只需做 PCA trunk line 拟合以修正 z 值，与预测后处理步骤对齐：
 *   - applyPca    ：PCA trunk line 拟合（无帧间历史，参数与预测对齐）
 *   - resetSceneState ：场景切换时重置拟合器内部状态
 *
 * 不采用的步骤（与预测不对称，不应施加于真值）：
 *   - fillFrontFromRear  ：点云稠密，xy 直接提取，无需填补
 *   - 帧间运动补偿        ：点云已 VCS，无需坐标系变换
 *   - Kalman/空间滤波    ：不影响真实空间位置
 *   - 地图目标融合       ：真值不参与地图融合
 */
#ifndef _TRAJ_POSTPROCESS_MANAGER_HPP_
#define _TRAJ_POSTPROCESS_MANAGER_HPP_

#include "data/wheel_trajectory.hpp"
#include "postprocess/trajectory_surface_fitter.hpp"
using namespace uroad_m100;

class EvalTrajPostprocessManager {
public:
    EvalTrajPostprocessManager() = default;

    // 设置 PCA 拟合参数（从 YAML 加载后传入）
    void setOptions(const TrajectorySurfaceFitter::Options& opts) { options_ = opts; }

    // 场景切换时调用：重置拟合器内部平面缓存
    void resetSceneState() { fitter_.Reset(); }

    // PCA trunk line 拟合（无帧间历史）
    void applyPca(WheelTrajectory& trajectory);

private:
    TrajectorySurfaceFitter          fitter_;
    TrajectorySurfaceFitter::Options options_;
};

#endif // _TRAJ_POSTPROCESS_MANAGER_HPP_
