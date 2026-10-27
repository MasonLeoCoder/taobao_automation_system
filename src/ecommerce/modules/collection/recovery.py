"""原计划内的页、商品、模块恢复；未完成模块一律清理后新建批次重采。"""
from copy import deepcopy
import json
from pathlib import Path
from ecommerce.infrastructure.tasks.events import current_events, emit_event
from ecommerce.infrastructure.storage.collection_recovery import RecoveryStore, COMPLETE
from ecommerce.shared.collection_contract import MODULES
from ecommerce.shared.exceptions import NeedsAttention


def current():
    """取得本次恢复会话；参数：无；返回：会话或空。"""
    session = current_events()
    return getattr(session, 'recovery', None) if session else None


def register_attempt(settings,product_id,module,batch_id,task_id):
    """下载前登记批次目录归属，突然退出也能精确清理；参数：明确归属；返回：无。"""
    from ecommerce.infrastructure.tasks.ownership import atomic_json
    from ecommerce.modules.collection.detail_storage import product_directory
    directory=product_directory(settings,product_id)/'history'/batch_id
    atomic_json(directory/'.collection-owner.json',{'product_id':product_id,'module':module,
        'batch_id':batch_id,'task_id':task_id,'status':'running'})


def begin(settings, options, *, resume_task_id=None, cleanup_tasks=None):
    """浏览器启动前固定原计划或领取恢复计划；参数：原范围及来源；返回：恢复会话或空。"""
    session = current_events()
    if session is None or settings.collection_backend != 'mysql':
        if resume_task_id:
            raise ValueError('完整任务恢复需要 MySQL 任务和采集存储')
        return None
    existing = current()
    if existing:
        return existing
    recovery = RecoverySession(settings, session, options, resume_task_id, cleanup_tasks or [])
    session.recovery = recovery
    recovery.persist('采集计划及恢复来源已固定')
    session.checkpoint('采集计划已确认入库')
    return recovery


