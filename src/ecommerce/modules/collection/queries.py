"""采集查询的公开业务入口，统一 CLI/Web 的数据库启用状态和范围。"""
from ecommerce.infrastructure.storage import collection_queries as storage


def history_page(history,page=1,*,sort_by=None,order=None):
    """排序并分页已读取的商品历史；参数：历史、页码和排序；返回：当前页及排序信息。"""
    from ecommerce.shared.pagination import PAGE_SIZE,pagination
    from ecommerce.shared.sorting import sorting
    spec=sorting(sort_by,order,{'captured_at','module','status'},'captured_at','desc')
    key=spec['sort']
    present=[row for row in history if row.get(key) is not None]
    missing=[row for row in history if row.get(key) is None]
    ordered=sorted(present,key=lambda row:(row[key],row['id']),reverse=spec['order']=='desc')+sorted(missing,key=lambda row:row['id'])
    info=pagination(len(ordered),page)
    return {'history_rows':ordered[(page-1)*PAGE_SIZE:page*PAGE_SIZE],**info,'sort_spec':spec}


def query(settings,query_type,**parameters):
    """查询商品、批次或明细；参数：配置、种类和已解析筛选；返回：统一查询结果。"""
    if settings.collection_backend!='mysql':
        raise ValueError('当前采集数据库尚未启用，请完成历史核对和配置切换')
    from ecommerce.infrastructure.storage.collection_browser import browse
    if query_type.startswith('browse_'):
        result=browse(settings, query_type.removeprefix('browse_'), **parameters)
        inspect_thumbnails(settings,result.get('observations',[]))
        inspect_thumbnails(settings,result.get('products',[]))
        return result
    actions={'products':storage.products,'batches':storage.batches,'records':storage.records}
    if query_type not in actions:
        raise ValueError('不支持的采集查询')
    result=actions[query_type](settings,**parameters)
    if query_type=='records' and result.get('kind')=='media':
        inspect_media(settings,result['records'])
    if query_type=='records' and result.get('kind') in {'items','skus'}:
        inspect_thumbnails(settings,result['records'])
    if query_type=='records' and result.get('kind')=='reviews':
        for row in result['records']:
            inspect_media(settings,row.get('media',[]))
    if query_type=='records' and result.get('kind')=='questions':
        from ecommerce.modules.collection.media_view import media_preview
        for question in result['records']:
            for answer in question.get('answers',[]):
                inspect_media(settings,answer.get('media',[]))
                answer['media']=[media_preview(item) for item in answer.get('media',[])]
    return result


def inspect_thumbnails(settings,rows):
    """校验列表或规格图片原件；参数：配置及观察行；返回：无，缺失或变化时给出占位提示。"""
    for row in rows:
        if 'thumbnail_candidates' in row:
            candidates=row.pop('thumbnail_candidates')
            inspect_thumbnails(settings,candidates)
            preview=next((item for item in candidates if item.get('thumbnail')),None)
            if preview:
                row.update({key:preview.get(key) for key in ('thumbnail','thumbnail_file','thumbnail_message')})
            elif candidates:
                row['thumbnail_message']=candidates[-1].get('thumbnail_message') or '暂无缩略图'
            continue
        if not row.get('thumbnail_file'):
            continue
        check={'file':row['thumbnail_file']}
        if row['thumbnail_file'].get('availability')=='missing':
            check['preview_status']='missing'
        else:
            inspect_media(settings,[check])
        if check['preview_status']!='present':
            row['thumbnail']=None
            row['thumbnail_message']={'missing':'本地图片缺失','changed':'图片校验失败'}.get(check['preview_status'],'本地图片不可用')


def inspect_media(settings,rows):
    """核对预览原件，不修改文件索引；参数：配置、媒体记录；返回：无，补充展示状态。"""
    from pathlib import Path
    from ecommerce.infrastructure.storage.collection_snapshot import digest_file
    root=settings.workspace.resolve()
    for row in rows:
        asset=row.get('file')
        status='unavailable'
        if asset and asset.get('availability')=='present' and asset.get('root_key')=='workspace':
            path=(root/Path(asset['relative_path'])).resolve()
            if path.is_relative_to((root/'output').resolve()):
                try:
                    status='missing' if not path.is_file() else 'changed' if path.stat().st_size!=asset['size_bytes'] or digest_file(path)!=asset['sha256'] else 'present'
                except OSError:
                    status='unavailable'
        row['preview_status']=status
