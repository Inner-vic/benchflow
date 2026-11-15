# 历史上游评测器说明

本文件保留导入时原说明。Windows构建及build.ps1等未随此仓库交付，不作为当前部署步骤。统一入口在Linux/云端使用固定评测器；不要按本文覆盖GT、指标或运行后端。

# uroad_offline_eval — 离线评测工具

读取 `uroad-new` 推理输出和带标真值数据，离线计算路面 IoU/mAP、轨迹 Z_MAE/Acc_1cm/Pass_Rate、减速带匹配误差、视觉分类 mAP 等指标。完全脱离 DAG 框架与 GPU 依赖，在 PC（x64）上直接运行。

---

## 目录结构

```
uroad/
├── src/
│   ├── main.cpp                   # 入口
│   ├── evaluator.cpp              # OfflineEvaluator 主驱动
│   ├── postprocess/
│   │   ├── surface_fitter.cpp     # PCA 曲面拟合
│   │   └── traj_postprocessor.cpp # 真值轨迹后处理（fillFront + PCA，稠密点云已 VCS 去噪，无需运动补偿）
│   └── io/
│       └── pcd_writer.cpp         # 可视化 PCD 输出
├── include/
│   ├── compat/logging_compat.hpp  # 轻量日志（替代 glog，自动加 [uroad-eval] 前缀）
│   ├── evaluator.hpp
│   ├── eval/{iou,map}.hpp         # 路面 IoU / 视觉 mAP
│   ├── postprocess/{surface_fitter,traj_postprocess_manager}.hpp
│   └── io/pcd_writer.hpp
├── idls/                          # 数据 IDL 定义
├── etc/evaluation_param.yaml      # 评测配置
├── conanfile.py                   # 依赖：vbs, vbs-idlgen, yaml-cpp, eigen
├── CMakeLists.txt
├── build.sh                       # 一键编译（lidocker 内）
└── run.sh                         # 一键运行（宿主机，自动设 LD_LIBRARY_PATH）
```

---

## 一、依赖

本工具不依赖 App 侧 DAG、GPU 或 NPU 运行时；仅需 `yaml-cpp` 和 Eigen3。按使用的平台准备相应的构建环境：

| 平台 | 编译工具 | 依赖安装方式 |
|------|----------|--------------|
| Windows x64 | Visual Studio 2019/2022（MSVC）+ CMake + vcpkg | `yaml-cpp:x64-windows`、`eigen3:x64-windows` |
| Linux x86_64 | CMake + Ninja + GCC/Clang | 系统包 `libyaml-cpp-dev`、`libeigen3-dev` |

---

## 二、Windows 原生编译（推荐）

工程已在 Windows 下通过 **MSVC + CMake + vcpkg** 编译和运行。历史构建缓存位于 `build-win/CMakeCache.txt`：使用 Visual Studio 2019、MSVC v142、`C:\vcpkg` 和 `x64-windows` triplet；当前 `build.ps1（build.ps1）` 同时兼容 Visual Studio 2019 / 2022。

> Windows 不使用 `build.sh（build.sh）` 或 `run.sh（run.sh）`；这两个脚本仅用于 Linux / Bash。Windows 请使用仓库提供的 PowerShell 脚本。

### 1. 前置条件

安装 Visual Studio 2019/2022（或相应 Build Tools），并选择以下组件：

- **Desktop development with C++**
- MSVC x64 编译工具集（v142 或 v143）
- Windows SDK、CMake
- `vcpkg`

本工程使用 MSVC 动态运行时，因此必须使用 `x64-windows`，而非 `x64-windows-static`。

若本机尚未安装依赖，在 PowerShell 中执行：

```powershell
# 以实际 vcpkg 所在目录为准；历史环境为 C:\vcpkg
C:\vcpkg\vcpkg.exe install yaml-cpp:x64-windows eigen3:x64-windows
```

可选：设置环境变量，便于脚本定位 vcpkg：

```powershell
$env:VCPKG_ROOT = 'C:\vcpkg'
```

### 2. 编译

在工程根目录的 PowerShell 中运行：

```powershell
cd D:\ASU\uroad-dev-eval-20260721
.\build.ps1
```

默认构建配置为：

- 构建目录：`build-win`
- 配置：`Release`
- vcpkg triplet：`x64-windows`
- 输出：`build-win\Release\uroad_offline_eval.exe`

常用命令：

```powershell
.\build.ps1 -Config Debug  # Debug 构建
.\build.ps1 -Clean         # 删除 build-win 后重新配置、编译
.\build.ps1 -BuildOnly     # 仅编译已经配置好的 build-win
```

`build.ps1` 会依次从 `VCPKG_ROOT`、现有 `build-win/CMakeCache.txt`、`C:\vcpkg` 和 `D:\vcpkg` 中查找 `vcpkg.cmake`。如上述位置均不适用，可明确指定：

```powershell
.\build.ps1 -ToolchainFile 'E:\tools\vcpkg\scripts\buildsystems\vcpkg.cmake'
```

运行时需要 `yaml-cpp.dll`。正常构建会将其放在可执行文件同目录，即 `build-win\Release\yaml-cpp.dll`，请勿单独移动 `uroad_offline_eval.exe` 而遗漏该 DLL。