class RecoverySession:
    """只在持有浏览器资源锁的执行线程内推进，快照通过原持久流水入库。"""
    def __init__(self, settings, session, options, parent_task_id, cleanup_tasks):
        self.settings, self.session = settings, session
        self.store = RecoveryStore(settings)
        old = self.store.read(parent_task_id) if parent_task_id else None
        if parent_task_id and (old is None or old['format_version'] != 1):
            raise ValueError('该旧任务没有可靠恢复计划，请选择具体模块重新采集')
        if old and (old['options'] != options or old['active_task_id'] != parent_task_id):
            raise ValueError('原采集范围或最近执行已变化，请重新查看恢复计划')
        self.id = old['id'] if old else session.task_id
        self.parent_task_id = parent_task_id
        self.options = deepcopy(options)
        self.state = deepcopy(old['checkpoint']) if old else {'pages': {}, 'products': {}, 'finished': False}
        self.state['finished'] = False
        self.revision = old['revision'] if old else 0
        self.cleanup_tasks = list(dict.fromkeys((old['checkpoint'].get('cleanup_tasks',[])+self.store.lineage(self.id)) if old else cleanup_tasks))
        self.state['cleanup_tasks']=self.cleanup_tasks
        self.reused = 0

    def snapshot(self):
        """生成不可变流水载荷；参数：无；返回：完整检查点副本。"""
        return {'id': self.id, 'parent_task_id': self.parent_task_id, 'options': deepcopy(self.options),
                'checkpoint': deepcopy(self.state), 'revision': self.revision}

    def changed(self):
        """推进本地版本；参数：无；返回：无。"""
        self.revision += 1

    def persist(self, message):
        """通过持久流水保存独立检查点；参数：提示；返回：无。"""
        self.changed()
        operation = self.session.operation('recovery', 'collection_checkpoint', message)
        operation['recovery'] = self.snapshot()
        self.session.collection_barrier = self.session.append(operation)
        self.session.recovery_exported_revision=self.revision

    def module(self, product_id, name):
        """读取原计划模块处理结果；参数：商品、模块；返回：字典。"""
        return self.state['products'].get(product_id, {}).get(name, {})

    def should_skip(self, product_id, name):
        """只复用本计划的完成、无入口或失败已跳过决策；参数：商品、模块；返回：是否跳过。"""
        old = self.module(product_id, name)
        if old.get('state') not in {'complete', 'no_entry', 'failed_skipped', 'not_run_skipped'}:
            return False
        # 完成结果关联批次不能只靠当前商品资料推断；文件缺失则回到清理/重采或人工处理。
        if old.get('state') == 'complete' and old.get('history_record_path'):
            from ecommerce.infrastructure.storage.collection_snapshot import owned_path, digest_file
            path = owned_path(self.settings.workspace, old['history_record_path'])
            if not path.is_file() or (old.get('record_sha256') and digest_file(path) != old['record_sha256']):
                raise NeedsAttention('原任务已完成结果文件缺失或变化，请核对后单模块重采')
        if old.get('state')=='complete':
            self.store.validate_result(old['task_id'],product_id,name)
        self.reused += 1
        emit_event('collection_module_reused', '复用原计划处理结果', details={'product_id':product_id,'module':name,**old})
        return True

    def prepare(self, product_id, name):
        """清理已停止的对应模块旧批次，再登记新执行；参数：商品、模块；返回：无。"""
        old = self.module(product_id, name)
        self.state['products'].setdefault(product_id, {})[name] = {**old, 'state':'cleaning', 'cleanup_sources': self.cleanup_tasks}
        self.persist('正在清理未完成模块')
        self.session.checkpoint('清理计划已入库')
        if self.cleanup_tasks:
            for source in self.store.batches(self.cleanup_tasks, product_id, name):
                self.check_stop()
                counts = self.store.cleanup(self.session.task_id, source, product_id, name)
                emit_event('collection_cleanup', '未完成模块旧明细及专属文件已清理', details={
                    'product_id':product_id,'module':name,'source_batch_key':source['source_batch_key'],'counts':counts})
        self.state['products'][product_id][name] = {'state':'running','task_id':self.session.task_id}
        self.changed()

    def check_stop(self):
        """文件清理间隙响应明确停止，不借用无关平台控制；参数：无；返回：无。"""
        from ecommerce.platforms.taobao.verification import active_guard
        guard = active_guard()
        if guard:
            guard.checkpoint('清理未完成模块')

    def finish_module(self, product_id, name, value, *, failed=False):
        """在模块汇总快照前更新检查点，随结果同事务提交；参数：结果及失败标志；返回：无。"""
        status = value.get('video_status', value.get('status'))
        state = 'failed_skipped' if failed else 'no_entry' if status == 'skipped' else 'complete'
        data = {'state':state,'task_id':self.session.task_id,'status':status,
                'message':value.get('message',''),'stop_reason':value.get('stop_reason')}
        if value.get('progress'):data['progress']=deepcopy(value['progress'])
        for key in ('batch_id','history_record_path','record_path','captured_at'):
            if value.get(key) is not None:
                data[key] = str(value[key])
        if data.get('history_record_path'):
            from ecommerce.infrastructure.storage.collection_snapshot import owned_path, digest_file
            path = owned_path(self.settings.workspace, data['history_record_path'])
            if path.is_file():
                data['record_sha256'] = digest_file(path)
        self.state['products'].setdefault(product_id, {})[name] = data
        self.changed()

    def page(self, number, collected=None):
        """读取或保存已确认列表清单引用；参数：页码、可选批次；返回：批次或空。"""
        if collected is not None:
            self.state['pages'][str(number)] = deepcopy(collected)
            self.persist('列表批次及本页商品清单已固定')
            self.session.checkpoint('列表恢复检查点已入库')
        return self.state['pages'].get(str(number))

    def finish(self):
        """整批结束保留模块错误，不把失败改成完成；参数：无；返回：汇总计数。"""
        self.state['finished'] = True
        counts = self.counts()
        self.state['counts'] = counts
        self.persist('采集流程已结束，处理结果已汇总')
        self.session.checkpoint('采集结束检查点已确认')
        return counts

    def counts(self):
        """按实际模块决策计数；参数：无；返回：成功、无需采集、失败、未执行和复用数量。"""
        values = [value for product in self.state['products'].values() for value in product.values()]
        counts={'complete':sum(v.get('state')=='complete' for v in values), 'no_entry':sum(v.get('state')=='no_entry' for v in values),
                'failed':sum(v.get('state')=='failed_skipped' and not v.get('resolved_by_task_id') for v in values),
                'not_run':sum(v.get('state') in {'running','cleaning','not_run_skipped'} and not v.get('resolved_by_task_id') for v in values), 'reused':self.reused}
        if any(v.get('resolved_by_task_id') for v in values):counts['resolved']=sum(bool(v.get('resolved_by_task_id')) for v in values)
        return counts


