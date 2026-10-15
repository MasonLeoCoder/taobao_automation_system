"""商品命令适配及终端交互。"""
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.shared.config import Settings


from ecommerce.cli.interaction import prompt_manual_verification, prompt_submission_review


def execute(settings: Settings, start: bool, wait: bool, keyword: str | None = None, category: str | None = None, brand: str | None = None, main_images_dir: str | None = None, portrait_images_dir: str | None = None, detail_images_dir: str | None = None, video_file: str | None = None, product_info_file: str | None = None, submit: bool = False, sku_images_dir: str | None = None):
    """执行商品任务；参数：配置、导航选项、素材、Excel、提交开关、SKU图片目录；返回：任务结果。"""
    from ecommerce.modules.product.service import open_publish
    on_ready = (lambda: input("页面已就绪，按回车关闭浏览器：")) if wait else None
    on_attention = None if settings.headless else prompt_manual_verification
    on_submit_attention = None if settings.headless else prompt_submission_review
    return run_task(settings, "product.submit" if submit else "product.open", lambda: open_publish(settings, start, on_ready, keyword, category, brand, main_images_dir, portrait_images_dir, on_attention=on_attention, detail_images_dir=detail_images_dir, video_file=video_file, product_info_file=product_info_file, submit=submit, on_submit_attention=on_submit_attention, sku_images_dir=sku_images_dir))