### 3. 运行（Windows）

确认 `etc/evaluation_param.yaml（etc/evaluation_param.yaml）` 中的 `dataset.labeled_data_root`、`dataset.pred_data_root` 和 `dataset.scene_folder_prefix` 与本机数据一致后，执行：

```powershell
.\run.ps1
```

也可以让运行脚本先编译再启动：

```powershell
.\run.ps1 -BuildFirst
.\run.ps1 -Config Debug -BuildFirst
```

评测报告会输出到工程根目录的 `eval_report\`。更完整的 Windows 排查说明见 `docs/windows_native_build.md（docs/windows_native_build.md）`。

---

## 三、Linux 编译（系统依赖）

```bash
sudo apt install libyaml-cpp-dev libeigen3-dev ninja-build cmake
./build.sh                # release 编译（默认）
./build.sh -b debug       # debug 编译
./build.sh -c             # 清理 build 目录
```

产物：`build/uroad_offline_eval`（Linux x86-64 ELF）。

---

## 四、配置

修改 `etc/evaluation_param.yaml（etc/evaluation_param.yaml）` 中的 `dataset` 段：

```yaml
dataset:
  # 真值根目录（必填）
  labeled_data_root: /path/to/Labeled_Data-xxx
  # App 预测落盘根目录（必填，场景结构与真值对齐）
  pred_data_root: /path/to/eval_dump/predictions
  # 场景过滤前缀
  scene_folder_prefix: L7_SH_
  # 调试：0=全量，>0=仅评前 N 个场景
  max_scenes_for_debug: 0
```

Windows 路径建议使用正斜杠，例如 `D:/ASU/PCM_eval_v14/scenes_xxx`；或使用加引号的双反斜杠形式，如 `"D:\\ASU\\PCM_eval_v14\\scenes_xxx"`。

各评测任务由 `eval_tasks.eval_road`、`eval_tasks.eval_traj` 和 `eval_tasks.eval_vision` 独立控制。运行时每个预测帧固定按“路面 → 轨迹/目标物 → 视觉”顺序尝试评测：每项指标只要求本任务的预测与真值可用，某项缺失不会阻断同帧其他任务。最终报告同时给出“至少完成一项任务”的帧级统计，以及路面、轨迹/目标物、视觉各自的实际 KPI 样本数与缺失原因。其他评测阈值（轨迹 Z 容差、减速带匹配范围、IoU 网格等）已有默认值，按需调整。

---

## 五、运行

### Windows

```powershell
.\run.ps1                              # 默认 etc\evaluation_param.yaml
.\run.ps1 -BuildFirst                  # 先编译再运行
.\run.ps1 -Config Debug                # 运行 Debug 产物
```

### Linux

```bash
./run.sh                              # 默认 etc/evaluation_param.yaml
./run.sh etc/evaluation_param.yaml    # 显式指定配置
```

### 输出

- **stderr**：实时进度，格式 `[I file:line] [uroad-eval] ...`（对齐 App 侧 `[uroad]` 前缀风格）
- **stdout**：最终汇总（样本统计、路面 IoU、轨迹 Z_MAE/Acc/Pass、减速带、视觉 mAP）
- **eval_report/**：与 stdout 同步的文本报告

---

## 五、数据目录约定

真值与预测必须**场景对齐**：

```
labeled_data_root/                        pred_data_root/
├── L7_SH_2025xxxx_scene01/               ├── L7_SH_2025xxxx_scene01/
│   ├── 1700000000123.json/.npy           │   ├── 1700000000123/
│   │   (ground truth occ/dense/vision)   │   │   ├── pred_road_1700000000123.npy
│   └── ...                               │   │   ├── pred_traj_1700000000123.npy
└── L7_SH_2025xxxx_scene02/               │   │   └── pred_img_1700000000123.npy
                                          │   └── ...
                                          └── L7_SH_2025xxxx_scene02/
```

按 13 位毫秒时间戳匹配（10ms 容差）。预测数据落盘细节见 `docs/phase2_eval_dumper.md`。

---

## 六、常见问题

| 现象 | 排查 |
|------|------|
| `OfflineEvaluator: 配置文件加载失败` | 第一参数路径错；确认 yaml 文件存在 |
| `未找到匹配真值` 大量出现 | `labeled_data_root` 路径错或时间戳不对齐；检查 `scene_folder_prefix` 是否过滤掉了所有场景 |
| `cannot find -lvbs / libyaml-cpp.so` | 不要直接调 `./build/bin/uroad_offline_eval`，请用 `./run.sh`（自动设置 LD_LIBRARY_PATH）|
| `run.sh` 报路径找不到 .so | `~/.lidocker/.conan/` 不存在 → 检查 lidocker 是否正常启动过；或编辑 run.sh 把 `host_conan` 改成你的 Conan 缓存路径 |
| `conan install` 失败 | 网络问题，加 `./build.sh -s` 从源码编译；或检查 `liconan-dev-local` 远程认证 |
| `idl_samplePubSubTypes.cxx: multiple definition` | 执行 `./build.sh -c` 清理后重新编译（IDL 去重逻辑按内容哈希处理，正常不应出现） |
