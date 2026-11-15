"""Bounded compiler diagnostics; no commands or arbitrary log text reach chat."""
import re

MANUAL_ACTION='请手动编译与本APP及本次相机、雷达配套的 liburoad_m100.so，再提供本人工作站路径、下载链接或允许大小内的附件；其余材料保留，不自动进入台架。'

def diagnose(text):
    for line in text[-2*1024*1024:].splitlines():
        if 'error:' not in line:continue
        match=re.search(r'has no member named\s+[\x27\x22‘’]([A-Za-z_][A-Za-z_0-9]{0,100})',line)
        if match:
            provider='XLidar' if 'XLidar::' in line else 'XCamera' if 'XCamera::' in line else '依赖库'
            return {'code':'M100_COMPILER_API_MISMATCH','provider':provider,'symbol':match[1],
                    'message':'本APP自动编译失败：'+provider+'开发头文件缺少接口 '+match[1]+'；未修改业务代码或自动替换依赖。','next_action':MANUAL_ACTION}
    if "doesn't match the locked one" in text or 'does not match the locked' in text:
        return {'code':'M100_DEPENDENCY_LOCK_MISMATCH','message':'本APP自动编译的依赖版本或包ID无法与构建锁对齐，已停止。','next_action':MANUAL_ACTION}
    return {'code':'M100_BUILD_FAILED','message':'本APP自动编译未完成，已停止，不自动换版本或反复重编。','next_action':MANUAL_ACTION}
