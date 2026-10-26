"""将淘宝关键词搜索提交给现有串行任务执行器。"""
from ecommerce.cli.interaction import prompt_taobao_attention
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.platforms.taobao.search import validate_keyword


def execute(settings, keyword, *, wait=True):
    from ecommerce.modules.collection.service import search_keyword
    keyword = validate_keyword(keyword)
    browser_settings = settings.for_platform("taobao")
    return run_task(settings, "collection.taobao.search", lambda: search_keyword(
        browser_settings, keyword, wait=wait, on_attention=None if settings.headless else prompt_taobao_attention))


def execute_collect(settings, keyword, *, limit=None, pages=1, wait=True, with_details=False):
    """验证输入后提交逐页采集；参数：配置、词、每页上限、页数、保留窗口；返回：任务结果。"""
    from ecommerce.modules.collection.service import collect_keyword
    from ecommerce.platforms.taobao.cards import validate_limit
    from ecommerce.platforms.taobao.pagination import validate_pages

    keyword = validate_keyword(keyword)
    validate_limit(limit)
    validate_pages(pages)
    browser_settings = settings.for_platform("taobao")
    return run_task(settings, "collection.taobao.collect", lambda: collect_keyword(
        browser_settings, keyword, limit=limit, pages=pages, wait=wait, with_details=with_details,
        on_attention=None if settings.headless else prompt_taobao_attention))


def execute_detail(settings, value, *, wait=True, basic_info=False, video=False, main_images=False, skus=False, parameters=False, detail_images=False, review_summary=False, reviews=False, all_reviews=False, questions=False):
    from ecommerce.modules.collection.detail_service import open_detail_input
    from ecommerce.platforms.taobao.detail import resolve_detail_input

    if all_reviews and not (reviews or questions):
        raise ValueError('--all 必须与 --reviews 或 --questions 一起使用')
    resolve_detail_input(value)
    browser_settings = settings.for_platform("taobao")
    return run_task(settings, "collection.taobao.detail.open", lambda: open_detail_input(
        browser_settings, value, wait=wait, basic_info=basic_info, video=video, main_images=main_images, skus=skus, parameters=parameters, detail_images=detail_images, review_summary=review_summary, reviews=reviews, all_reviews=all_reviews, questions=questions,
        on_attention=None if settings.headless else prompt_taobao_attention))
