# 云端恢复参考

`profile.json`记录当前已部署的旧策略；`profile.benchflow-pcm-test1.candidate.json`为已核验接收包对应的候选，尚未激活。不能直接覆盖原profile或修改已有Job的冻结策略。

接收程序及profile来自Linux业务发布，小体积evaluator来自固定原生环境。11月5日官方查询原bench-eval-offline返回404；用户已改为benchflow-pcm-test1，启动请求待平台审批。新实例原有协议/公钥与当前网关不一致，接收端尚未重绑，不将旧READY文件当新鲜就绪证据。当前进展见[紧急修复与云端接入](../docs/08-紧急修复与云端接入-20261105.md)。

迁移Windows时不重装本目录。若另行重建云端，先阅读运维Skill参考05、06：核对三个挂载、runtime/Jupyter路径、固定程序摘要及任务GT关联；从原部署安全取得签名**公钥**public.pem并核对profile所列SHA256。本包不导出签名私钥，也未携带public.pem；不能随意生成新密钥对而继续使用旧签名。

evaluator中的上游默认配置不是本任务GT，运行时由受控接收程序生成任务配置。不得直接执行上游run.sh以绕过签名与场景绑定。新实例没有验证前，不开启后台自动提交或替换现有profile。
