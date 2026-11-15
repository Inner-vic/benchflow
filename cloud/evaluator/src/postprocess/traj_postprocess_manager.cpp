#include "postprocess/traj_postprocess_manager.hpp"

// PCA trunk line 拟合（无帧间历史，对应预测 ApplyPcaHeightOnFilteredTrajectory 的核心步骤）
void EvalTrajPostprocessManager::applyPca(WheelTrajectory& trajectory)
{
    fitter_.ApplyIterativeTrunkLinePerWheel(trajectory, options_);
}
