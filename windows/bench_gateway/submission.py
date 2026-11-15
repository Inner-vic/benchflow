"""Parse the explicit, line-based submission form without model interpretation."""
import re

from .core import GatewayError
from .intake import CLEAR_YAML, SUBMISSION_TEMPLATE, validate


TITLES={'新建回放任务':'new_task','补充任务材料':'prepare'}
LABELS={
    '软件名称':'software_name','软件包':'package','相机so':'camera_so',
    '雷达so':'lidar_so','m100so':'m100_so','数据集':'dataset',
    '场景范围':'scene_scope','任务目标':'goal','yaml修改':'yaml_request',
    '交付方式':'lpai_target','构建来源':'build_reference',
    '装配存档':'assembly_ref',
    '回放方式':'replay_mode',
}
OPTIONAL={'software_name','build_reference','yaml_request'}
GOALS={'仅回放':'replay','回放后评测':'replay_and_evaluate'}
TITLES.update({k.replace('回放','回灌'):v for k,v in list(TITLES.items()) if '回放' in k})
GOALS.update({k.replace('回放','回灌'):v for k,v in list(GOALS.items()) if '回放' in k})
DELIVERY={'工作站目录':'none','团队LPAI':'team','个人LPAI':'personal'}


def parse_form(text):
    """Return an intent only for an exact form title; ordinary prose stays with Codex.

    Every nonempty following line must be one known ``label: value`` pair.
    Continuations are deliberately rejected, including YAML blocks: this keeps a
    second command or a misspelled field from silently changing the task meaning.
    """
    if not isinstance(text,str):return None
    lines=text.splitlines()
    if not lines or lines[0] not in TITLES:return None
    values={};seen=set()
    for number,line in enumerate(lines[1:],2):
        if not line.strip():continue
        match=re.match(r'^([^:：]+)[:：](.*)$',line)
        if not match:
            raise GatewayError('任务模板第 '+str(number)+' 行缺少“字段：内容”；模板不支持续行，YAML 要求请写在同一行或另发自然语言')
        label=re.sub(r'\s+','',match.group(1)).lower()
        key=LABELS.get(label)
        if key is None:
            raise GatewayError('任务模板包含未知字段“'+match.group(1).strip()+'”；请使用固定模板，未保存本条材料')
        if key in seen:
            raise GatewayError('任务模板字段重复：“'+match.group(1).strip()+'”；请只保留一行，未保存本条材料')
        seen.add(key);value=match.group(2).strip()
        if not value:
            if key not in OPTIONAL:
                raise GatewayError('请填写“'+match.group(1).strip()+'”，或暂时删去该行后补充；未保存本条材料')
            # Omitted optional values never clear a previously supplied request.
            continue
        if value.startswith('<') and value.endswith('>'):
            raise GatewayError('“'+match.group(1).strip()+'”仍是模板占位说明，请替换为真实内容或暂时删去该行')
        if key=='goal':
            if value not in GOALS:raise GatewayError('任务目标只支持“仅回放”或“回放后评测”；未保存本条材料')
            value=GOALS[value]
        elif key=='lpai_target':
            if value not in DELIVERY:raise GatewayError('交付方式只支持“工作站目录”“团队LPAI”或“个人LPAI”；未保存本条材料')
            value=DELIVERY[value]
        elif key=='replay_mode':
            modes={'流式回放':'stream','逐帧回放':'frame_by_frame','stream':'stream','frame_by_frame':'frame_by_frame'}
            if value not in modes:raise GatewayError('回放方式只支持“流式回放”或“逐帧回放”。')
            value=modes[value]
        elif key=='m100_so':
            if value=='尝试编译':
                values['m100_mode']='build_if_missing'
                continue
            values['m100_mode']='provided'
        elif key=='yaml_request' and value==CLEAR_YAML:
            value=CLEAR_YAML
        values[key]=value
    if values.get('assembly_ref') and any(values.get(key) for key in ('package','camera_so','lidar_so','m100_so','m100_mode')):
        raise GatewayError('装配存档不能与新软件包或 SO 原料混填；请选择一个软件起点，未保存本条材料')
    return {'action':TITLES[lines[0]],'inputs':validate(values)}
