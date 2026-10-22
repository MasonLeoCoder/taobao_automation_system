"""保存商品基础信息、SKU 价格和按规格名称命名的原图片。"""
import hashlib
import json
import re
import shutil
import unicodedata
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from uuid import uuid4

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_images import _remove_owned_tree, download_image, reuse_image_file
from ecommerce.modules.collection.detail_storage import product_directory, read_detail_document, save_detail_section, save_detail_sections
from ecommerce.platforms.taobao.detail import matches_detail_url, resolve_detail_input
from ecommerce.platforms.taobao.detail_skus import collect_sku_data
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention

SKU_IMAGE_FOLDER = 'sku图'
RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$', 'CONIN$', 'CONOUT$',
                  *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


def filename_stem(name, identity, used):
    """Windows 文件名清理；保留原 SKU 名称于 JSON，并阻止大小写等冲突。"""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', unicodedata.normalize('NFC', name)).strip().rstrip('. ')
    stem = stem or '未命名规格'
    if stem.split('.')[0].upper() in RESERVED_NAMES:
        stem = 'SKU_' + stem
    while len(stem.encode('utf-16-le')) > 192:
        stem = stem[:-1]
    stem = stem.rstrip('. ')
    key = stem.casefold()
    if key in used:
        suffix = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:10]
        stem = stem[:78].rstrip('. ') + '_' + suffix
        counter = 2
        candidate = stem
        while candidate.casefold() in used:
            candidate = stem + '_' + str(counter)
            counter += 1
        stem = candidate
    used.add(stem.casefold())
    return stem


def _check_owned(path, product_dir):
    resolved = path.resolve()
    resolved.relative_to(product_dir.resolve())
    if resolved == product_dir.resolve():
        raise ValueError('SKU 图片路径不能是商品根目录')


