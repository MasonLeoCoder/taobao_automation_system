"""将淘宝关键词搜索提交给现有串行任务执行器。"""
from ecommerce.cli.interaction import prompt_taobao_attention
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.platforms.taobao.search import validate_keyword


def execute(settings, keyword, *, wait=True):
    from ecommerce.modules.collection.service import search_keyword
    keyword = validate_keyword(keyword)
    browser_settings = settings.for_platform("taobao")
    return run_task(browser_settings, "collection.taobao.search", lambda: search_keyword(
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
    from ecommerce.modules.collection.requests import execute as request_execute
    return request_execute(browser_settings, "collection.taobao.collect", lambda: collect_keyword(
        browser_settings, keyword, limit=limit, pages=pages, wait=wait, with_details=with_details,
        on_attention=None if settings.headless else prompt_taobao_attention),runner=run_task,
        options={'mode':'keyword','keyword':keyword,'pages':pages,'limit':limit,'with_details':with_details})


def execute_detail(settings, value, *, wait=True, basic_info=False, video=False, main_images=False, skus=False, parameters=False, detail_images=False, review_summary=False, reviews=False, all_reviews=False, questions=False):
    from ecommerce.modules.collection.detail_service import open_detail_input
    from ecommerce.platforms.taobao.detail import resolve_detail_input

    if all_reviews and not (reviews or questions):
        raise ValueError('--all 必须与 --reviews 或 --questions 一起使用')
    resolve_detail_input(value)
    browser_settings = settings.for_platform("taobao")
    action=lambda: open_detail_input(
        browser_settings, value, wait=wait, basic_info=basic_info, video=video, main_images=main_images, skus=skus, parameters=parameters, detail_images=detail_images, review_summary=review_summary, reviews=reviews,
        all_reviews=bool(all_reviews and reviews),all_questions=bool(all_reviews and questions),questions=questions,
        on_attention=None if settings.headless else prompt_taobao_attention)
    from ecommerce.shared.collection_contract import MODULES
    flags=dict(basic_info=basic_info,video=video,main_images=main_images,skus=skus,parameters=parameters,
        detail_images=detail_images,review_summary=review_summary,reviews=reviews,questions=questions)
    if not any(flags.values()):
        return run_task(browser_settings,'collection.taobao.detail.open',action)
    from ecommerce.modules.collection.requests import execute as request_execute
    return request_execute(browser_settings,'collection.taobao.detail.open',action,runner=run_task,
        options={'mode':'detail','input':resolve_detail_input(value).canonical_url,'modules':[m for m in MODULES if flags[m]],
                 'all_reviews':bool(all_reviews and reviews),'all_questions':bool(all_reviews and questions)})


def execute_resume(settings,task_id,*,preview=False,wait=True):
    """查看或在原资源锁内续采原计划；参数：原执行、预览及保留窗口；返回：预览或执行结果。"""
    import re
    from ecommerce.modules.collection.recovery import preview as recovery_preview
    from ecommerce.infrastructure.storage.collection_recovery import RecoveryStore
    from ecommerce.infrastructure.tasks.ownership import execution_key
    import sqlalchemy as sa
    if not isinstance(task_id,str) or not re.fullmatch('[a-f0-9]{32}',task_id):
        raise ValueError('原任务编号无效')
    if settings.task_backend!='mysql' or settings.collection_backend!='mysql':
        raise ValueError('续采需要MySQL任务及采集存储')
    browser_settings=settings.for_platform('taobao')
    persistence=RecoveryStore(settings)
    table=persistence.tables['tasks']
    with persistence.engine.connect() as connection:
        owner=connection.scalar(sa.select(table.c.execution_key).where(table.c.id==task_id))
    if owner!=execution_key(browser_settings):
        raise ValueError('原任务不属于当前浏览器执行配置')
    view=recovery_preview(settings,task_id)
    if preview: return view
    if view['finished']: raise ValueError('原计划已经结束，请选择失败模块单独采集')
    options=view['options']
    def action():
        """已取得浏览器锁并补写原流水后，再次核对计划；参数：无；返回：业务结论。"""
        latest=recovery_preview(settings,task_id)
        if latest['revision']!=view['revision']:
            raise ValueError('原计划进度已变化，请重新查看恢复计划')
        if options['mode']=='keyword':
            from ecommerce.modules.collection.service import collect_keyword
            return collect_keyword(browser_settings,options['keyword'],pages=options['pages'],limit=options['limit'],
                with_details=options['with_details'],wait=wait,resume_task_id=task_id,
                on_attention=None if settings.headless else prompt_taobao_attention)
        from ecommerce.modules.collection.detail_service import open_detail_input
        from ecommerce.shared.collection_contract import MODULES
        return open_detail_input(browser_settings,options['input'],wait=wait,resume_task_id=task_id,
            all_reviews=options['all_reviews'],all_questions=options['all_questions'],
            on_attention=None if settings.headless else prompt_taobao_attention,
            **{name:name in options['modules'] for name in MODULES})
    from ecommerce.modules.collection.requests import execute as request_execute
    return request_execute(browser_settings,'collection.taobao.resume',action,runner=run_task,options=options,
        resume_task_id=task_id,resume_revision=view['revision'])
