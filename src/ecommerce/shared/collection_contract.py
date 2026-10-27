"""采集模块和记录文件的共用契约，不依赖业务或存储实现。"""
MODULE_FILES = {'basic_info':'basic_info.json','video':'video.json','main_images':'main_images.json','skus':'sku_details.json',
    'parameters':'parameters.json','detail_images':'detail_images.json','review_summary':'review_summary.json',
    'reviews':'reviews.json','questions':'questions.json'}
MODULES = tuple(MODULE_FILES)
SECTIONS = {'basic_info':'product_info', 'skus':'sku_details', **{m:m for m in MODULES if m not in {'basic_info','skus'}}}
