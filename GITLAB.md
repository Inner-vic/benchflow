# GitLab维护

交接项目：`https://github.com/Inner-vic/benchflow`，private。项目ID57075；旧work-handover-developer地址已迁移至当前路径。默认分支main，后续修改建codex/分支并提交MR。首次导入为审阅后的源码快照，不迁入旧Git历史；来源见[收录说明](docs/10-仓库收录与来源.md)。

```bash
git clone https://github.com/Inner-vic/benchflow.git
cd bench-evaluation
python tools/check_repository.py
python -m unittest discover -s tools/tests -q
```

这些检查不登录外部平台或启动服务。本库没有自动部署流水线。

GitLab保存三端源码、模板、文档和测试。凭据、业务状态、大数据和现场备份留Linux。CC Connect补丁二进制、原生适配器和评测器不在Git；受控来源和摘要见设备手册，完整发布流程仍待建设。

## 修改顺序

1. 克隆后先读START_HERE、当前基线及问题清单；源码目录不直接当运行目录使用。
2. 建codex/分支，先比较生产摘要及他人改动，明确拟变更文件。
3. 运行python tools/check_repository.py；用既有Linux环境做相关定向单测。记录初始失败及复查，不能只贴测试总数。
4. 推送分支、创建合并请求；说明问题、改变的行为、测试、部署范围和剩余风险。
5. 源码审查/合并与生产部署分开。生产备份、排空和回退步骤见docs/03-运维与验收.md。

README和docs按现网更新；详细手册保留设备差异。外部修改经审查后纳入Git，不反向用旧仓库覆盖生产。原ZIP校验器不适用于此仓库，当前使用仓库静态检查。尚无配置完成并验收的GitLab Runner自动部署流水线。
