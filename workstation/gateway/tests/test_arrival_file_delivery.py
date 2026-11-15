import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from bench_gateway.feishu_files import FeishuFiles
from bench_gateway.core import GatewayError

class ArrivalFileDeliveryTests(unittest.TestCase):
    def test_small_report_uses_bot_multipart_and_preserves_filename(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'arrival-accuracy.xlsx';p.write_bytes(b'xlsx fixture')
            api=FeishuFiles({});api.auth=Mock(return_value='test-token')
            response=Mock();response.__enter__=Mock(return_value=io.BytesIO(b'{"code":0,"data":{"file_key":"file_report"}}'));response.__exit__=Mock(return_value=False)
            with patch('bench_gateway.feishu_files.urlopen',return_value=response) as request:
                self.assertEqual('file_report',api.upload_report(p,timeout=3))
            actual=request.call_args.args[0]
            self.assertEqual('https://open.feishu.cn/open-apis/im/v1/files',actual.full_url)
            self.assertIn(b'filename="arrival-accuracy.xlsx"',actual.data);self.assertIn(b'xlsx fixture',actual.data)
            self.assertIn('multipart/form-data',actual.get_header('Content-type'))
    def test_reply_binds_original_message_and_deduplication_key(self):
        api=FeishuFiles({});api._json=Mock(return_value={'data':{'message_id':'om_report'}})
        self.assertEqual('om_report',api.reply_report('om_original','file_report','dedup',timeout=2))
        self.assertEqual('/im/v1/messages/om_original/reply',api._json.call_args.args[0])
        body=json.loads(api._json.call_args.kwargs['body']);self.assertEqual('dedup',body['uuid'])
        self.assertEqual({'file_key':'file_report'},json.loads(body['content']))
    def test_nonreport_file_is_never_uploaded(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'dataset.bin';p.write_bytes(b'not a report');api=FeishuFiles({});api.auth=Mock()
            with self.assertRaises(GatewayError):api.upload_report(p)
            api.auth.assert_not_called()

if __name__=='__main__':unittest.main()
