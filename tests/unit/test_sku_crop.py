"""只接收本次单图原素材匹配且成功唯一的裁剪回执。"""
import json
import re
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import patch
from ecommerce.platforms.qianniu.product_sku_crop import _CROP_PATH, _crop_body, _crop_input, _crop_output, _CropEvidence
from ecommerce.platforms.qianniu import product_sku_media as media

ORIGINAL='https://img.alicdn.com/imgextra/i3/shop/original.png'
RESULT='https://img.alicdn.com/imgextra/i2/shop/result.png'
API='https://h5api.m.taobao.com'+_CROP_PATH


def request_body(source=ORIGINAL, **values):
    return urlencode({'data':json.dumps({'paramStrList':[{'imgUrl':source}],**values})})


def response_body(source=RESULT):
    return {'ret':['SUCCESS::调用成功'],'data':{'result':{'taskResults':[{'jsPictureDOS':[{'fullUrl':source}]}]}}}


class SkuCropTests(unittest.TestCase):
    def check_wait_deadline(self, feedback):
        """模拟较慢的检查与真正人工等待，核对超时不会因普通检测无限延长。"""
        clock=SimpleNamespace(now=0.0,calls=0)
        def checkpoint(step):
            clock.calls+=1
            if clock.calls==1:return 0
            clock.now+=10 if feedback and clock.calls==2 else .4
            return int(feedback)
        def wait(milliseconds):clock.now+=milliseconds/1000
        evidence=SimpleNamespace(match=lambda source:None,problem=lambda:None)
        with patch.object(media,'WAIT_MS',1000),patch.object(media,'checkpoint',side_effect=checkpoint),\
             patch.object(media.time,'monotonic',side_effect=lambda:clock.now),patch.object(media,'_source',return_value=None):
            with self.assertRaises(media._ImageError):
                media._selection_result(SimpleNamespace(wait_for_timeout=wait),object(),media._Asset('example',ORIGINAL),
                                        evidence,crop_enabled=True,crop_required=True)
        return clock

    def test_normal_check_duration_does_not_extend_selection_deadline(self):
        clock=self.check_wait_deadline(False)
        self.assertLess(clock.now,2)
        self.assertLessEqual(clock.calls,4)

    def test_manual_feedback_duration_is_excluded_from_selection_deadline(self):
        clock=self.check_wait_deadline(True)
        self.assertGreaterEqual(clock.now,10)
        self.assertLess(clock.now,12)

    def test_json_and_jsonp_are_data_only(self):
        body=response_body()
        text=json.dumps(body)
        self.assertEqual(_crop_body(text),body)
        self.assertEqual(_crop_body('mtopjsonp17('+text+');'),body)
        self.assertIsNone(_crop_body('unknown code'))
        self.assertIsNone(_crop_body('callback('+text+');extra()'))

    def test_only_observed_api_and_one_input_are_recognized(self):
        self.assertEqual(_crop_input(API,request_body()),ORIGINAL)
        self.assertEqual(_crop_input(API+'?'+request_body(),None),ORIGINAL)
        self.assertIsNone(_crop_input(API.replace('h5api.m.taobao.com','wrong.test'),request_body()))
        self.assertIsNone(_crop_input(API,request_body(paramStrList=[{'imgUrl':ORIGINAL},{'imgUrl':RESULT}])))
        self.assertIsNone(_crop_input(API,'data={bad'))
        self.assertIsNone(_crop_input(API,request_body()+'&'+request_body()))

    def test_json_encoded_parameter_list_is_supported(self):
        body=request_body(paramStrList=json.dumps([{'imgUrl':ORIGINAL}]))
        self.assertEqual(_crop_input(API,body),ORIGINAL)
        # 实际千牛将数组及数组内的参数对象分别编码为 JSON 文本。
        body=request_body(paramStrList=json.dumps([json.dumps({'imgUrl':ORIGINAL})]))
        self.assertEqual(_crop_input(API,body),ORIGINAL)

    def test_output_requires_success_single_picture_and_known_https_cdn(self):
        self.assertEqual(_crop_output(response_body()),RESULT)
        for source in ('http://img.alicdn.com/a.png','https://img.alicdn.com.wrong.test/a.png','blob:unknown'):
            self.assertIsNone(_crop_output(response_body(source)))
        failed=response_body();failed['ret']=['FAIL::裁剪失败']
        self.assertIsNone(_crop_output(failed))
        failed=response_body();failed['ret'].append('FAIL::错误')
        self.assertIsNone(_crop_output(failed))
        multiple=response_body();multiple['data']['result']['taskResults']*=2
        self.assertIsNone(_crop_output(multiple))
        multiple=response_body();multiple['data']['result']['taskResults'][0]['jsPictureDOS']*=2
        self.assertIsNone(_crop_output(multiple))
        for invalid in (None,{},[],{'ret':['SUCCESS::ok'],'data':None}):
            self.assertIsNone(_crop_output(invalid))

    def test_stale_wrong_original_failed_responses_and_cleanup(self):
        listeners={}
        events=SimpleNamespace(on=lambda event,callback:listeners.setdefault(event,callback),
                               remove_listener=lambda event,callback:listeners.pop(event))
        page=SimpleNamespace(context=events)
        class Request:
            def __init__(self,source=ORIGINAL):
                self.url=API;self.post_data=request_body(source)
        class Response:
            status=200
            def __init__(self,request):self.request=request
            def text(self):return json.dumps(response_body())
        with _CropEvidence(page,ORIGINAL,lambda value:re.compile(re.escape(value))) as evidence:
            listeners['response'](Response(Request()))
            self.assertFalse(evidence.results)
            wrong=Request(RESULT);listeners['request'](wrong);listeners['response'](Response(wrong))
            self.assertFalse(evidence.results)
            current=Request();listeners['request'](current)
            failed=Response(current);failed.status=500
            listeners['response'](failed)
            self.assertFalse(evidence.results)
            self.assertIn('HTTP 500',evidence.problem())
            listeners['response'](Response(current))
            self.assertEqual(evidence.match(RESULT),RESULT)
            self.assertIsNone(evidence.problem())
            self.assertIsNone(evidence.match(ORIGINAL))
        self.assertEqual(listeners,{})

    def test_crop_on_upload_completion_can_be_bound_after_original_is_known(self):
        listeners={}
        events=SimpleNamespace(on=lambda event,callback:listeners.setdefault(event,callback),
                               remove_listener=lambda event,callback:listeners.pop(event))
        request=type('Request',(),{'url':API,'post_data':request_body()})()
        response=SimpleNamespace(request=request,status=200,text=lambda:json.dumps(response_body()))
        with _CropEvidence(SimpleNamespace(context=events),None,lambda value:re.compile(re.escape(value))) as evidence:
            listeners['request'](request);listeners['response'](response)
            self.assertIsNone(evidence.match(RESULT))
            evidence.bind(RESULT)
            self.assertIsNone(evidence.match(RESULT))
            evidence.bind(ORIGINAL)
            self.assertEqual(evidence.match(RESULT),RESULT)
