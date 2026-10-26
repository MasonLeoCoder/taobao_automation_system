"""DOM主图按完整地址去重、保持顺序与下载边界，不访问真实平台。"""
import copy
import unittest

from ecommerce.platforms.taobao.detail_images import DOM_IMAGES_SOURCE,gallery_diagnostics,main_image_manifest,_resume_manifest
from unittest.mock import patch
from types import SimpleNamespace


def gallery():
    """准备首图重复的六个DOM位置；参数：无；返回：脱敏快照。"""
    urls=[f'https://img.alicdn.com/imgextra/i2/123/O1CN0image{i}_!!123.jpg' for i in range(1,6)]
    return {'ready':True,'data_item_id':'456','ssr_item_id':'456','video_cover_skipped':1,'parameters_skipped':1,
        'photos':[{'slot_index':i+1,'thumbnail_url':url+'_q50.jpg_.webp'} for i,url in enumerate([urls[0],*urls])],
        'item_images':[url.replace('_!!','~crop,48,0,600,800~_!!') for url in urls],'head_images':urls}


class MainImageManifestTests(unittest.TestCase):
    def test_dom_images_deduplicate_in_first_seen_order_without_changing_urls(self):
        value=gallery();before=copy.deepcopy(value);manifest=main_image_manifest(value,'456')
        self.assertEqual(manifest['source_path'],DOM_IMAGES_SOURCE)
        self.assertEqual([row['image_url'] for row in manifest['images']],[p['thumbnail_url'] for p in value['photos'][1:]])
        self.assertEqual([row['slot_index'] for row in manifest['images']],[1,3,4,5,6])
        self.assertEqual([row['index'] for row in manifest['images']],[1,2,3,4,5])
        self.assertEqual((manifest['raw_image_count'],manifest['duplicate_count']),(6,1))
        self.assertEqual(manifest['video_cover_skipped'],0)
        self.assertEqual(value,before)
        self.assertEqual(manifest['verified_source_paths'],[DOM_IMAGES_SOURCE])

    def test_missing_or_conflicting_page_data_has_no_effect_on_dom(self):
        value=gallery()
        value.update(item_images=['unrelated'],head_images=False,data_item_id='other',ssr_item_id='other')
        expected=main_image_manifest(value,'456')
        for key in ('item_images','head_images','data_item_id','ssr_item_id'):value.pop(key)
        self.assertEqual(main_image_manifest(value,'456'),expected)

    def test_url_normalization_keeps_query_and_size_variants_separate(self):
        value=gallery();value['photos']=[{'slot_index':i+1,'thumbnail_url':url} for i,url in enumerate([
            '//img.alicdn.com/a.jpg','https://img.alicdn.com/a.jpg#preview',
            'https://img.alicdn.com/a.jpg?size=1','https://img.alicdn.com/a.jpg?size=2',
            'https://img.alicdn.com/a.jpg_q50.jpg_.webp','https://img.alicdn.com/other/a.jpg'])]
        result=main_image_manifest(value,'456')
        self.assertEqual(len(result['images']),5)
        self.assertEqual(result['images'][0]['image_url'],'https://img.alicdn.com/a.jpg')
        self.assertIn('?size=1',result['images'][1]['image_url'])

    def test_empty_invalid_or_foreign_image_urls_are_not_silently_skipped(self):
        for url in (None,'','https://example.com/a.jpg','https://img.alicdn.com:444/a.jpg',
                    'https://user:secret@img.alicdn.com/a.jpg','file:///a.jpg'):
            with self.subTest(url=url):
                value=gallery();value['photos'][0]['thumbnail_url']=url
                with self.assertRaises(ValueError):main_image_manifest(value,'456')
        for value in ({'ready':True,'photos':[]},{'ready':False,'photos':[]}):
            with self.assertRaises(ValueError):main_image_manifest(value,'456')

    def test_recheck_accepts_duplicate_movement_but_rejects_unique_order_changes(self):
        value=gallery();manifest=main_image_manifest(value,'456')
        value['photos'].pop(0)
        for index,photo in enumerate(value['photos'],1):photo['slot_index']=index
        result=SimpleNamespace(target=SimpleNamespace(item_id='456'),page=object())
        with patch('ecommerce.platforms.taobao.detail_images._read_manifest',return_value=main_image_manifest(value,'456')):
            self.assertIs(_resume_manifest(result,manifest),True)
        value['photos'].reverse()
        with patch('ecommerce.platforms.taobao.detail_images._read_manifest',return_value=main_image_manifest(value,'456')):
            self.assertIn('顺序',_resume_manifest(result,manifest).source)

    def test_diagnostics_include_positions_and_hide_url_queries(self):
        value=gallery();value['photos'][0]['thumbnail_url']+='?token=secret#hidden'
        details=gallery_diagnostics(value,'456')
        first=details['dom_images'][0]
        self.assertEqual(first['slot_index'],1)
        self.assertEqual(details['raw_image_count'],6)
        self.assertNotIn('secret',str(details));self.assertNotIn('#hidden',str(details))


if __name__=='__main__':unittest.main()
