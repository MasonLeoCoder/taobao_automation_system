"""图片来源冻结与通用五表存取，固定商品、角色及发布批次。"""
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.infrastructure.images import vision
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.modules.composite_analysis.store import identity

TYPE, VERSION = 'images', 'images-v1'
ROLES = {'main_images':('main_image','主图'), 'detail_images':('detail_image','详情')}


def choices(conn,tables,product_id):
    """读取全部已发布图片批次的轻量候选；不跨商品借用。"""
    batch=tables['collection_batches']
    return {kind:[dict(v) for v in conn.execute(sa.select(batch.c.id,batch.c.captured_at,batch.c.status)
        .where(batch.c.product_id==product_id,batch.c.module==kind,batch.c.visibility=='published')
        .order_by(batch.c.captured_at.desc(),batch.c.id.desc())).mappings()] for kind in ROLES}


def inputs(settings,conn,tables,product,main_batch=None,detail_batch=None,allow_partial=False):
    """完整校验并准备实际来源；参数：两批次；返回：固定输入、商品、候选。"""
    if type(allow_partial) is not bool: raise ValueError('部分范围确认无效')
    item=identity(conn,tables,product); options=choices(conn,tables,item['id']); batches=[]; images=[]; issues=[]
    b,m,f=(tables[n] for n in ('collection_batches','media_references','file_assets'))
    for module,selected in (('main_images',main_batch),('detail_images',detail_batch)):
        role,label=ROLES[module]
        if selected is None: selected=options[module][0]['id'] if options[module] else None
        if selected is None or selected == 0:
            issues.append(dict(label=label,reason='未选择可用来源批次')); continue
        if type(selected) is not int or selected<1: raise ValueError('图片批次编号无效')
        batch=conn.execute(sa.select(b).where(b.c.id==selected,b.c.product_id==item['id'],b.c.module==module,b.c.visibility=='published')).mappings().one_or_none()
        if not batch: raise LookupError('所选图片批次不属于本商品、模块或尚未发布')
        batches.append(dict(module=module,id=selected,captured_at=str(batch['captured_at']),status=batch['status']))
        rows=conn.execute(sa.select(m.c.id.label('media_id'),m.c.position,*f.c).select_from(m.outerjoin(f,m.c.file_asset_id==f.c.id))
            .where(m.c.batch_id==selected,m.c.role==role).order_by(m.c.position,m.c.id)).mappings().all()
        if not rows: issues.append(dict(label=label,reason='所选批次没有图片'))
        for index, row in enumerate(rows,1):
            name=label+str(index)
            asset={k:row[k] for k in ('id','root_key','relative_path','size_bytes','sha256','availability')}
            try: processed=vision.prepare(settings,asset)
            except (ValueError,OSError) as error:
                reason=str(error) if isinstance(error,ValueError) else '本地文件无法读取'
                issues.append(dict(label=name,media_id=row['media_id'],batch_id=selected,reason=reason)); continue
            images.append(dict(media_id=row['media_id'],batch_id=selected,module=module,position=row['position'],label=name,asset=asset,**processed))
    incoming=dict(version='images-input-v1',product=repository.product_context(conn,tables,item),batches=batches,images=images,
        scope=dict(issues=issues,allow_partial=allow_partial,source_count=len(images),
            note='仅所选已采集图片；主图与详情可能采于不同时间；不等于全平台、真实SKU包含物或性能证明'))
    return incoming,item,options


