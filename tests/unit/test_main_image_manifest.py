"""主图裁切资源归一及安全来源边界，不访问真实平台。"""
import copy
import unittest

from ecommerce.platforms.taobao.detail_images import HEAD_IMAGES_SOURCE,ITEM_IMAGES_SOURCE,gallery_diagnostics,main_image_manifest


def gallery():
    """准备原图和裁切商品图；参数：无；返回：脱敏快照。"""
    urls=[f'https://img.alicdn.com/imgextra/i2/123/O1CN0image{i}_!!123.jpg' for i in range(1,6)]
    return {'ready':True,'data_item_id':'456','ssr_item_id':'456','video_cover_skipped':1,'parameters_skipped':1,
        'photos':[{'slot_index':i+2,'thumbnail_url':url+'_q50.jpg_.webp'} for i,url in enumerate(urls)],
        'item_images':[url.replace('_!!','~crop,48,0,600,800~_!!') for url in urls],'head_images':urls}


class MainImageManifestTests(unittest.TestCase):
    def test_crop_and_original_match_but_original_actual_source_is_selected(self):
        value=gallery();before=copy.deepcopy(value);manifest=main_image_manifest(value,'456')
        self.assertEqual(manifest['source_path'],HEAD_IMAGES_SOURCE)
        self.assertEqual([row['image_url'] for row in manifest['images']],value['head_images'])
        self.assertEqual([row['slot_index'] for row in manifest['images']],[2,3,4,5,6])
        self.assertEqual(value,before)
        self.assertEqual(manifest['verified_source_paths'],[ITEM_IMAGES_SOURCE,HEAD_IMAGES_SOURCE])

    def test_crop_only_uses_actual_page_address_and_does_not_invent_original(self):
        value=gallery();value['head_images']=None
        result=main_image_manifest(value,'456')
        self.assertEqual(result['images'][0]['image_url'],value['item_images'][0])

    def test_cropped_thumbnail_also_prefers_page_provided_originals(self):
        value=gallery()
        for photo,image in zip(value['photos'],value['item_images']):photo['thumbnail_url']=image+'_q50.jpg_.webp'
        self.assertEqual(main_image_manifest(value,'456')['source_path'],HEAD_IMAGES_SOURCE)

    def test_order_host_directory_identity_and_count_conflicts_still_fail(self):
        mutations=[lambda v:v['item_images'].reverse(),lambda v:v['item_images'].pop(),
            lambda v:v['item_images'].__setitem__(0,v['item_images'][0].replace('img.alicdn.com','other.alicdn.com')),
            lambda v:v['item_images'].__setitem__(0,v['item_images'][0].replace('/i2/','/i3/')),
            lambda v:v['item_images'].__setitem__(0,v['item_images'][0].replace('image1','other1')),
            lambda v:v.__setitem__('data_item_id','789')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value=gallery();mutate(value)
                with self.assertRaises(ValueError):main_image_manifest(value,'456')

    def test_unknown_transform_is_not_stripped_and_duplicates_remain_positions(self):
        value=gallery();value['item_images'][0]=value['item_images'][0].replace('~crop,','~unknown,')
        with self.assertRaises(ValueError):main_image_manifest(value,'456')
        value=gallery()
        for field in ['item_images','head_images']:value[field][1]=value[field][0]
        value['photos'][1]['thumbnail_url']=value['photos'][0]['thumbnail_url']
        self.assertEqual(len(main_image_manifest(value,'456')['images']),5)

    def test_diagnostics_include_positions_and_hide_url_queries(self):
        value=gallery();value['item_images'][0]+='?token=secret#hidden'
        details=gallery_diagnostics(value,'456')
        first=details['comparisons'][0]
        self.assertEqual((first['position'],first['slot_index']),(1,2))
        self.assertTrue(first['same_origin_asset']);self.assertFalse(first['exact_asset_match'])
        self.assertNotIn('secret',str(details));self.assertNotIn('#hidden',str(details))


if __name__=='__main__':unittest.main()
