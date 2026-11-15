"""
HTTP 回放接口示例脚本

演示如何通过 HTTP 接口控制回放流程：
1. 连接板端 gRPC 服务器
2. 加载回放配置文件
3. 开始回灌数据
4. 监控回放状态
5. 停止回灌

支持多场景连续回放：
  - 指定一个配置文件目录，脚本自动扫描其中的 replay_config_*.json
  - 按文件名排序后依次回放

使用前提：
  - 已启动上位机：python main_rviz.py
  - HttpServer 自动运行在 http://127.0.0.1:15000
  - 已安装依赖：pip install requests

运行方式：
  python http_replay_multi_scene.py                 # 使用默认配置目录
  python http_replay_multi_scene.py <config_dir>    # 指定配置目录
"""

import time
import requests
import json
import sys
import glob
import os

# -----------------------------------------------------------------------
# 配置参数
# -----------------------------------------------------------------------
BASE_URL = "http://127.0.0.1:15000"

# 回放配置文件所在目录：脚本会自动扫描该目录下所有 replay_config_*.json
REPLAY_CONFIG_DIR = r"C:\Users\lichao36\Desktop\eval\replay_scenes\replay_config"

# 配置文件名匹配模式（glob 通配）
CONFIG_PATTERN = "replay_config_*.json"

# 指定场景列表（优先级高于全场景扫描）：
#   - 列表为空 → 全场景模式：扫描 REPLAY_CONFIG_DIR 下所有 replay_config_*.json
#   - 列表非空 → 指定场景模式：仅按列表顺序回放下列场景
# 列表元素为场景名（与 replay_config_<场景名>.json 中的 <场景名> 一致）
SPECIFIC_SCENES = [
    # "2026-05-21_17-58-14_HLX33B166R1997378",
    # "2026-05-21_17-58-26_HLX33B166R1997378",
    # "2026-05-21_17-58-40_HLX33B166R1997378",
    # "2026-05-21_17-58-56_HLX33B166R1997378",
    # "2026-05-21_18-00-50_HLX33B166R1997378",
]

# 板端 gRPC 服务器地址
CAMERA_SERVER = "192.0.2.15:50051"
LIDAR_SERVER = "192.0.2.15:50052"

# 回放参数
REPLAY_MODE = "frame_by_frame"  # "stream" 或 "frame_by_frame"
REPLAY_SPEED = 1.0      # 播放速度倍率

# 监控参数
MONITOR_INTERVAL = 2.0  # 状态轮询间隔（秒）
SCENE_INTERVAL = 5.0    # 场景之间的间隔（秒）


# -----------------------------------------------------------------------
# 工具函数
# -----------------------------------------------------------------------

def print_section(title):
    """打印分隔线标题"""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def check_response(resp, operation):
    """检查 HTTP 响应并打印结果"""
    try:
        data = resp.json()
        code = data.get("code", resp.status_code)
        message = data.get("message", "")
        
        if resp.status_code == 200 and code == 200:
            print(f"[OK] {operation}: {message}")
            return True, data
        else:
            print(f"[FAIL] {operation} [HTTP {resp.status_code}, code {code}]: {message}")
            return False, data
    except Exception as e:
        print(f"[ERROR] {operation}: {resp.text}")
        return False, None


def test_http_server():
    """测试 HTTP 服务器连通性"""
    try:
        resp = requests.get(f"{BASE_URL}/replay/status", timeout=5)
        print(f"[OK] HttpServer 可达: {resp.status_code}")
        return True
    except requests.exceptions.ConnectionError:
        print("[ERROR] 无法连接到 HttpServer (http://127.0.0.1:15000)")
        print("        请确保已启动上位机: python main_rviz.py")
        return False
    except Exception as e:
        print(f"[ERROR] HttpServer 连接异常: {e}")
        return False


