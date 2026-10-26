"""基本信息独立批次保存，不依赖SKU完整性或规格图片。"""
from uuid import uuid4
from ecommerce.infrastructure.storage.database import now
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.modules.collection.detail_storage import product_directory,save_detail_sections
from ecommerce.platforms.taobao.detail import matches_detail_url,resolve_detail_input
from ecommerce.platforms.taobao.detail_basic_info import read_basic_info
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.basic_info import assessment
from ecommerce.shared.exceptions import NeedsAttention


def collect_basic_info(settings,result):
    """只采当前商品基本信息；参数：配置、详情页；返回：独立保存结果及真实状态。"""
    guard=active_guard()
    if guard is None:
        raise RuntimeError('基本信息采集必须在淘宝守卫中运行')
    value=read_basic_info(result)
    captured=now()
    source=resolve_detail_input(result.page.url).canonical_url
    info={**value,'captured_at':captured}
    record={'schema_version':1,'product_id':result.target.item_id,'task_id':guard.task_id,
            'batch_id':guard.task_id+'-basic-'+uuid4().hex[:8],
            'captured_at':captured,'source_url':source,'scope':'product_basic_info','product_info':info,
            **assessment(info,source,captured)}
    directory=product_directory(settings,result.target.item_id)/'history'/record['batch_id']
    path=directory/'basic_info.json'
    record['history_record_file']=path.relative_to(directory.parent.parent).as_posix()
    def save():
        """确认目标后保存独立原件及有效/尝试分区；参数：无；返回：商品汇总文件。"""
        if not matches_detail_url(result.page,result.target.item_id):
            raise NeedsAttention('保存基本信息前目标商品发生变化')
        atomic_json(path,record)
        sections={'product_info_last_attempt':record}
        if record['status']=='complete':
            sections['product_info']={**info,'batch_id':record['batch_id'],'task_id':record['task_id'],'status':record['status']}
        return save_detail_sections(directory.parent.parent,result.target.item_id,sections)
    destination=guard.perform('保存基本信息',save)
    guard.checkpoint('基本信息已确认入库')
    return {**record,'record_path':str(destination),'history_record_path':str(path),
            'message':'基本信息采集'+('完成' if record['status']=='complete' else '部分完成，标题或来源未确认')}
