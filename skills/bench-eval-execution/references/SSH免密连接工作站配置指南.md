# SSH 接入：个人身份与工作站执行分离

公开示例中的地址属于文档专用网段，不能用于实际接入。工作站地址、账号和主机指纹必须由本环境负责人提供。

个人电脑保存私钥，工作站只登记公钥。使用独立 SSH 别名明确目标主机、用户和密钥，不覆盖已有身份。

```sshconfig
Host bench-workstation-example
    HostName <approved-workstation-host>
    User <approved-user>
    IdentityFile <local-private-key-path>
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

首次连接前通过可信渠道核对主机指纹；指纹变化时暂停核查。密钥口令与 ssh-agent 的使用遵循所在环境的认证要求。

`ssh -G <alias>` 可检查展开后的配置；实际握手成功后再检查 Runner 协议、队列及设备可达性。交互密码登录成功不表示非交互脚本已经就绪。

共享账号下每位提交者仍应保有个人身份。凭据不得进入 Job、Profile、日志或产品收据。
