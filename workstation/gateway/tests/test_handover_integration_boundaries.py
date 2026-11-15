"""Offline checks for the newly collected production integration sources."""
import io
import json
import unittest

from bench_gateway.core import GatewayError
from bench_gateway.service import identity
from bench_gateway import pcm_test1_ingress as pcm


class HandoverIntegrationTests(unittest.TestCase):
    def test_virtual_principal_cannot_enter_as_a_feishu_user(self):
        with self.assertRaises(GatewayError):
            identity('feishu:oc_fixture:pcm_test1_'+'a'*32)
        self.assertEqual('ou_fixture',identity('feishu:oc_fixture:ou_fixture'))

    def test_legacy_user_keeps_its_cloud_configuration(self):
        cfg={'cloud_evaluation_profiles':[]}
        self.assertIs(cfg,pcm.cloud_scope(cfg,{'owner':'ou_fixture'}))

    def test_dedicated_principal_without_its_cloud_binding_fails_closed(self):
        with self.assertRaises(GatewayError):
            pcm.cloud_scope({'cloud_evaluation_profiles':[]},{'owner':'pcm_test1_'+'a'*32,'payload':{}})

    def test_unsigned_ingress_cannot_reach_material_registration(self):
        raw=json.dumps({'payload':{},'hmac_sha256':'0'*64}).encode()+b'\n'
        with self.assertRaises(GatewayError):
            pcm.request(io.BytesIO(raw),b'fictional-offline-fixture-key-123456','cli_fixture')


if __name__=='__main__':unittest.main()