def _write_json(path, value):
    """原子保存本批SKU文件，避免中途停止留下不完整JSON。"""
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(f'.{path.stem}-{uuid4().hex}.json.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _summary(record):
    """分别统计规格选项、候选组合、价格、显式SKU标识与图片，不把候选数当真实SKU数。"""
    rows=record['skus']
    return {
        'option_count':sum(len(g.get('options',[])) for g in record['groups']),
        'candidate_count':record['candidate_count'],'processed_count':len(rows),
        'recognized_count':sum(bool(s.get('combination_confirmed')) for s in rows),
        'selectable_count':sum(s.get('stock_status')=='selectable' for s in rows),
        'explicit_sku_id_count':len({s['sku_id'] for s in rows if s.get('sku_id')}),
        'sku_id_unconfirmed_count':sum(not s.get('sku_id') for s in rows),
        'price_collected_count':sum(s.get('price_status')=='collected' for s in rows),
        'out_of_stock_count':sum(s.get('stock_status')=='out_of_stock' for s in rows),
        'unavailable_unconfirmed_count':sum(s.get('stock_status')=='unavailable_unconfirmed' for s in rows),
        'image_downloaded_count':sum(s.get('image_status')=='downloaded' for s in rows),
        'image_reused_count':sum(bool((s.get('image') or {}).get('reused_from_batch_id')) for s in rows),
        'image_failed_count':sum(s.get('image_status')=='download_failed' for s in rows),
        'no_image_count':sum(s.get('image_status')=='no_image' for s in rows),
    }


def sku_continuation_allowed(record):
    """仅允许完整结果或孤立不可选警告继续；未确认组合、价格和图片失败仍拦截。"""
    rows=record.get('skus',[])
    if record.get('status') not in {'complete','partial'} or not rows or len(rows)!=record.get('candidate_count'):
        return False
    for sku in rows:
        if not sku.get('combination_confirmed') or sku.get('image_status') not in {'downloaded','no_image'}:
            return False
        price,stock=sku.get('price_status'),sku.get('stock_status')
        if price=='collected':
            if stock!='selectable' or sku.get('price_value') is None:
                return False
        elif price=='out_of_stock':
            if stock!='out_of_stock' or not sku.get('stock_evidence'):
                return False
        elif price=='unavailable_unconfirmed':
            if stock!='unavailable_unconfirmed' or not sku.get('unavailability_evidence') or sku.get('price') is not None or sku.get('price_value') is not None:
                return False
        else:
            return False
    return True


def _notices(record):
    """返回逐项警告及阻断原因，保留名称和具体缺失原因。"""
    warnings,issues=[],[]
    for sku in record['skus']:
        base={'index':sku['index'],'sku_name':sku['name']}
        if sku['stock_status']=='unavailable_unconfirmed':
            warnings.append({**base,'code':'sku_unavailable_unconfirmed','message':sku['price_reason']})
        if not sku.get('combination_confirmed'):
            issues.append({**base,'code':'combination_unconfirmed','message':'规格组合尚未完整确认'})
        if sku['price_status'] not in {'collected','out_of_stock','unavailable_unconfirmed'}:
            issues.append({**base,'code':'price_unconfirmed','message':sku.get('price_reason') or '价格尚未确认'})
        elif sku['price_status']=='collected' and sku.get('price_value') is None:
            issues.append({**base,'code':'price_missing','message':'可选规格的价格数值尚未确认'})
        elif sku['price_status']=='out_of_stock' and not sku.get('stock_evidence'):
            issues.append({**base,'code':'stock_evidence_missing','message':'缺货依据尚未确认'})
        elif sku['price_status']=='unavailable_unconfirmed' and not sku.get('unavailability_evidence'):
            issues.append({**base,'code':'disabled_evidence_missing','message':'选项不可选的页面依据尚未记录'})
        if sku['image_status'] not in {'downloaded','no_image'}:
            issues.append({**base,'code':'image_unconfirmed','message':sku.get('image_error') or '图片尚未保存成功'})
    return warnings,issues


def _inventory_fingerprint(item_id,groups):
    """规格组、顺序、名称与选项标识相同才允许复用图片，价格不参与恢复。"""
    values=[{'name':g['name'],'options':[{'name':o['name'],'value_id':o.get('value_id')} for o in g['options']]} for g in groups]
    return hashlib.sha256(json.dumps([item_id,values],ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()


def _resume_batches(product_dir,item_id):
    """只沿最后尝试的恢复关系读取未完成批次，兼容旧版保存的部分结果。"""
    latest=read_detail_document(product_dir,item_id).get('sku_details_last_attempt',{})
    batch=latest.get('batch_id') if isinstance(latest,dict) else None
    records,seen=[],set()
    while isinstance(batch,str) and batch and batch not in seen:
        if batch in {'.','..'} or any(c in batch for c in '/\\'):
            break
        seen.add(batch)
        directory=product_dir/'history'/batch
        try:
            _check_owned(directory,product_dir)
            path=directory/'sku_progress.json'
            if not path.exists():
                path=directory/'sku_details.json'
            _check_owned(path,product_dir)
            previous=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            break
        if (not isinstance(previous,dict) or previous.get('product_id')!=item_id or previous.get('batch_id')!=batch
                or previous.get('status') not in {'in_progress','interrupted','partial'}
                or not isinstance(previous.get('skus'),list) or not isinstance(previous.get('groups'),list)):
            break
        records.append(previous)
        batch=previous.get('resumed_from_batch_id')
    return records


def _reuse_sku_image(sku,stem,previous,directory):
    """仅复制同组合、同SKU标识、同图片地址的完整原件，不复制旧价格。"""
    candidates=[s for s in previous['skus'] if isinstance(s,dict) and s.get('properties')==sku['properties'] and s.get('sku_id')==sku['sku_id']]
    if len(candidates)!=1:
        return None
    old=candidates[0].get('image')
    if not isinstance(old,dict) or not isinstance(old.get('image_file'),str):
        return None
    prefix=SKU_IMAGE_FOLDER+'/'
    name=old['image_file'][len(prefix):] if old['image_file'].startswith(prefix) else ''
    if not name or name in {'.','..'} or any(c in name for c in '/\\'):
        return None
    source=directory.parent/previous['batch_id']/SKU_IMAGE_FOLDER/name
    try:
        _check_owned(source,directory.parent.parent)
    except ValueError:
        return None
    return reuse_image_file({'index':sku['index'],'image_url':sku['image_url']},old,source,directory/SKU_IMAGE_FOLDER,
                            file_stem=stem,image_folder=SKU_IMAGE_FOLDER,source_batch_id=previous['batch_id'])


def save_sku_result(directory, record):
    product_dir = directory.parent.parent
    item_id = record['product_id']
    document = read_detail_document(product_dir, item_id)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / 'sku_details.json'
    _write_json(destination,record)
    if record['status'] != 'complete':
        record_path = save_detail_sections(product_dir, item_id, {'sku_details_last_attempt': record})
        return {**record, 'record_path': str(record_path.resolve()), 'history_record_path': str(destination.resolve()),
                'result_section':'sku_details_last_attempt','image_directory':str((directory/SKU_IMAGE_FOLDER).resolve())}
    target = product_dir / SKU_IMAGE_FOLDER
    if target.exists():
        previous = document.get('sku_details', {}).get('skus', [])
        known = {sku['image']['image_file'].split('/')[-1]: sku['image']['sha256'] for sku in previous if sku.get('image')}
        if set(p.name for p in target.iterdir()) != set(known):
            raise ValueError('现有 SKU 图片目录与登记信息不一致，未覆盖')
        for path in target.iterdir():
            if not path.is_file() or path.is_symlink():
                raise ValueError('现有 SKU 图片存在非登记文件，未覆盖')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != known[path.name]:
                    raise ValueError('现有 SKU 图片已修改，未覆盖')
    staging = product_dir / f".sku-images-{record['batch_id']}.tmp"
    backup = product_dir / f".sku-images-{record['batch_id']}.backup"
    for path in (target, staging, backup):
        _check_owned(path, product_dir)
    latest = {**record, 'folder': SKU_IMAGE_FOLDER,
              'history_record_file': destination.relative_to(product_dir).as_posix(),
              'skus': [{**sku, 'image': {**sku['image'], 'image_path': str((target / sku['image']['image_file'].split('/')[-1]).resolve())}
                       if sku.get('image') else None} for sku in record['skus']]}
    installed = False
    try:
        shutil.copytree(directory / SKU_IMAGE_FOLDER, staging)
        if target.exists():
            target.replace(backup)
        staging.replace(target)
        installed = True
        sku_section = {k: v for k, v in latest.items() if k != 'product_info'}
        record_path = save_detail_sections(product_dir, item_id, {'product_info': latest['product_info'], 'sku_details': sku_section,
                                                                'sku_details_last_attempt':record})
    except (Exception,KeyboardInterrupt):
        if installed:
            _remove_owned_tree(target, product_dir)
        if backup.exists():
            backup.replace(target)
        raise
    finally:
        _remove_owned_tree(staging, product_dir)
    _remove_owned_tree(backup, product_dir)
    return {**latest, 'record_path': str(record_path.resolve()), 'history_record_path': str(destination.resolve()),
            'result_section':'sku_details','image_directory':str(target.resolve())}


def collect_skus(settings, result, *, settle_ms=1200):
    """本轮重新确认每项价格，逐项保存进度，仅复用同来源的已校验图片。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError('SKU 采集必须在淘宝专用守卫中运行')
    if not matches_detail_url(result.page,result.target.item_id):
        raise NeedsAttention('SKU采集开始前目标商品发生变化')
    product_dir=product_directory(settings,result.target.item_id)
    previous_batches=_resume_batches(product_dir,result.target.item_id)
    batch_id = guard.task_id + '-' + uuid4().hex[:8]
    directory = product_dir / 'history' / batch_id
    (directory / SKU_IMAGE_FOLDER).mkdir(parents=True, exist_ok=True)
    progress_path = directory / 'sku_progress.json'
    record={'schema_version':2,'product_id':result.target.item_id,'batch_id':batch_id,'task_id':guard.task_id,
            'source_url':resolve_detail_input(result.page.url).canonical_url,'scope':'product_info_and_skus',
            'captured_at':now(),'status':'in_progress','phase':'prices','product_info':{},'groups':[],'skus':[],
            'candidate_count':0,'processed_count':0,'warnings':[],'blocking_issues':[],'continuation_allowed':False,
            'price_refresh_policy':'always_current_run','inventory_fingerprint':None,
            'history_record_file':f'history/{batch_id}/sku_details.json',
            'progress_record_file':f'history/{batch_id}/sku_progress.json',
            'resumed_from_batch_id':previous_batches[0]['batch_id'] if previous_batches else None}

    def matching_batches():
        batches=[]
        for previous in previous_batches:
            if not previous['groups']:
                continue
            if _inventory_fingerprint(result.target.item_id,previous['groups'])!=record['inventory_fingerprint']:
                break
            batches.append(previous)
        return batches

    def persist():
        record.update(updated_at=now(),summary=_summary(record))
        _write_json(progress_path,record)
        save_detail_section(product_dir,result.target.item_id,'sku_details_last_attempt',record)

    def save_progress(data):
        if not matches_detail_url(result.page, result.target.item_id):
            raise NeedsAttention('保存 SKU 进度前商品发生变化')
        record.update(data)
        record['inventory_fingerprint']=_inventory_fingerprint(result.target.item_id,record['groups'])
        batches=matching_batches()
        record['resumed_from_batch_id']=batches[0]['batch_id'] if batches else None
        persist()

    persist()
    try:
        data=collect_sku_data(result,settings.timeout_ms,settle_ms=settle_ms,on_progress=save_progress)
        save_progress(data)
        record['phase']='images'
        batches=matching_batches()
        used=set()
        for sku in record['skus']:
            sku.update(image=None,image_error=None)
        persist()
        for sku in record['skus']:
            guard.checkpoint('保存下一项SKU图片')
            if not matches_detail_url(result.page,result.target.item_id):
                raise NeedsAttention('SKU图片保存前目标商品发生变化')
            if sku['image_status']=='no_image':
                continue
            identity=json.dumps(sku['properties'],ensure_ascii=False,sort_keys=True)
            stem=filename_stem(sku['name'],identity,used)
            for previous in batches:
                image=_reuse_sku_image(sku,stem,previous,directory)
                if image is not None:
                    sku.update(image=image,image_status='downloaded')
                    break
            else:
                sku['image_status']='downloading'
                persist()
                try:
                    image=download_image({'index':sku['index'],'image_url':sku['image_url']},directory/SKU_IMAGE_FOLDER,
                                         result,guard,file_stem=stem,image_folder=SKU_IMAGE_FOLDER,label='SKU图片')
                    sku.update(image=image,image_status='downloaded')
                except HTTPError as error:
                    sku.update(image_status='download_failed',image_error=f'HTTP {error.code}；未自动重试')
                    persist()
                    if error.code in {401,403,429}:
                        guard.request_attention(result.page,f'SKU 图片返回 HTTP {error.code}，请人工检查访问状态后回复继续；本图片不会自动重试',step='检查 SKU 图片访问')
                except (URLError,OSError,ValueError,IncompleteRead) as error:
                    sku.update(image_status='download_failed',image_error=f'图片下载或校验失败（{type(error).__name__}）；未自动重试')
            persist()
        record.update(phase='finished',processed_count=len(record['skus']))
        record['warnings'],record['blocking_issues']=_notices(record)
        record['status']='partial' if record['warnings'] or record['blocking_issues'] else 'complete'
        record['continuation_allowed']=sku_continuation_allowed(record)
        persist()
        def save():
            if not matches_detail_url(result.page,result.target.item_id):
                raise NeedsAttention('SKU 保存前目标商品发生变化')
            return save_sku_result(directory,record)
        saved=guard.perform('保存商品信息、SKU 价格及图片',save)
    except (Exception,KeyboardInterrupt) as error:
        record.update(status='interrupted',continuation_allowed=False,error=f'SKU任务中断（{type(error).__name__}），已保存进度；下次重新确认价格')
        for sku in record['skus']:
            if sku.get('image_status')=='downloading':
                sku.update(image_status='interrupted',image_error='本图片下载中断，未记为成功')
        try:
            persist()
        except (OSError,ValueError):
            pass
        raise
    summary=record['summary']
    for warning in record['warnings']:
        print(f"待核对SKU：{warning['sku_name']}；{warning['message']}",flush=True)
    return {**saved, 'message': f"SKU采集{('完成' if record['status']=='complete' else '部分完成')}：候选组合{summary['candidate_count']}项，确认{summary['recognized_count']}项，"
            f"价格{summary['price_collected_count']}项，缺货{summary['out_of_stock_count']}项，"
            f"不可选原因未确认{summary['unavailable_unconfirmed_count']}项，图片{summary['image_downloaded_count']}张（复用{summary['image_reused_count']}张）；"
            + ('已记录警告，允许继续后续采集' if record['status']=='partial' and record['continuation_allowed'] else '结果存在阻断项，后续采集未放行' if record['status']=='partial' else '全部候选已处理')}
