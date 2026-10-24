"""本次详情采集的模块状态、警告与实际文件位置汇总，不替代任务执行器状态。"""
from ecommerce.infrastructure.tasks.events import notify as print
import json
from uuid import uuid4

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_storage import product_directory, read_detail_document, save_detail_section
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped


LABELS = {
    'video':'视频','main_images':'主图','skus':'SKU','parameters':'参数',
    'detail_images':'详情图','review_summary':'评价概览','reviews':'用户评价','questions':'问大家',
}
STATUS_LABELS = {
    'complete':'完成','downloaded':'完成','partial':'部分完成','failed':'失败',
    'no_video':'无视频（已确认）','no_parameters':'无参数（已确认）','no_images':'无详情图（已确认）',
    'no_reviews':'无评价（已确认）','no_questions':'无问答（已确认）',
    'pending':'未执行','not_run':'未执行（前序模块未通过）','not_requested':'未启用',
    'running':'尚未完成','interrupted':'已中断','needs_attention':'需要人工处理',
}


class DetailCollectionReport:
    """按任务记录当前商品的所选模块，保留历史及最新 collection_summary 分区。"""
    def __init__(self,settings,item_id,task_id,requested):
        self.product_dir=product_directory(settings,item_id)
        batch_id=task_id+'-summary-'+uuid4().hex[:8]
        self.path=self.product_dir/'history'/batch_id/'collection_summary.json'
        self.record={'schema_version':1,'product_id':item_id,'task_id':task_id,'batch_id':batch_id,
                     'started_at':now(),'finished_at':None,'status':'running','warnings':[],
                     'history_record_file':self.path.relative_to(self.product_dir).as_posix(),
                     'steps':[{'name':name,'label':label,'requested':bool(requested[name]),
                               'status':'pending' if requested[name] else 'not_requested',
                               'continuation_allowed':False,'warnings':[]} for name,label in LABELS.items()]}
        self.save()

    def save(self):
        """保存本次历史汇总并合并商品JSON，其他商品资料分区保持原内容。"""
        self.record['updated_at']=now()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(f'.summary-{uuid4().hex}.json.tmp')
        try:
            temporary.write_text(json.dumps(self.record,ensure_ascii=False,indent=2),encoding='utf-8')
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.record_path=save_detail_section(self.product_dir,self.record['product_id'],'collection_summary',self.record)

    def start(self,name):
        """在执行模块前记录运行状态，避免中断后仍显示未开始或成功。"""
        self._step(name).update(status='running',started_at=now())
        self.save()

    def finish(self,name,value,allowed):
        """记录实际模块状态及允许继续的判定，部分结果不会改写为完整结果。"""
        step=self._step(name)
        step.update(status=value.get('video_status') if name=='video' else value.get('status','unconfirmed'),
                    continuation_allowed=allowed,finished_at=now(),message=value.get('message',''),
                    warnings=value.get('warnings',[]),blocking_issues=value.get('blocking_issues',[]))
        for key in ('record_path','history_record_path','image_directory','result_section','summary','stop_reason'):
            if key in value:
                step[key]=value[key]
        self.save()

    def finalize(self,error=None):
        """结束汇总：所有所选模块完成但存在允许继续的缺失时，标记含警告完成。"""
        self.record['warnings']=[{'module':step['name'],**warning} for step in self.record['steps'] for warning in step['warnings']]
        for step in self.record['steps']:
            if step['status']=='pending':
                step.update(status='not_run',reason='前序模块未通过，未执行本项目')
        self.record.update(status='partial' if error else 'complete_with_warnings' if self.record['warnings'] else 'complete',
                           finished_at=now(),error=error)
        self.save()

    def interrupt(self,error):
        """保存中断/需人工/失败状态和未执行项目，不把异常降级为可继续警告。"""
        status=('interrupted' if isinstance(error,(TaskStopped,KeyboardInterrupt)) else
                'needs_attention' if isinstance(error,NeedsAttention) else 'failed')
        for step in self.record['steps']:
            if step['status']=='running':
                step.update(status=status,continuation_allowed=False,error_type=type(error).__name__,finished_at=now())
                if step['name']=='skus':
                    self._attach_sku_progress(step)
            elif step['status']=='pending':
                step.update(status='not_run',reason='前序模块中断，未执行本项目')
        self.record.update(status=status,error_type=type(error).__name__,finished_at=now())
        self.record['warnings']=[{'module':step['name'],**warning} for step in self.record['steps'] for warning in step['warnings']]
        self.save()

    def display(self):
        """打印各模块状态、待核对项及本次部分结果的真实目录，并返回简短任务结语。"""
        status={'complete':'详情采集完成','complete_with_warnings':'详情采集完成（含警告，部分数据待核对）',
                'partial':'详情采集部分完成','interrupted':'详情采集已中断',
                'needs_attention':'详情采集需要人工处理','failed':'详情采集失败'}.get(self.record['status'],'详情采集尚未完成')
        print('\n【本次详情采集汇总】'+status,flush=True)
        for step in self.record['steps']:
            suffix='（记录警告后允许继续）' if step['status']=='partial' and step['continuation_allowed'] else ''
            print(step['label']+'：'+STATUS_LABELS.get(step['status'],step['status'])+suffix,flush=True)
            for warning in step['warnings']:
                print('  待核对：'+warning.get('sku_name','')+'；'+warning['message'],flush=True)
            for issue in step.get('blocking_issues',[]):
                print('  阻断项：'+issue.get('sku_name','')+'；'+issue['message'],flush=True)
            if step['status'] in {'partial','interrupted','needs_attention','failed'}:
                if step.get('history_record_path'):
                    print('  本次部分结果：'+step['history_record_path'],flush=True)
                if step.get('progress_record_path'):
                    print('  本次进度记录：'+step['progress_record_path'],flush=True)
                if step.get('image_directory'):
                    print('  本次图片目录：'+step['image_directory'],flush=True)
        print('采集汇总：'+str(self.path.resolve()),flush=True)
        return status+'；汇总：'+str(self.path.resolve())

    def _step(self,name):
        return next(step for step in self.record['steps'] if step['name']==name)

    def _attach_sku_progress(self,step):
        """异常时只附加同一任务已登记的SKU进度，不把其他批次的部分结果当成本次结果。"""
        value=read_detail_document(self.product_dir,self.record['product_id']).get('sku_details_last_attempt',{})
        if not isinstance(value,dict) or value.get('task_id')!=self.record['task_id']:
            return
        step['result_section']='sku_details_last_attempt'
        for key,field in (('progress_record_file','progress_record_path'),('history_record_file','history_record_path')):
            relative=value.get(key)
            if not isinstance(relative,str):
                continue
            path=self.product_dir/relative
            try:
                path.resolve().relative_to(self.product_dir.resolve())
            except ValueError:
                continue
            if path.is_file():
                step[field]=str(path.resolve())
                images=path.parent/'sku图'
                try:
                    images.resolve().relative_to(self.product_dir.resolve())
                except ValueError:
                    continue
                if images.is_dir():
                    step['image_directory']=str(images.resolve())
        for key in ('summary','warnings','blocking_issues'):
            if key in value:
                step[key]=value[key]
