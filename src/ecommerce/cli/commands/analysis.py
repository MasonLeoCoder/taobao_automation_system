"""评价分析CLI入口，查询与本地重统计不调用AI。"""
import json
from pathlib import Path
from ecommerce.modules.review_analysis import service
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.ai.config import load_config
from ecommerce.infrastructure.storage.time_values import iso_value


def execute(settings, args):
    """调度共用业务；参数：配置和命令参数；返回：退出码。"""
    action = args.action
    if action in {'preview', 'start'}:
        data = service.preview(settings, args.input, args.batch_id)
        if data.get('rule_version')=='reviews-v6':
            data=service.preview(settings,args.input,args.batch_id,model=args.model,output_tokens=args.output_tokens)
        if action == 'preview':
            result = {'product': args.input, 'batch_id': data['batch']['id'], 'source_count': len(data['items']),
                      'planned_calls': data['planned_calls'], 'reserved_tokens': data['reserved_tokens'],
                      'recommended_tokens': data['default_tokens'], 'plan_hash': data['plan_hash'], 'plan': data['plan'],
                      'report_template': data['report_template'], 'input_hash': data['input_hash'], 'models': list(data['models'])}
        else:
            result = service.start(settings, args.input, batch_id=data['batch']['id'], model=args.model or load_config(settings.root).model,
                input_hash=data['input_hash'], plan_hash=data['plan_hash'], max_calls=args.max_calls, max_tokens=args.max_tokens,
                allow_paid=args.allow_paid, force=args.force,**({'output_tokens':args.output_tokens} if data.get('rule_version')=='reviews-v6' else {}))
    elif action == 'history':
        result = repository.history(settings, service.product_id(args.input))
    elif action == 'resume':
        data = service.read(settings,args.task_id)
        if args.preview:
            result = {'task_id':args.task_id,'needs_local_recovery':data['needs_local_recovery'],'pending_plan':data['resume_budget']}
        else:
            result = service.resume(settings, args.task_id, allow_paid=args.allow_paid, acknowledge_unknown=args.acknowledge_unknown,
                                    max_calls=args.max_calls, max_tokens=args.max_tokens,plan_hash=data['resume_budget']['plan_hash'])
    elif action == 'recover':
        result = service.recover_saved(settings,args.task_id)
    elif action == 'recount':
        result = service.recount(settings, args.task_id)
    else:
        data = service.read(settings, args.task_id)
        result = {'task': data['task'], 'report': data['report']} if action == 'status' else data
        if action == 'export':
            path = Path(args.output)
            if not path.is_absolute():
                path = settings.root / path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2, default=iso_value)
            print('已导出：' + str(path))
            return 0
    print(json.dumps(result, ensure_ascii=False, indent=2, default=iso_value))
    return 0
