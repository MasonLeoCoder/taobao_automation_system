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


def execute_collect(settings, keyword, *, limit=None, wait=True):
    from ecommerce.modules.collection.service import collect_keyword
    from ecommerce.platforms.taobao.cards import validate_limit

    keyword = validate_keyword(keyword)
    validate_limit(limit)
    browser_settings = settings.for_platform("taobao")
    return run_task(settings, "collection.taobao.collect", lambda: collect_keyword(
        browser_settings, keyword, limit=limit, wait=wait,
        on_attention=None if settings.headless else prompt_taobao_attention))


def execute_detail(settings, value, *, wait=True, video=False, main_images=False, skus=False):
    from ecommerce.modules.collection.detail_service import open_detail_input
    from ecommerce.platforms.taobao.detail import resolve_detail_input

    resolve_detail_input(value)
    browser_settings = settings.for_platform("taobao")
    return run_task(settings, "collection.taobao.detail.open", lambda: open_detail_input(
        browser_settings, value, wait=wait, video=video, main_images=main_images, skus=skus,
        on_attention=None if settings.headless else prompt_taobao_attention))
