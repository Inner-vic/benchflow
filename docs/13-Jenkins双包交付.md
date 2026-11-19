# Jenkins双包交付与区分

> 本页为早期设计与兼容接口记录，正文中的输入字段、版本方案及使用命令不作为当前操作指引。当前入口见[Jenkins 发包](17-Jenkins发包.md)，原部署事实见[历史验收](16-双版本构建确认与派发.md)。

Jenkins发普通版OFF、评测版ON是独立功能，本轮不修改台架评测入口、M100替换策略、回放或评测流程。

## 使用过程

用户在独立卡片填写分支链接，修改或追加APP_VERSION，确认配置。助手读取本次源码、组件配方、成功基线与Jenkins参数，固定同一源码提交和公共依赖，准备两份不同M100引用：OFF固定WITH_EVAL_DUMP=OFF，ON固定为ON。远端组件核验完成后生成两套原参数，展示给用户审阅，按已有授权分别沿用高鹏原流程点击一次Build并跟踪。配置确认本身不授权构建。

代码已提供卡片确认到计划的衔接和结果标识；没有部署Bot或构建客户端。平台查询、组件CI发布、Jenkins浏览器操作仍由执行助手按原Skill完成，不把本地JSON写入成功说成线上构建成功。

## 两包如何区分

| 标识 | 普通包 | 评测包 |
|---|---|---|
| 展示名称 | 普通版 OFF | 评测版 ON |
| M100编译开关 | WITH_EVAL_DUMP=OFF | WITH_EVAL_DUMP=ON |
| M100组件引用 | 本次独立OFF引用 | 本次独立ON引用 |
| Jenkins构建 | 本次真实OFF构建编号 | 本次真实ON构建编号，两者不可相同 |
| 建议下载目录 | OFF/ | ON/ |

每次双包有唯一pair ID。`package-index.json`和`package-index.md`按模式列出pair ID、源码提交、M100引用、实际构建编号、APP/Lockfile链接及SHA-256。原包文件名保持原样，不在包内加标签、不重新压缩。原机器人仍分别发两张正式卡片，其标题和包名未必带OFF/ON；使用卡片中的构建编号与清单对应，不能只按相似文件名区分。

在构建前，清单只有模式与计划引用，产物链接为空。某版失败只交付实际已有结果，不能补造链接或把另一版标成成功。双包比较未通过时保留COMPARISON_FAILED；“两次构建成功”与“只有开关不同”分别判断，实际台架效果不在本功能验收范围内。

## 维护接口

宿主先验证操作者身份和本人确认卡片。将原Skill的scripts目录加入导入路径后：

```python
from plan_from_card import plan_from_card, write_plan

# cards/actor/session_id 来自独立卡片模块的受信宿主。
# resolved_context 由助手读取平台实际证据后生成。
state = plan_from_card(cards, actor, session_id, resolved_context)
write_plan(state, private_pair_directory)
```

原[上下文结构](../skills/pcm-dual-package/references/pair-state.md)之外，需要input_component：ref为用户填写的uroad_m100完整引用，source_commit为读取该配方后确认的源码提交，必须与本次分支固定提交相同。不能仅根据版本号判断源码对应关系。卡片公共组件覆盖基线的同名APP默认值，与LOCKED_LIBS冲突时停止，交由助手核对。

计划生成后的pair.json包含本人确认摘要，参与计划校验；修改确认来源或参数后原审阅SHA失效。草稿、他人卡片、过期或已被较新确认取代的卡片不能导出为当前输入。未发布配方继续阻止提交。

可信宿主导出确认输入后，也可在私有工作目录运行：

```sh
python skills/pcm-dual-package/scripts/plan_from_card.py --confirmed-input confirmed.json --context context.json --output-dir new-pair
```

本命令只输出pair.json、OFF.parameters.json、ON.parameters.json和初始模式清单，不会访问平台。不能把未认证的HTTP回调直接当作confirmed.json。

实际Build的提交意图、编号绑定和结果记录继续使用原pcm_existing_job.py；两个模式分别记录，不用lastBuild猜编号。记录实际结果后导出交付清单：

```sh
python skills/pcm-dual-package/scripts/pcm_existing_job.py export-delivery --state new-pair/pair.json --output-dir new-delivery
```

输出目录必须是新目录，不覆盖旧清单。这个导出命令不下载、上传或发送消息，不改写状态。下载原APP和Lockfile后按清单核验摘要，分别保存在OFF/ON目录。

## 离线验证

新增的卡片到双包及交付测试使用虚构提交、替身远端证据和本地文件，覆盖参数继承、本人确认、源码不匹配、缺配方阻断、结果串版拒绝及清单导出。未实际触发Jenkins或发布组件。运行：

```sh
python -B -m unittest discover -s skills/pcm-dual-package/tests -p "test_*.py"
python -B -m unittest discover -s modules/jenkins-input-card/tests -p "test_*.py"
```
