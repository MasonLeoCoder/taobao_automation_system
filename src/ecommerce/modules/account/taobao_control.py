"""向当前淘宝任务发送暂停或停止请求，不启动第二个浏览器。"""
import json
import re
from uuid import uuid4

from ecommerce.infrastructure.storage.database import initialize, list_tasks


def request_control(settings, action, *, task_id=None, prompt_id=None):
    replies = {"continue": "继续", "confirm": "确认", "cancel": "取消"}
    if action not in {"pause", "stop", *replies}:
        raise ValueError("不支持的淘宝控制指令")
    if settings.task_backend=='mysql' and settings.collection_backend=='mysql':
        from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
        from ecommerce.infrastructure.storage.mysql import MySQLStorageError
        from ecommerce.infrastructure.tasks.ownership import execution_key
        persistence=CollectionJobStore(settings)
        try:
            matches=persistence.rows(execution_key(settings),pending=True)
            matches=[row for row in matches if row['id']==task_id] if task_id else [row for row in matches if row['dispatch_state'] in {'claimed','started'}]
            if len(matches)>1:
                raise ValueError('发现多个采集申请，请指定任务编号')
            if matches:
                row=matches[0]
                if action=='stop':
                    from ecommerce.modules.collection.dispatch import pause_queue
                    pause_queue(settings)
                prompt_id=prompt_id or (row.get('prompt') or {}).get('prompt_id')
                persistence.control(row['id'],action,prompt_id)
                return '采集控制指令已保存到数据库，等待当前执行器接收'
        except MySQLStorageError:
            if action!='stop' or not task_id:
                raise
            from ecommerce.infrastructure.tasks.collection_control import emergency_stop
            return emergency_stop(settings,task_id)
    if settings.task_backend=='mysql':
        from ecommerce.infrastructure.tasks.ownership import active_local_tasks
        tasks=[task for task in active_local_tasks(settings) if task['name'].startswith(('account.taobao.','collection.taobao.'))]
        if not tasks:
            # 没有可核实的本地活跃进程时，不向数据库里的遗留记录盲目发送操作。
            return '当前没有可确认执行中的淘宝任务'
    else:
        initialize(settings.task_database)
        tasks = [task for task in list_tasks(settings.task_database) if task["name"].startswith(("account.taobao.", "collection.taobao."))
                 and task["finished_at"] is None and task["status"] in {"running", "needs_attention"}]
    if task_id is not None:
        tasks=[task for task in tasks if task['id']==task_id]
        if not tasks:
            raise ValueError('指定任务已结束或无法核实所属进程')
    if not tasks:
        return "当前没有执行中的淘宝任务"
    if len(tasks) != 1:
        raise ValueError("发现多个未结束的淘宝任务，请先核对任务记录")
    task_id = tasks[0]["id"]
    if action in replies and tasks[0].get('storage_pending'):
        raise ValueError('当前等待数据库恢复，不能用继续或确认绕过存储暂停；可发送停止')
    if action in replies and settings.task_backend=='mysql':
        from ecommerce.modules.monitoring.service import local_task_alerts
        if local_task_alerts(settings,task_id,active_tasks=tasks):
            raise ValueError('任务已中断或结果待入库，不能继续或确认；可发送停止并核对日志')
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("任务编号格式无效")
    directory = settings.workspace / "storage/task_controls"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}.json"
    request = {"action": action}
    if action in replies:
        state_path = path.with_suffix(".state.json")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("当前任务没有可接收反馈的提示，请等待任务提示后再操作") from error
        if not state.get("prompt_id") or replies[action] not in state.get("allowed", []):
            raise ValueError("当前提示不接受这个指令，请按任务提示输入继续、确认或取消")
        if prompt_id is not None and prompt_id != state['prompt_id']:
            raise ValueError('提示已更新，请按当前弹窗重新选择')
        request["prompt_id"] = state["prompt_id"]
    temporary = directory / f"{task_id}-{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(request), encoding="utf-8")
    temporary.replace(path)
    name = {"pause": "暂停", "stop": "停止", **replies}[action]
    return f"已发送{name}请求，等待当前淘宝任务的检查点接收；任务编号：{task_id}"
