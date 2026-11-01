"""主图导出的兼容入口，共用已采集图片导出能力。"""
from .collected_images import export_images


def export_main_images(settings,product_id,batch_id,output_format,*,media_id=None):
    """导出本批主图；参数：配置、商品、批次、格式及媒体编号；返回：临时附件。"""
    return export_images(settings,product_id,batch_id,output_format,media_id=media_id,module='main_images')
