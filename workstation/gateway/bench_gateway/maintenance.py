"""Operator-owned, hot-readable admission notice; running workers are unaffected."""
import json
from pathlib import Path

MESSAGE='台架评测正在优化，暂时停止受理新材料和新任务，请稍后体验。已有任务和材料记录保留；本条消息或附件未登记，请恢复后重新提交。'

def notice(cfg):
    path=Path(cfg['project_root'])/'runtime/maintenance.json'
    if not path.exists():return ''
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        return MESSAGE if value.get('enabled') is True else ''
    except (OSError,ValueError,AttributeError):return MESSAGE
