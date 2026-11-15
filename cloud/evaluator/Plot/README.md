# uroad 点云与预测数据可视化工具

独立、轻量的桌面工具，用于对比离线评测输入中的真值点云/栅格与预测路面/轨迹点。它不修改评测数据，也不依赖车端 DAG、NPU、PCL、Open3D 或 VTK。

## 能力

- 分别选择并叠加显示一份真值数据和一份预测数据；
- 可选择真值/预测场景目录，自动列出同时存在 `dense_labeled` 与 `pred_traj` 的精确时间戳共同帧，并支持上一帧/下一帧导航；
- 依据语义 `label` 以固定鲜艳颜色显示点；相同 label 在真值与预测中始终使用同一颜色；
- 动态显示标签图例、类别名称以及 GT/Pred 的原始点数；
- 标签过滤与图例区域支持独立拉伸和滚动，确保所有 label 都可访问；
- 记住上次的真值/预测场景及共同帧，重启后自动恢复；
- 可在当前 GT/Pred 场景父目录中的同名可用场景间切换上一/下一场景；
- 右上角真值图片支持等比例拖拽缩放；
- 支持 A/B 双点 FIFO 比较：两个高亮点在点云中持续显示，详情并排展示；
- 默认隐藏无效/空标签 `0`、`26`，可以显式打开显示；
- 支持旋转、平移、缩放、自动重置视角和 PNG 截图；
- 左键短点击选择当前可见点，显示来源、x/y/z、label、类别名和轨迹车轮；
- 提供 Y-X、X-Z、Y-Z、鸟瞰四种固定视角及快捷键切换；
- 大点云按 label 分层采样，默认每一侧最多绘制 300,000 点，状态栏显示原始/筛选/实际绘制数量。

## 支持格式

| 类型 | 文件模式 | 格式 | 坐标 |
|---|---|---|---|
| 真值稠密点云 | `dense_labeled_*.npy` | `[N,5] = [x,y,z,label,drivable]` | 物理米坐标 |
| 真值 OCC | `occ_*.npy` | `[N,5] = [x_idx,y_idx,z_idx,label,drivable]` | 栅格索引，不是米坐标 |
| 路面预测 | `pred_road_*.npy` | `[N,4] = [x,y,z,label]` | 物理米坐标 |
| 轨迹预测 | `pred_traj_*.npy` | 16B 四轮点数 header + 四段 `[x,y,z,label] float32` | 物理米坐标 |

`pred_traj` 将四个车轮点合并绘制，但以 **label** 着色；报告中的误检诊断仍可使用离线评测工具查看车轮信息。

首版不显示 `traj_xy_*.npy` 和 `pred_img_*.json`：它们不是可按 3D label 点集显示的数据。

## 安装

### Windows PowerShell

```powershell
cd Plot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python run.py
```

可选：运行解析与颜色逻辑测试：

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m unittest discover -s tests -v
```

如果 PowerShell 禁止激活脚本，可在当前终端临时允许：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

### Linux

```bash
cd Plot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python run.py
```

可选：运行解析与颜色逻辑测试：

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

Linux 需有桌面 OpenGL 环境（本机桌面、X11/Wayland 转发等）。纯 headless shell 无法启动 GUI。

## 使用方式

### 场景浏览模式（推荐）

1. 在“场景与帧浏览”中选择真值场景目录，例如：

   ```text
   D:\\ASU\\PCM_eval_v14\\scenes\\2026-06-30_18-42-09_HLX34B167T1300224
   ```

   工具自动优先扫描其 `dataset/` 子目录；不存在时才扫描场景目录本身。

2. 选择预测场景目录，例如：

   ```text
   D:\\ASU\\eval_pred_result\\eval_pred_result_20260817_2563\\2026-06-30_18-39-31_HLX34B167T1300224
   ```

3. 点击 **扫描共同帧**。列表只显示同时满足以下条件的帧：真值/预测帧目录可解析出完全相同的 13 位毫秒 timestamp、真值帧内有 `dense_labeled_*.npy`、预测帧内有 `pred_traj_*.npy`。匹配不按帧序号，也不使用时间容差。
4. 选择如 `7_1782815972654` 的共同帧，工具自动加载 dense 真值点云与 pred_traj 预测轨迹。
5. 使用 **上一帧 / 下一帧** 按共同帧时间顺序浏览；到首尾帧时相应按键自动禁用。切帧会保持当前旋转、缩放和移动视角；若新帧不在当前画面范围内，按 `R` 重新适配。
6. 使用 **上一场景 / 下一场景** 可在当前选择场景父目录中的同名 GT/Pred 场景之间切换。程序会自动跳过没有有效共同帧的场景，并从目标场景时间最早的有效共同帧开始显示；切换场景时会重新适配点云视角。
7. 右上角真值图片窗口会同步当前真值帧：优先选择文件名包含 `f_ca_120` 的 `.jpg/.jpeg/.png`。标题栏按钮可隐藏/展开，右下角拖动时窗口保持图片原始宽高比例。
8. 左侧 **A / B 选中点** 支持比较两个不同点：首次点击填 A、第二个不同点填 B；随后点击新的不同点按 FIFO 刷新（旧 B→A，新点→B）。重复点击 A/B 已有点不变化；使用“清除 A/B”可重新开始。

### 手工单文件模式

1. 点击顶部 **加载真值文件**，选择 `dense_labeled_*.npy` 或 `occ_*.npy`。
2. 点击 **加载预测文件**，选择 `pred_road_*.npy` 或 `pred_traj_*.npy`。
3. 左侧按 label 勾选要显示的点；同一标签在两侧显示同样的颜色。标签区支持三列紧凑图例，显示色块、名称和 GT/Pred 点数。
4. 使用数据源的“显示”、点大小和透明度控制叠加效果。
5. 在 3D 图中左键短点击一个实际显示的点，左侧“选中点”显示来源、坐标、label 和车轮；点击空白处会取消选择。
6. x/y/z 坐标轴会根据当前可见点范围自动调整主刻度；主刻度最细为 `0.01m`（1cm）。真值 OCC 使用 `idx` 栅格索引单位，不应与米坐标数据直接比较。
7. 使用工具栏重置视角或截图。

### 鼠标与快捷键

| 操作 | 控制 |
|---|---|
| 旋转 | 左键拖动（移动超过约 4 像素） |
| 选择点 | 左键短点击 |
| 平移 | 中键拖动 |
| 缩放 | 鼠标滚轮 |
| Y-X 视角 | `1` |
| X-Z 视角 | `2` |
| Y-Z 视角 | `3` |
| 鸟瞰视角 | `4` |
| 重置视角 | `R` |
| 截图 | `S` |

## 常见问题

### 文件加载失败或维度异常

确认选用的是受支持文件。预测轨迹必须是 App 落盘的 `pred_traj_*.npy` 字节流；不能选择普通的 4 列 NPY 后改名为 `pred_traj`。

### 真值 OCC 与预测路面位置不重合

OCC 的 xyz 是栅格索引，而 `pred_road` 是物理米坐标，不能直接叠加做空间对齐。请使用稠密点云与轨迹预测，或在离线评测中完成 OCC 坐标转换后再判断对应关系。

### 百万点云卡顿

降低“每数据源最大绘制点数”，例如 100,000；或使用标签过滤，只保留需要分析的类别。工具会优先保持每个可见 label 都有样本。

### `ModuleNotFoundError`

确认已在 `Plot/` 下运行，并在测试时设置项目路径：

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

运行界面只需：

```bash
python run.py
```
