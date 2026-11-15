"""Facts about the agreed cloud data source; asking never changes a task."""
import re

GUIDE = '''评测数据说明
专用开发空间：bench-eval-offline（整车电动-智能底盘，团队可见）。
回放数据由用户指定路径，不默认选择PCM、V151或其他数据集。评测必须使用该任务冻结数据源明确关联的GT根目录；未关联时提示补充，不使用通用默认真值。
同目录组织的场景通常为“场景名/original”与“场景名/dataset”，关联后根据完整场景标识匹配，只评测本次场景。找不到或存在多个版本时明确提示，不猜选。
回放结果目录由系统生成，无需用户填写。预测和 TXT 报告保存到专用评测空间的独立持久化任务目录，不写回输入数据卷或GT目录。新增数据卷需管理员配置挂载，普通聊天不会自动挂载。
已有任务保留原后端及冻结来源，不改写历史收据；缺少视觉GT等有效样本时如实交付部分结果。
平台设有4小时空闲停止策略；实例停止时提示管理员启动，保留已有回放结果，不自动重跑回放或切换评测后端。发送“评测环境”可查询实时状态。此说明不会提交或修改任务。'''


def answer(text):
    """Route short informational questions, never mixed material/control requests."""
    compact = ''.join(text.strip().split()).lower().rstrip('。！？!?')
    if len(compact) > 240 or '\n' in text or '\r' in text:
        return None
    if re.search(r'https?://|/mnt/|/lpai/|\.so\b|\.ya?ml\b|修改|替换|切换|改为|改成|提交|确认执行|开始执行|取消评测|本次仅回放|本次回放后评测', compact):
        return None
    if compact in {'评测数据说明', '数据卷说明', '云端评测说明', 'gt说明', '数据集在哪里',
                   '默认评测目录', '评测数据路径', 'v151路径', 'v151地址', 'gt路径'}:
        return GUIDE
    subject = any(word in compact for word in ('数据卷', '云端gt', '开发空间', 'v151', 'gt路径',
                                               'gt在哪', 'gt怎么', 'gt如何', '评测数据'))
    inquiry = any(word in compact for word in ('什么', '哪个', '哪里', '在哪', '怎么', '如何', '吗', '说明', '逻辑'))
    return GUIDE if subject and inquiry else None