def preview(settings, task_id):
    """生成只读恢复预览；参数：配置、原执行；返回：原范围、恢复位置及待处理项。"""
    store = RecoveryStore(settings)
    plan = store.read(task_id)
    if plan is None or plan['format_version'] != 1:
        raise ValueError('该旧任务没有可靠恢复计划，可选择具体模块重新采集')
    if plan['active_task_id'] != task_id:
        raise ValueError('已有更新的执行，请从最近执行查看恢复计划')
    items = []
    for product_id, product in plan['checkpoint']['products'].items():
        for module, value in product.items():
            action = 'reuse' if value['state'] in {'complete','no_entry'} else 'manual_retry' if value['state'] in {'failed_skipped','not_run_skipped'} else 'clean_restart'
            items.append({'product_id':product_id,'module':module,'action':action,**value})
    lineage=store.lineage(plan['id'])
    for item in items:
        if item['action']=='clean_restart':
            candidates=store.batches(lineage,item['product_id'],item['module'])
            directories=[settings.workspace/'output/raw/taobao_details'/item['product_id']/'history'/b['source_batch_key'] for b in candidates]
            item['cleanup']={'batch_count':len(candidates),'file_count':sum(sum(p.is_file() for p in directory.rglob('*')) for directory in directories if directory.exists())}
    selected=plan['options'].get('modules',list(MODULES) if plan['options'].get('with_details') else [])
    pending=[]
    pages=plan['checkpoint']['pages']
    products=[]
    if plan['options']['mode']=='detail':
        from ecommerce.platforms.taobao.detail import resolve_detail_input
        products=[(None,resolve_detail_input(plan['options']['input']).item_id)]
    else:
        from ecommerce.infrastructure.storage.collection_snapshot import owned_path
        for number,collected in sorted(pages.items(),key=lambda pair:int(pair[0])):
            path=owned_path(settings.workspace,collected['json_path'])
            if path.is_file():
                products.extend((int(number),row['product_id']) for row in json.loads(path.read_text(encoding='utf-8'))['items'])
    for page,product_id in products:
        for module in selected:
            value=plan['checkpoint']['products'].get(product_id,{}).get(module,{})
            if value.get('state') not in {'complete','no_entry','failed_skipped','not_run_skipped'}:
                pending.append({'page':page,'product_id':product_id,'module':module})
    next_item=pending[0] if pending else None
    if next_item is None and plan['options']['mode']=='keyword' and not plan['checkpoint'].get('finished') and len(pages)<plan['options']['pages']:
        next_item={'page':max([int(n) for n in pages],default=0)+1,'product_id':None,'module':'list'}
    return {'plan_id':plan['id'],'revision':plan['revision'],'options':plan['options'],'items':items,
            'next':next_item,'pending':pending,
            'pages':sorted(int(n) for n in plan['checkpoint']['pages']), 'finished':plan['checkpoint'].get('finished',False)}
