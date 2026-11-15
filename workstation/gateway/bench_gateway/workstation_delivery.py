"""Upload workstation reports directly to Feishu; no PC file staging."""
import hashlib,json
from .feishu_files import FeishuFiles
from .core import GatewayError

def sender(cfg,store,timeout):
    client=FeishuFiles(cfg)
    def deliver(row):
        from .service import identity
        user=identity(row['session']);store.require_user(user);chat=row['session'].split(':')[1]
        if not chat.startswith('oc_'):raise GatewayError('交付私聊身份无效。')
        key=hashlib.sha256((str(row.get('job'))+'\0'+str(row.get('id'))+'\0'+str(row.get('version',1))).encode()).hexdigest()[:32]
        if row.get('text') and not row.get('text_sent'):
            client._json('/im/v1/messages?receive_id_type=chat_id',body=json.dumps({'receive_id':chat,
                'msg_type':'text','content':json.dumps({'text':row['text']},ensure_ascii=False),'uuid':'t-'+key}).encode(),timeout=timeout(25))
            if row.get('_outbox_table')=='outbox':
                with store.connect() as c:
                    if not c.execute('UPDATE outbox SET text_sent=1 WHERE id=? AND version=?',(row['id'],row['version'])).rowcount:return False
        if row.get('attachment'):
            file_key=client.upload_report(row['attachment'],timeout=timeout(60))
            with store.connect() as c:card=c.execute('SELECT message_id FROM task_cards WHERE job=?',(row.get('job'),)).fetchone()
            if card and card['message_id']:client.reply_report(card['message_id'],file_key,'f-'+key,timeout=timeout(25))
            else:client._json('/im/v1/messages?receive_id_type=chat_id',body=json.dumps({'receive_id':chat,
                'msg_type':'file','content':json.dumps({'file_key':file_key}),'uuid':'f-'+key}).encode(),timeout=timeout(25))
        return True
    return deliver