def connect_to_server():
    """连接板端 gRPC 服务器"""
    print(f"Camera: {CAMERA_SERVER}")
    print(f"LiDAR:  {LIDAR_SERVER}")
    print("注意: 此步骤可能需要 10-15 秒，请耐心等待...")
    
    try:
        resp = requests.post(
            f"{BASE_URL}/replay/connect",
            json={
                "camera": CAMERA_SERVER,
                "lidar": LIDAR_SERVER
            },
            timeout=25
        )
        success, data = check_response(resp, "连接服务器")
        return success
    except requests.exceptions.Timeout:
        print("[ERROR] HTTP 请求超时（25秒）")
        print("        板端服务器可能未运行或网络不通")
        return False
    except Exception as e:
        print(f"[ERROR] 连接请求异常: {e}")
        return False


def replay_scene(config_file, scene_index, total_scenes):
    """
    回放单个场景
    
    Args:
        config_file: 配置文件路径
        scene_index: 当前场景索引（从1开始）
        total_scenes: 总场景数
    
    Returns:
        bool: 是否成功完成回放
    """
    print_section(f"场景 {scene_index}/{total_scenes}: {config_file}")
    
    # 检查配置文件
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        print(f"配置文件: {config_file}")
        print(f"  FPS: {config.get('fps', 'N/A')}")
        print(f"  Cameras: {len(config.get('cameras', []))} 个")
        print(f"  LiDARs: {len(config.get('lidars', []))} 个")
        print(f"  Topics: {len(config.get('topics', []))} 个")
    except Exception as e:
        print(f"[ERROR] 配置文件读取失败: {e}")
        return False
    
    # 加载配置
    print("\n[Step 1] 加载回放配置...")
    try:
        resp = requests.post(
            f"{BASE_URL}/replay/load",
            json={"config_file": config_file},
            timeout=30
        )
        success, data = check_response(resp, "加载配置")
        if not success:
            return False
        
        if data and "data" in data:
            details = data["data"]
            print(f"  总帧数: {details.get('total_frames', 'N/A')}")
            print(f"  时长: {details.get('duration_s', 'N/A'):.2f} 秒")
    except Exception as e:
        print(f"[ERROR] 加载请求异常: {e}")
        return False
    
    # 开始回灌
    print("\n[Step 2] 开始回灌...")
    try:
        resp = requests.post(
            f"{BASE_URL}/replay/start",
            json={
                "mode": REPLAY_MODE,
                "speed": REPLAY_SPEED
            },
            timeout=15
        )
        success, _ = check_response(resp, "开始回灌")
        if not success:
            return False
    except Exception as e:
        print(f"[ERROR] 启动请求异常: {e}")
        return False
    
    # 监控回放状态
    print("\n[Step 3] 监控回放状态...")
    print(f"每 {MONITOR_INTERVAL} 秒轮询一次\n")
    
    start_time = time.time()
    last_frame = -1
    
    try:
        while True:
            time.sleep(MONITOR_INTERVAL)
            
            try:
                resp = requests.get(f"{BASE_URL}/replay/status", timeout=5)
                data = resp.json()
                
                if data.get("code") == 200 and "data" in data:
                    status = data["data"]
                    state = status.get("state", "UNKNOWN")
                    current_frame = status.get("current_frame_index", 0)
                    total_frames = status.get("total_frames", 0)
                    
                    # 计算进度
                    progress = (current_frame / total_frames * 100) if total_frames > 0 else 0
                    
                    # 计算帧率
                    fps_info = ""
                    if last_frame >= 0 and current_frame > last_frame:
                        actual_fps = (current_frame - last_frame) / MONITOR_INTERVAL
                        fps_info = f" | FPS: {actual_fps:.1f}"
                    last_frame = current_frame
                    
                    # 打印状态
                    elapsed = time.time() - start_time
                    print(f"[{elapsed:6.1f}s] {state:8s} | "
                          f"帧: {current_frame:4d}/{total_frames:4d} ({progress:5.1f}%){fps_info}")
                    
                    # 检查是否播放完成
                    if state in ("STOPPED", "IDLE") and current_frame >= total_frames - 1:
                        print(f"\n[OK] 场景 {scene_index} 回放完成！")
                        break
                        
            except Exception as e:
                print(f"[ERROR] 状态查询异常: {e}")
                
    except KeyboardInterrupt:
        print("\n[WARN] 用户中断回放（Ctrl+C）")
        return False
    
    # 停止回灌
    print("\n[Step 4] 停止回灌...")
    try:
        resp = requests.post(f"{BASE_URL}/replay/stop", timeout=10)
        check_response(resp, "停止回灌")
    except Exception as e:
        print(f"[ERROR] 停止请求异常: {e}")
    
    return True


