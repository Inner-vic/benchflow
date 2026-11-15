"""Native local credential input; user input never enters chat or a shell command."""
import argparse
import sys

from .config import load
from .core import GatewayError
from .download_auth import credential_status,save_basic,save_team_candidate,activate_team_candidate,team_credential_status


def show_dialog(cfg,user,host,*,team=False,probe_url=None):
    import tkinter as tk
    from tkinter import ttk
    credential_status(cfg,user,host)
    if team:
        from urllib.parse import urlsplit
        if not probe_url or urlsplit(probe_url).hostname != host:
            raise GatewayError('团队下载认证需要同一仓库的已知软件包链接以进行验证')
        team_credential_status(cfg,user,host)
    title='团队 JFrog 下载认证' if team else 'JFrog 下载认证'
    root=tk.Tk();root.title(title)
    root.resizable(False,False)
    frame=ttk.Frame(root,padding=24);frame.grid()
    ttk.Label(frame,text=title,font=('Microsoft YaHei UI',15)).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,12))
    ttk.Label(frame,text='目标仓库：'+host).grid(row=1,column=0,columnspan=2,sticky='w')
    ttk.Label(frame,text='请填写专用下载账号；验证通过后供机器人可用范围内用户下载。' if team else '仅用于你本人的飞书下载任务。').grid(row=2,column=0,columnspan=2,sticky='w',pady=(0,14))
    ttk.Label(frame,text='用户名').grid(row=3,column=0,sticky='w',padx=(0,12))
    username=ttk.Entry(frame,width=44);username.grid(row=3,column=1,pady=5)
    ttk.Label(frame,text='密码').grid(row=4,column=0,sticky='w')
    password=ttk.Entry(frame,width=44,show='*');password.grid(row=4,column=1,pady=5)
    ttk.Label(frame,text='Windows 加密保存在本机；工作站下载时临时使用。\n请勿把账号密码发送到聊天。').grid(row=5,column=0,columnspan=2,sticky='w',pady=(12,8))
    message=tk.StringVar(value='保存后将另行验证仓库访问。')
    ttk.Label(frame,textvariable=message,wraplength=440).grid(row=6,column=0,columnspan=2,sticky='w',pady=6)
    completed=[False]
    def submit(event=None):
        try:
            if team:
                from .asset_fetch import probe_download
                message.set('正在验证工作站仓库访问，验证通过后才启用团队认证。');root.update_idletasks()
                digest=save_team_candidate(cfg,user,host,username.get().strip(),password.get())
                verified=probe_download({**cfg,'_download_auth_team_candidate_host':host},user,probe_url)
                activate_team_candidate(cfg,user,host,digest,verified)
            else:
                save_basic(cfg,user,host,username.get().strip(),password.get())
        except GatewayError as exc:
            if str(exc)=='下载账号或密码格式无效':
                message.set('请填写有效用户名和密码；用户名不能含冒号或换行。')
            else:message.set('认证未启用：'+str(exc) if team else '本机安全存储失败，请返回 Codex 告知此提示。')
            return
        except Exception:
            message.set('本机配置保存失败，请返回 Codex 告知此提示。');return
        username.delete(0,'end');password.delete(0,'end')
        completed[0]=True
        print('TEAM_CREDENTIALS_VERIFIED_AND_ACTIVATED' if team else 'CREDENTIALS_SAVED_LOCALLY; DOWNLOAD_NOT_VERIFIED',flush=True)
        root.destroy()
    ttk.Button(frame,text='加密保存',command=submit).grid(row=7,column=1,sticky='e',pady=(10,0))
    root.bind('<Return>',submit)
    root.update_idletasks()
    root.geometry('+%d+%d'%((root.winfo_screenwidth()-root.winfo_width())//2,
                           (root.winfo_screenheight()-root.winfo_height())//2))
    username.focus_set();root.lift();root.attributes('-topmost',True)
    root.after(1200,lambda:root.attributes('-topmost',False))
    print('LOCAL_CREDENTIAL_DIALOG_OPENED',flush=True)
    root.mainloop()
    return 0 if completed[0] else 1


def main():
    parser=argparse.ArgumentParser(description='Enter JFrog credentials in a native local dialog')
    parser.add_argument('--root',required=True);parser.add_argument('--user',required=True);parser.add_argument('--host',required=True)
    parser.add_argument('--team',action='store_true');parser.add_argument('--probe-url')
    args=parser.parse_args()
    try:return show_dialog(load(args.root),args.user,args.host,team=args.team,probe_url=args.probe_url)
    except Exception:
        print('LOCAL_CREDENTIAL_DIALOG_FAILED; NO_CREDENTIALS_PRINTED',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