def create(settings,product,selection,expected_hash,rules,parameters,provider,model,force=False):
    """商品行锁内重核原图，活动互斥、成功明确选择、失败新建。"""
    with repository.connection(settings,True) as (conn,tables):
        p,t,s=(tables[n] for n in ('products','analysis_tasks','analysis_sources'))
        conn.execute(sa.select(p.c.id).where(p.c.platform=='taobao',p.c.platform_product_id==product).with_for_update()).all()
        incoming,item,_=inputs(settings,conn,tables,product,**selection)
        if fingerprint(incoming)!=expected_hash or fingerprint(parameters['plan']['input'])!=expected_hash:
            raise ValueError('图片来源或处理结果已变化，请重新预览，未发送')
        digest=fingerprint(dict(product=item['id'],input=incoming,rules=rules,provider=provider,model=model,
            output=parameters['plan']['report_output_tokens']))
        existing=conn.execute(sa.select(t.c.id,t.c.active_key,t.c.status).where(t.c.analysis_type==TYPE,t.c.fingerprint==digest,
            sa.or_(t.c.active_key.is_not(None),t.c.status=='succeeded')).order_by(t.c.created_at.desc(),t.c.id.desc())).mappings().all()
        active=next((v for v in existing if v['active_key']),None)
        if active or existing and not force: return dict(active or existing[0]),False
        now=database_now(); row=dict(id=uuid4().hex,product_id=item['id'],analysis_type=TYPE,fingerprint=digest,active_key=digest,
            status='queued',provider=provider,model=model,rule_version=VERSION,rules=rules,parameters=parameters,
            source_count=len(incoming['images']),processed_count=0,usage={},message='图片及发送范围已固定，等待后台启动。',
            owner=None,execution_task_id=None,created_at=now,updated_at=now)
        conn.execute(t.insert().values(**row))
        for pos,image in enumerate(incoming['images'],1):
            conn.execute(s.insert().values(id=uuid4().hex,analysis_id=row['id'],batch_id=image['batch_id'],review_id=None,
                question_id=None,media_id=image['media_id'],position=pos,snapshot=image,content_hash=fingerprint(image),
                scope=dict(module=image['module'],processing=vision.VERSION,batch_id=image['batch_id'])))
        return row,True


def history(settings,product):
    """只排序轻量历史列，避免大JSON进入MySQL排序缓存。"""
    with repository.connection(settings) as (conn,tables):
        item=identity(conn,tables,product); t=tables['analysis_tasks']
        query=sa.select(*[t.c[k] for k in ('id','status','created_at','model','message','owner','source_count')]).where(t.c.product_id==item['id'],t.c.analysis_type==TYPE)
        order=(t.c.created_at.desc(),t.c.id.desc())
        rows=[dict(v) for v in conn.execute(query.order_by(*order).limit(100)).mappings()]
        latest=conn.execute(query.where(t.c.status=='succeeded').order_by(*order).limit(1)).mappings().one_or_none()
        return dict(tasks=rows,latest_result=dict(latest) if latest else None,choices=choices(conn,tables,item['id']))


def verify_sources(settings,task,ids):
    """联网前重核数据库归属、发布状态及原文件索引，不因旧快照绕过来源守卫。"""
    if not ids: return
    with repository.connection(settings) as (conn,tables):
        m,b,f=(tables[n] for n in ('media_references','collection_batches','file_assets'))
        rows=conn.execute(sa.select(m.c.id,m.c.batch_id,m.c.role,m.c.file_asset_id,b.c.module,f.c.sha256,f.c.size_bytes,f.c.relative_path,
            f.c.root_key,f.c.availability).join(b,b.c.id==m.c.batch_id).join(f,f.c.id==m.c.file_asset_id)
            .where(m.c.id.in_(ids),b.c.product_id==task['product_id'],b.c.visibility=='published')).mappings().all()
        actual={v['id']:v for v in rows}
        for source in task['parameters']['plan']['input']['images']:
            if source['media_id'] not in ids: continue
            value=actual.get(source['media_id'])
            if not value or value['batch_id']!=source['batch_id'] or value['module']!=source['module'] or value['role']!=ROLES[source['module']][0] or value['file_asset_id']!=source['asset']['id'] or any(value[k]!=source['asset'][k] for k in ('sha256','size_bytes','relative_path','root_key','availability')):
                raise ValueError('图片来源归属、发布状态或文件索引已变化，未发送')