# -----------------------------------------------------------------------
# 主流程
# -----------------------------------------------------------------------

def discover_configs(config_dir):
    """扫描配置目录，返回排序后的配置文件路径列表"""
    if not os.path.isdir(config_dir):
        print(f"[ERROR] 配置目录不存在: {config_dir}")
        return []

    pattern = os.path.join(config_dir, CONFIG_PATTERN)
    configs = sorted(glob.glob(pattern))
    return configs


def resolve_specific_configs(config_dir, scene_names):
    """按指定场景名列表生成配置路径，并校验每个文件存在。"""
    paths = [os.path.join(config_dir, f"replay_config_{name}.json") for name in scene_names]
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        for p in missing:
            print(f"[ERROR] 配置文件不存在: {p}")
        return []
    return paths


def main():
    # 支持命令行参数覆盖默认配置目录
    config_dir = sys.argv[1] if len(sys.argv) > 1 else REPLAY_CONFIG_DIR

    print_section("HTTP 多场景回放示例")
    print(f"配置目录: {config_dir}")

    # 优先级：SPECIFIC_SCENES 非空 → 指定场景模式；否则 → 全场景扫描模式
    if SPECIFIC_SCENES:
        print(f"模式  : 指定场景（共 {len(SPECIFIC_SCENES)} 个）")
        replay_configs = resolve_specific_configs(config_dir, SPECIFIC_SCENES)
        if not replay_configs:
            sys.exit(1)
    else:
        print(f"模式  : 全场景扫描")
        replay_configs = discover_configs(config_dir)
        if not replay_configs:
            print(f"[ERROR] 未在目录中找到匹配 {CONFIG_PATTERN} 的配置文件")
            sys.exit(1)

    print(f"共 {len(replay_configs)} 个场景待回放:")
    for i, cfg in enumerate(replay_configs, start=1):
        print(f"  {i:2d}. {os.path.basename(cfg)}")

    # Step 0: 测试 HTTP 服务器
    print_section("Step 0: 测试 HTTP 服务器连通性")
    if not test_http_server():
        sys.exit(1)
    
    # Step 1: 连接板端服务器（只需连接一次）
    print_section("Step 1: 连接板端 gRPC 服务器")
    if not connect_to_server():
        print("[WARN] 连接失败，但继续执行（某些功能可能不可用）")
    
    # Step 2: 依次回放每个场景
    success_count = 0
    total = len(replay_configs)
    for i, config_file in enumerate(replay_configs, start=1):
        if replay_scene(config_file, i, total):
            success_count += 1
            
            # 场景之间的间隔（最后一个场景不需要）
            if i < total:
                print(f"\n等待 {SCENE_INTERVAL} 秒后开始下一个场景...")
                time.sleep(SCENE_INTERVAL)
        else:
            print(f"[ERROR] 场景 {i} 回放失败")
            user_input = input("是否继续下一个场景？(y/n): ").strip().lower()
            if user_input != 'y':
                break
    
    # 总结
    print_section("回放完成")
    print(f"成功: {success_count}/{total} 个场景")
    print("如需再次回放，请重新运行此脚本")


if __name__ == "__main__":
    main()
