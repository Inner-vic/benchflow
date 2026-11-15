/*!
 * @file main_eval.cpp
 * @brief 离线评测程序入口
 *
 * 用法：
 *   uroad_offline_eval <evaluation_param.yaml>
 *   默认配置：./etc/evaluation_param.yaml
 */
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <string>
#ifdef _WIN32
#include <windows.h>
#endif

#include "compat/logging_compat.hpp"
#include "evaluator.hpp"

/* 当前版本软件更改
- 扩展为减速带(16)/井盖(17)/大桥接缝(23)统一评测框架
- 跨帧锁帧对齐App UpdateLockedFeatures（err < min_dist持续更新）
- 锁帧判据：16用peak_x，17/23用x_leading；误差：16用x_peak/z_peak，17/23用x_leading/z_mean
- FP/FN仅在pred锁定帧统计，避免重复计数；加eval_x_max远场过滤
- 命名统一：SpeedBump→TargetObject，BumpTrack→TargetTrack，YAML段speed_bump→target_object
- 清理：删除凹凸区分、x_trailing、resetSpeedBumpDefaults、点级统计等冗余代码
*/

int main(int argc, char** argv)
{
#ifdef _WIN32
    // 将控制台输出代码页切换为 UTF-8，避免中文乱码
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);
#endif

    namespace fs = std::filesystem;

    std::string yaml_path;
    if (argc >= 2) {
        yaml_path = argv[1];
    } else {
        fs::path cwd_config = fs::current_path() / "etc" / "evaluation_param.yaml";
        if (fs::exists(cwd_config)) {
            yaml_path = cwd_config.string();
        } else {
            fs::path exe_path = fs::absolute(argv[0]);
            fs::path exe_dir_config = exe_path.parent_path().parent_path() / "etc" / "evaluation_param.yaml";
            if (fs::exists(exe_dir_config)) {
                yaml_path = exe_dir_config.string();
            } else {
                yaml_path = "./etc/evaluation_param.yaml";
            }
        }
    }

    LOG(INFO) << "=================================================";
    LOG(INFO) << "  uroad_offline_eval";
    LOG(INFO) << "  config: " << yaml_path;
    LOG(INFO) << "=================================================";

    OfflineEvaluator evaluator;
    if (!evaluator.initialize(yaml_path)) {
        LOG(ERROR) << "OfflineEvaluator 初始化失败";
        return EXIT_FAILURE;
    }
    if (!evaluator.runAll()) {
        LOG(ERROR) << "OfflineEvaluator 执行失败";
        return EXIT_FAILURE;
    }
    evaluator.writeFinalStats();
    return EXIT_SUCCESS;
}
