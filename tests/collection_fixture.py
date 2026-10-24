"""仅供测试使用的脱敏采集样例，包含重复展示、模糊数量、部分 SKU 和分层回答。"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from ecommerce.modules.collection.detail_reviews import ReviewStore,export_reviews
from ecommerce.modules.collection.detail_questions import QuestionStore,export_questions


def create_sources(settings,task_id,item_id):
    """创建独立临时来源；参数：临时配置、已登记任务及原商品号；返回：原件、批次及缺失详情商品。"""
    instant='2026-10-20T04:00:00+00:00'
    root=settings.workspace/'output/raw/taobao_details'/item_id
    root.mkdir(parents=True)
    document={'schema_version':2,'product_id':item_id,'created_at':instant,'updated_at':instant}
    records={}
    paths={}
    def save(path,value):
        """写临时原件；参数：路径和值；返回：无。"""
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    def image(directory,folder):
        """创建带真实校验和的合成字节文件；参数：历史目录和用途；返回：登记属性。"""
        path=directory/folder/'1.jpg'
        path.parent.mkdir(parents=True,exist_ok=True)
        content=b'synthetic-image-content-for-validation-only'
        path.write_bytes(content)
        return {'index':1,'image_file':folder+'/1.jpg','image_path':str(path.resolve()),
            'image_url':'https://example.invalid/image.jpg?signature=private','sha256':hashlib.sha256(content).hexdigest(),
            'size_bytes':len(content),'content_type':'image/jpeg','width':100,'height':100,'status':'downloaded'}
    modules=['video','main_images','skus','parameters','detail_images','review_summary','reviews','questions']
    for module in modules:
        batch=task_id+'-'+uuid4().hex[:8]
        directory=root/'history'/batch
        directory.mkdir(parents=True)
        record={'schema_version':2 if module in {'main_images','skus'} else 1,'product_id':item_id,
            'task_id':task_id,'batch_id':batch,'captured_at':instant,'status':'complete','scope':'test_'+module,
            'source_url':'https://item.taobao.com/item.htm?id='+item_id}
        filename='sku_details.json' if module=='skus' else module+'.json'
        path=directory/filename
        record['history_record_file']=path.relative_to(root).as_posix()
        if module=='video':
            record.update(video_status='no_video',video_path=None,video_file=None,video_url=None)
        elif module=='main_images':
            record.update(images=[image(directory,'1-1主图')],expected_count=1,collected_count=1)
        elif module=='detail_images':
            record.update(images=[image(directory,'详情图')],list_confirmed=True)
        elif module=='skus':
            record.update(status='partial',candidate_count=1,processed_count=1,product_info={'title':'脱敏样例商品'},groups=[],
                warnings=[{'code':'sku_unavailable_unconfirmed','message':'样例规格不可选'}],blocking_issues=[],
                skus=[{'index':1,'name':'样例规格','sku_id':None,'properties':[],'price':None,'price_value':None,
                    'currency':'CNY','price_status':'unavailable_unconfirmed','stock_status':'unavailable_unconfirmed',
                    'unavailability_evidence':{'disabled':True},'combination_confirmed':True,'image_status':'no_image',
                    'image':None,'captured_at':instant,'price_checked_at':instant}])
        elif module=='parameters':
            record['items']=[{'index':i,'name':'同名参数','name_raw':'同名参数','value':'值'+str(i),'status':'read'} for i in [1,2]]
        elif module=='review_summary':
            record.update(review_count={'raw':'1万+','value':None,'lower_bound':10000,'is_exact':False},
                positive_rate={'raw':'近3个月99.8%','percent':99.8,'period_raw':'近3个月'},tags=[])
        elif module=='reviews':
            value={'index':1,'product_id':item_id,'review_key':'fp:sample:1','review_id':None,
                'identity_method':'fingerprint_occurrence','identity_exact':False,'author':'匿名样例',
                'date_raw':'2天前','date':None,'sku':'样例规格','meta_raw':'2天前 已购：样例规格',
                'content':'<script>unsafe()</script> 脱敏评价','image_urls':['https://example.invalid/review.jpg?signature=private'],
                'video_urls':[],'append_reviews':[],'unresolved_media':[],'captured_at':instant}
            store=ReviewStore(directory,item_id,1)
            with store.connection:
                store.connection.execute('INSERT INTO seen VALUES (?,?)',(value['review_key'],0))
                store.connection.execute('INSERT INTO seen VALUES (?,?)',('fp:template:1',1))
                store.connection.execute('INSERT INTO reviews VALUES (?,?,?)',(1,value['review_key'],json.dumps(value)))
            record.update(mode='limited',requested_limit=1,collected_count=1,scanned_count=2,skipped_template_count=1,
                          end_confirmed=False,stop_reason='limit_reached')
            record=export_reviews(store,directory,record)
            store.close()
        elif module=='questions':
            store=QuestionStore(directory,item_id)
            with store.db:
                for index in [1,2]:
                    q={'index':index,'product_id':item_id,'question_key':'fp:q:'+str(index),'question_id':None,
                        'identity_method':'fingerprint_occurrence','identity_exact':False,'question':'脱敏问题'+str(index),
                        'author':'匿名样例','date_raw':'昨天','captured_at':instant,'answers_complete':True,
                        'answer_stop_reason':'exact_answer_count','answer_count':1}
                    a={'index':1,'question_key':q['question_key'],'answer_key':'fp:a:1','answer_id':None,
                        'identity_method':'fingerprint_occurrence','identity_exact':False,'content':'所属问题'+str(index)+'的回答',
                        'author':'匿名样例','date_raw':'昨天','sku':None,'identity_labels':[],'purchase_labels':[],
                        'image_urls':[],'video_urls':[],'video_poster_urls':[],'unresolved_media':[],'captured_at':instant}
                    store.db.execute('INSERT INTO questions VALUES (?,?,?)',(index,q['question_key'],json.dumps(q)))
                    store.db.execute('INSERT INTO answers VALUES (?,?,?,?)',(q['question_key'],1,a['answer_key'],json.dumps(a)))
            record.update(mode='limited',requested_limit=50,answer_limit=None,collected_count=2,answer_count=2,
                completed_question_count=2,end_confirmed=True,stop_reason='stable_bottom')
            record=export_questions(store,directory,record)
            store.close()
        save(path,record)
        records[module]=record
        paths[module]=path
        if module=='skus':
            document['sku_details_last_attempt']=record
        else:
            document[module]=record
        if module=='main_images':
            document['main_images_last_attempt']=record
    summary_batch=task_id+'-summary-'+uuid4().hex[:8]
    summary={'schema_version':1,'product_id':item_id,'task_id':task_id,'batch_id':summary_batch,
        'status':'complete_with_warnings','started_at':instant,'finished_at':instant,'warnings':[],
        'steps':[{'name':module,'label':module,'requested':True,'status':records[module].get('video_status',records[module]['status']),
            'continuation_allowed':True,'warnings':[],'started_at':instant,'finished_at':instant} for module in modules]}
    summary_path=root/'history'/summary_batch/'collection_summary.json'
    summary['history_record_file']=summary_path.relative_to(root).as_posix()
    save(summary_path,summary)
    document['collection_summary']=summary
    save(root/(item_id+'.json'),document)
    other_id=str(int(item_id)+1)
    for identifier,label in [(item_id,'main'),(other_id,'missing')]:
        path=settings.workspace/'output/raw/taobao_detail_open'/(task_id+label+'.json')
        save(path,{'task_id':task_id,'product_id':identifier,'status':'opened_pending_dom_review','checked_at':instant})
    save(settings.workspace/'output/raw/taobao_search'/(task_id+'.json'),
        {'task_id':task_id,'keyword':'样例','status':'results','checked_at':instant})
    list_batch=task_id+'-'+uuid4().hex[:8]
    listing={'schema_version':2,'batch_id':list_batch,'task_id':task_id,'captured_at':instant,'keyword':'样例',
        'page_number':1,'sort_label':'销量','scope':'first_page_loaded_cards','requested_limit':None,
        'collection_status':'all_loaded_cards_collected','collected_count':2,
        'items':[{'slot_index':slot,'position':slot,'product_index':index,'product_id':item_id,'title':'同商品展示'+str(index),
                  'price_text':'优惠前 ￥2.50','price_value':'2.50','price_label':'优惠前',
                  'ad_status':'not_detected','identity_status':'verified'} for index,slot in enumerate([1,3],1)]}
    list_path=settings.workspace/'output/raw/taobao_products'/(list_batch+'.json')
    save(list_path,listing)
    return {'records':records,'paths':paths,'product_ids':[item_id,other_id],'root':root,'listing':listing,'list_path':list_path}


def save_fixture(settings, connection, fixture):
    """通过实时快照写入器保存脱敏样例；参数：配置、事务连接、来源；返回：各模块写入结果。"""
    from ecommerce.infrastructure.storage.collection_store import CollectionWriter
    from ecommerce.modules.collection.database_records import build_snapshot
    from ecommerce.modules.collection.persistence import validate_publication
    task = fixture['listing']['task_id']
    def apply(kind, record, path, module=None, context=None):
        """写入规范采集快照；参数：类型、原文、文件、模块和发布依据；返回：保存结果。"""
        return CollectionWriter(settings, connection).apply(build_snapshot(settings, kind, record, path, module=module), context)
    result = {'list': apply('batch', fixture['listing'], fixture['list_path'], 'list')}
    search = settings.workspace/'output/raw/taobao_search'/(task+'.json')
    apply('page', json.loads(search.read_text(encoding='utf-8')), search, 'search')
    for label in ('main', 'missing'):
        path = settings.workspace/'output/raw/taobao_detail_open'/(task+label+'.json')
        apply('page', json.loads(path.read_text(encoding='utf-8')), path, 'detail_open')
    path = next((fixture['root']/'history').glob('*/collection_summary.json'))
    report = json.loads(path.read_text(encoding='utf-8'))
    apply('report', report, path)
    for module, record in fixture['records'].items():
        sections = [] if module == 'skus' else [module]
        validate_publication(module, record, sections)
        result[module] = apply('batch', record, fixture['paths'][module], module,
            {'sections': sections, 'last_attempt': True, 'report_key': report['batch_id']})
    return result


def save_fixture(settings, connection, fixture):
    """通过实时快照写入器保存脱敏样例；参数：配置、事务连接、来源；返回：各模块写入结果。"""
    from ecommerce.infrastructure.storage.collection_store import CollectionWriter
    from ecommerce.modules.collection.database_records import build_snapshot
    from ecommerce.modules.collection.persistence import validate_publication
    task = fixture['listing']['task_id']
    def apply(kind, record, path, module=None, context=None):
        """写入规范采集快照；参数：类型、原文、文件、模块和发布依据；返回：保存结果。"""
        return CollectionWriter(settings, connection).apply(build_snapshot(settings, kind, record, path, module=module), context)
    result = {'list': apply('batch', fixture['listing'], fixture['list_path'], 'list')}
    search = settings.workspace/'output/raw/taobao_search'/(task+'.json')
    apply('page', json.loads(search.read_text(encoding='utf-8')), search, 'search')
    for label in ('main', 'missing'):
        path = settings.workspace/'output/raw/taobao_detail_open'/(task+label+'.json')
        apply('page', json.loads(path.read_text(encoding='utf-8')), path, 'detail_open')
    path = next((fixture['root']/'history').glob('*/collection_summary.json'))
    report = json.loads(path.read_text(encoding='utf-8'))
    apply('report', report, path)
    for module, record in fixture['records'].items():
        sections = [] if module == 'skus' else [module]
        validate_publication(module, record, sections)
        result[module] = apply('batch', record, fixture['paths'][module], module,
            {'sections': sections, 'last_attempt': True, 'report_key': report['batch_id']})
    return result
