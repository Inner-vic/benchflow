# Jenkins 配置卡片独立模块

> 早期独立模块说明，保留兼容接口与历史验证。当前 Bot 构建和真实双包交付见[当前实现](../../docs/16-双版本构建确认与派发.md)；本页“未部署”等描述仅对应当时模块，不代表当前 Bot 状态。

在同一张飞书 Card 2.0 卡片中填写分支链接及 APP_VERSION。默认带出本人上次确认的组件配置；首次使用展示三条版本为空的模板。用户可以在多行框直接修改版本、追加或删除可选组件，没有预填按钮。

本模块实现输入、格式检查、确认保存与个人历史，并提供本人已确认输入的导出接口，供独立双包计划使用。没有部署 Bot、接入现有台架网关或队列，也没有 Jenkins、GitLab、Conan、上传或发消息客户端。确认配置不等于授权或触发发包。

## 卡片行为

1. 打开卡片时读取 `(tenant, user)` 对应的最近确认配置；无历史则用空版本模板。分支由本次用户提供，不能静默沿用上次分支。
2. 用户直接编辑和追加。点击“检查参数”，服务端读取完整最新表单；格式错误在原卡提示，保留本次输入。
3. 检查通过后显示“确认并保存配置”。点击后才替换本人的默认组件配置；检查、未确认编辑和错误输入不更新历史。
4. 用户在检查后又改内容，必须重新检查；旧卡回调、并发确认和重复投递不会静默覆盖较新的配置。新卡仍保留最后确认的配置。

首次 APP_VERSION 模板为：

```text
conan_shared/@livc/release-schu
uroad_m100/@livc/stable
dag_compiler/@livc/stable
```

三条固定组件必须各出现一次、填入精确版本并保持所示用户/通道；空版本、星号、范围版本、重复名称不能确认。附加组件按 `组件/版本@用户/通道` 每行填写。按飞书输入组件限制，每个框最多1000字符，超长明确拒绝，不截断内容。

历史版本只是编辑起点，换分支后仍待核验。模块只检查格式；组件是否存在、用户权限、真实源码提交、SDK/雷达兼容性和 OFF/ON 专用引用必须由后续执行模块重新查证。不得把这里的稳定通道输入直接当作已准备好的两版组件，不得复用历史 pair 通道充当新任务结果。

## 宿主调用接口

依赖 Python 3.9+ 标准库。将本目录加入宿主导入路径后：

```python
from jenkins_input_card import Identity, InputCards

# 配置库位于宿主私有业务目录，不能放在本 Git 仓库。
cards = InputCards('/private/jenkins-input/preferences.sqlite3')

# 身份由已认证的宿主会话取得，不能读取用户表单中的身份字段。
actor = Identity(tenant=trusted_tenant_key, user=trusted_operator_open_id)
view = cards.open(actor)  # view['card'] 为完整 Card JSON 2.0

# 宿主验证回调来源及操作者，再传入 action.name 和 action.form_value。
# 字符串化的 form_value 需要宿主先解析为对象。
updated = cards.submit(actor, action_name, form_values)

# 在当前卡片已确认后导出；草稿、他人卡片和已被更新的旧确认会被拒绝。
snapshot = cards.export_confirmed(actor, updated['session_id'])
```

`open()` 和 `submit()` 返回完整卡片、状态、错误以及 `remote_validation: NOT_CHECKED`、`build_dispatch: NOT_IMPLEMENTED`。配置确认状态为 `CONFIRMED`，不是构建完成或可发包状态。卡片有效期24小时，过期重开；已确认历史不随卡片过期删除。

未来接入者负责：校验飞书回调签名或使用受信 SDK、租户与操作者映射、私有卡片投递和更新、私有配置库访问权限及备份。Card 2.0 为共享卡，个人配置卡应私聊发送；不得把个人历史默认发到群中。本模块只提供回调归约器，不提供未经认证的 HTTP 回调入口，不注册监听器，不更改生产 Bot。

下游如需发包，应从本人已确认配置构造新的构建计划，继续核对当前源码、组件、Jenkins 参数与发包授权；不要把卡片字段直接拼接成 shell 命令，也不要把配置确认当作点 Build 的许可。

独立双包衔接接口已提供：原Skill的 `scripts/plan_from_card.py` 可调用 `plan_from_card(cards, actor, session_id, resolved_context)`，生成OFF/ON参数、绑定本人确认摘要并导出模式清单。所需源码/组件证据由助手读取实际GitLab和制品库后提供，不从版本名猜测。完整流程见[双包交付与标识](../../docs/13-Jenkins双包交付.md)。这属于代码层的衔接，尚未部署真实飞书回调。

## 离线验证和预览

从仓库根目录执行：

```sh
python -B -m unittest discover -s modules/jenkins-input-card/tests -p "test_*.py"
python -B tools/check_repository.py
python -B modules/jenkins-input-card/demo.py --output ../jenkins-card-demo
```

示例输出目录必须位于仓库之外且为空。演示只创建虚构租户/用户的配置库、6份卡片 JSON 和 `preview.html`，不访问外部平台。HTML 是静态布局示意，不等同于飞书客户端渲染或线上回调验收。私有配置库及生成的预览不入库。

飞书结构参考：[输入框](https://open.feishu.cn/document/feishu-cards/card-components/interactive-components/input)、[表单容器](https://open.feishu.cn/document/feishu-cards/card-components/containers/form-container)。按钮使用表单提交，最新值从 `action.form_value` 获取；没有旧版 action 容器或单独预填动作。
